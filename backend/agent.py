"""
Doc2MD Agent - 核心转换管线

流水线：
  1. [预处理] pandoc 提取 docx → 粗糙 markdown + 图片
  2. [AI 分析] 分析文档结构（标题层级、文档类型）
  3. [AI 转换] 分片发送给 AI，逐片转换为优质 markdown
  4. [后处理] 合并片段、生成目录、修复图片路径、打包输出
"""

import json
import re
import logging
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from backend.llm_provider import LLMProvider
from backend.preprocessor import DocPreprocessor, split_content, fix_pandoc_table_codeblocks
from backend.prompts import (
    ANALYZE_STRUCTURE_SYSTEM, ANALYZE_STRUCTURE_USER,
    CONVERT_SYSTEM, CONVERT_USER,
    GENERATE_TOC_SYSTEM, GENERATE_TOC_USER,
)

logger = logging.getLogger(__name__)


class Doc2MDAgent:
    """文档转 Markdown 智能体"""

    def __init__(self, config: dict, event_callback: Optional[Callable[[dict[str, Any]], None]] = None):
        self.config = config
        self.event_callback = event_callback
        self.llm = LLMProvider(config, event_callback=self._emit_event)
        self.conv_config = config.get("conversion", {})
        self.chunk_size = self.conv_config.get("chunk_size", 8000)
        self.image_dir = self.conv_config.get("image_dir", "images")
        self.generate_toc = self.conv_config.get("generate_toc", True)
        self.strict_mode = self.conv_config.get("strict_mode", True)
        self.chunk_strategy = self.conv_config.get("chunk_strategy", "section")
        self.max_chunk_retries = self.conv_config.get("max_chunk_retries", 2)
        self.deterministic_toc = self.conv_config.get("deterministic_toc", True)
        self.max_validation_report_items = self.conv_config.get("max_validation_report_items", 8)

    def _emit_event(self, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(payload)

    def _report_progress(
        self,
        progress_callback: Optional[Callable[..., None]],
        stage: str,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if not progress_callback:
            return
        try:
            progress_callback(stage, current, total, message)
        except TypeError:
            progress_callback(stage, current, total)

    def convert(
        self,
        input_path: str,
        output_dir: str,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> tuple[str, dict]:
        """
        完整转换流程
        :param input_path: 输入的 docx 文件路径
        :param output_dir: 输出目录
        :return: (输出的 markdown 文件路径, token 用量摘要)
        """
        input_path = Path(input_path)
        output_dir = Path(output_dir)
        work_dir = output_dir / ".work"

        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        self._emit_event(
            {
                "type": "pipeline_started",
                "message": f"开始处理文档：{input_path.name}",
            }
        )

        # ========== 第 1 步：预处理 ==========
        logger.info("=" * 50)
        logger.info("📄 第 1 步：提取文档内容和图片")
        logger.info("=" * 50)
        self._report_progress(progress_callback, "preprocess", 0, 4, "预处理中：初始化提取器")

        preprocessor = DocPreprocessor(
            input_path=str(input_path),
            work_dir=str(work_dir),
            image_dir=self.image_dir,
        )
        self._report_progress(progress_callback, "preprocess", 1, 4, "预处理中：调用 pandoc 提取正文与图片")
        raw_md, images = preprocessor.extract()
        self._report_progress(
            progress_callback,
            "preprocess",
            2,
            4,
            f"预处理中：提取完成，正文约 {len(raw_md)} 字符",
        )

        # 预处理：将 pandoc 单列表格（含 JSON 等）转为代码块
        raw_md = fix_pandoc_table_codeblocks(raw_md)
        logger.info("已完成 pandoc 表格代码块修复")
        self._report_progress(progress_callback, "preprocess", 3, 4, "预处理中：修复表格中的代码块")

        # 整理图片
        image_mapping = preprocessor.organize_images(output_dir, images)
        logger.info(f"图片路径映射: {image_mapping}")
        self._report_progress(
            progress_callback,
            "preprocess",
            4,
            4,
            f"预处理完成：整理图片 {len(images)} 张",
        )
        self._emit_event(
            {
                "type": "preprocess_completed",
                "image_count": len(images),
                "message": f"预处理完成：提取正文并整理图片 {len(images)} 张",
            }
        )

        # ========== 第 2 步：结构分析（规则优先） ==========
        logger.info("=" * 50)
        logger.info("🔍 第 2 步：分析文档结构")
        logger.info("=" * 50)
        self._report_progress(progress_callback, "analyze", 0, 1, "结构分析中：规则提取目录与章节")

        expected_headings = self._extract_expected_headings_from_toc(raw_md)
        structure = self._build_rule_based_structure(raw_md, expected_headings)

        # 若规则提取不到可用结构，再回退到 AI 分析
        if not structure.get("heading_mapping"):
            logger.warning("规则结构提取失败，回退 AI 分析")
            analyze_content = raw_md[:3000]
            ai_structure = self._analyze_structure(analyze_content)
            structure["heading_mapping"] = ai_structure.get("heading_mapping", {})
            structure["doc_type"] = ai_structure.get("doc_type", structure.get("doc_type", "api_doc"))

        logger.info(f"文档类型: {structure.get('doc_type', 'unknown')}")
        logger.info(f"目录标题数: {len(expected_headings)}")
        logger.info(f"标题映射: {structure.get('heading_mapping', {})}")
        self._report_progress(
            progress_callback,
            "analyze",
            1,
            1,
            f"结构分析完成：文档类型 {structure.get('doc_type', 'unknown')}",
        )

        # ========== 第 3 步：AI 分片转换 ==========
        logger.info("=" * 50)
        logger.info("✨ 第 3 步：AI 转换为优质 Markdown")
        logger.info("=" * 50)

        # 跳过目录部分（通常在正文标题之前）
        content_start = self._find_content_start(raw_md)
        content_body = raw_md[content_start:]

        if self.chunk_strategy == "section":
            chunk_jobs = self._build_section_chunks(content_body, expected_headings)
        else:
            chunk_jobs = [
                {
                    "content": chunk,
                    "section_id": f"chunk-{idx + 1}",
                    "section_heading": "",
                    "allowed_headings": [],
                    "continuation_mode": False,
                    "chunk_has_heading": bool(re.search(r'^\s*#\s+', chunk, flags=re.MULTILINE)),
                    "previous_heading": "",
                    "next_heading": "",
                }
                for idx, chunk in enumerate(split_content(content_body, self.chunk_size))
            ]

        if not chunk_jobs:
            raise RuntimeError("正文切分失败：未生成任何分片")

        converted_chunks = []
        planned_llm_calls = len(chunk_jobs)
        if self.generate_toc and not self.deterministic_toc:
            planned_llm_calls += 1
        self._emit_event(
            {
                "type": "llm_plan",
                "planned_calls": planned_llm_calls,
                "chunk_count": len(chunk_jobs),
                "message": f"正文已分为 {len(chunk_jobs)} 个片段，预计调用大模型 {planned_llm_calls} 次",
            }
        )

        for i, job in enumerate(chunk_jobs):
            chunk = job["content"]
            logger.info(
                "正在转换第 %s/%s 个片段（section=%s, continuation=%s, %s 字符）",
                i + 1,
                len(chunk_jobs),
                job["section_id"],
                job["continuation_mode"],
                len(chunk),
            )
            self._report_progress(
                progress_callback,
                "convert",
                i,
                len(chunk_jobs),
                f"AI 转换中：准备处理第 {i+1}/{len(chunk_jobs)} 个分片（{len(chunk)} 字符）",
            )
            converted = self._convert_chunk_with_retry(
                chunk=chunk,
                structure=structure,
                chunk_index=i + 1,
                total_chunks=len(chunk_jobs),
                section_id=job["section_id"],
                section_heading=job["section_heading"],
                allowed_headings=job["allowed_headings"],
                continuation_mode=job["continuation_mode"],
                chunk_has_heading=job["chunk_has_heading"],
                previous_heading=job["previous_heading"],
                next_heading=job["next_heading"],
            )
            converted_chunks.append(converted)
            self._report_progress(
                progress_callback,
                "convert",
                i + 1,
                len(chunk_jobs),
                f"AI 转换中：已完成第 {i+1}/{len(chunk_jobs)} 个分片",
            )

        # ========== 第 4 步：后处理 ==========
        logger.info("=" * 50)
        logger.info("📦 第 4 步：后处理和组装")
        logger.info("=" * 50)

        # 合并所有片段
        full_md = "\n\n".join(converted_chunks)

        # 修复图片路径
        full_md = self._fix_image_paths(full_md, image_mapping)

        # 清除标题中的 {#xxx} 锚点属性（pandoc / AI 残留）
        full_md = re.sub(
            r'^(#{1,6}\s+.+?)\s*\{#[^}]*\}\s*$',
            r'\1',
            full_md,
            flags=re.MULTILINE,
        )

        # 统一表格中的树形符号：├── / └── → └─
        full_md = full_md.replace('├──', '└─')
        full_md = full_md.replace('└──', '└─')

        # 相邻的加粗行之间加空行（避免渲染成一行）
        full_md = re.sub(
            r'^(\*\*[^*]+\*\*)\n(\*\*[^*]+\*\*)$',
            r'\1\n\n\2',
            full_md,
            flags=re.MULTILINE,
        )

        # 生成目录
        if self.generate_toc:
            self._report_progress(progress_callback, "toc", 0, 1, "后处理中：生成目录")
            if self.deterministic_toc:
                toc = self._simple_toc(full_md)
            else:
                toc = self._generate_toc(full_md)
            # 在标题后插入目录
            full_md = self._insert_toc(full_md, toc)
            self._report_progress(progress_callback, "toc", 1, 1, "后处理中：目录已插入文档")

        # 清理 AI 输出中可能残留的 markdown 代码块标记
        full_md = self._clean_output(full_md)

        if self.strict_mode:
            self._validate_final_output(raw_md=raw_md, final_md=full_md, expected_headings=expected_headings)

        # 写入输出文件
        stem = input_path.stem
        output_file = output_dir / f"{stem}.md"
        output_file.write_text(full_md, encoding="utf-8")

        # 清理工作目录
        shutil.rmtree(work_dir, ignore_errors=True)

        logger.info(f"✅ 转换完成: {output_file}")
        logger.info(f"   输出目录: {output_dir}")
        logger.info(f"   图片目录: {output_dir / self.image_dir}")

        usage = self.llm.get_usage_summary()
        self._report_progress(progress_callback, "done", 1, 1, "转换完成")
        self._emit_event(
            {
                "type": "pipeline_completed",
                "output_file": str(output_file),
                "llm_calls": usage.get("llm_calls", 0),
                "message": f"转换完成，输出文件：{output_file.name}",
            }
        )
        return str(output_file), usage

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------

    def _normalize_heading_text(self, heading: str) -> str:
        """标题比较归一化：忽略空白差异。"""
        return re.sub(r'\s+', '', heading.strip())

    def _extract_section_id(self, numbered_heading: str) -> str:
        match = re.match(r'^(\d+(?:\.\d+)*)\s+', numbered_heading.strip())
        return match.group(1) if match else ""

    def _extract_expected_headings_from_toc(self, raw_md: str) -> list[str]:
        """从原始提取内容中的目录行提取编号标题序列。"""
        headings = []
        for line in raw_md.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# "):
                break
            match = re.match(r'^\[(\d+(?:\.\d+)*\s+.+?)\s+\[\d+\]\(#', stripped)
            if match:
                headings.append(match.group(1).strip())
        return headings

    def _build_rule_based_structure(self, raw_md: str, expected_headings: list[str]) -> dict[str, Any]:
        """基于目录编号构建结构信息，避免 AI 自行猜测层级。"""
        title = ""
        for line in raw_md.split("\n")[:30]:
            m = re.match(r'^\*\*(.+?)\*\*$', line.strip())
            if m and "说明书" in m.group(1):
                title = m.group(1).strip()
                break

        heading_mapping: dict[str, str] = {}
        sections = []
        for heading in expected_headings:
            m = re.match(r'^(\d+(?:\.\d+)*)\s+(.+)$', heading)
            if not m:
                continue
            section_id = m.group(1)
            section_title = m.group(2).strip()
            level = min(6, len(section_id.split(".")) + 1)  # 1 -> ##, 1.1 -> ###
            heading_mapping[section_id] = "#" * level
            sections.append({"id": section_id, "title": section_title, "level": level})

        return {
            "title": title,
            "doc_type": "api_doc",
            "heading_mapping": heading_mapping,
            "has_toc": bool(expected_headings),
            "has_json_examples": True,
            "sections": sections,
        }

    def _split_raw_sections(self, content_body: str) -> list[dict[str, Any]]:
        """按原始一级标题（pandoc 提取后的 `#` 行）切分正文。"""
        lines = content_body.split("\n")
        sections: list[list[str]] = []
        current: list[str] = []

        for line in lines:
            if re.match(r'^\s*#\s+', line):
                if current:
                    sections.append(current)
                current = [line]
            else:
                if not current:
                    current = [line]
                else:
                    current.append(line)

        if current:
            sections.append(current)

        result = []
        for section_lines in sections:
            content = "\n".join(section_lines)
            first_non_empty = next((ln for ln in section_lines if ln.strip()), "")
            has_heading = bool(re.match(r'^\s*#\s+', first_non_empty))
            heading_text = ""
            if has_heading:
                heading_text = re.sub(r'^\s*#\s+', '', first_non_empty).strip()
                heading_text = self._strip_heading_attrs(heading_text)
            result.append(
                {
                    "content": content,
                    "has_heading": has_heading,
                    "heading_text": heading_text,
                }
            )
        return result

    def _build_section_chunks(self, content_body: str, expected_headings: list[str]) -> list[dict[str, Any]]:
        """先按章节切，再对子章节内超长内容继续分片。"""
        sections = self._split_raw_sections(content_body)
        jobs: list[dict[str, Any]] = []
        heading_index = 0

        for section in sections:
            has_heading = bool(section["has_heading"])
            numbered_heading = ""
            section_id = ""

            if has_heading:
                if heading_index < len(expected_headings):
                    numbered_heading = expected_headings[heading_index]
                else:
                    numbered_heading = section["heading_text"]
                section_id = self._extract_section_id(numbered_heading) or f"section-{heading_index + 1}"
                prev_heading = expected_headings[heading_index - 1] if heading_index > 0 else ""
                next_heading = expected_headings[heading_index + 1] if heading_index + 1 < len(expected_headings) else ""
                heading_index += 1
            else:
                section_id = f"preamble-{len(jobs) + 1}"
                prev_heading = expected_headings[heading_index - 1] if heading_index > 0 else ""
                next_heading = expected_headings[heading_index] if heading_index < len(expected_headings) else ""

            section_chunks = split_content(section["content"], self.chunk_size)
            for idx, chunk in enumerate(section_chunks):
                if not chunk.strip():
                    continue
                chunk_has_heading = bool(re.search(r'^\s*#\s+', chunk, flags=re.MULTILINE))
                jobs.append(
                    {
                        "content": chunk,
                        "section_id": section_id,
                        "section_heading": numbered_heading,
                        "allowed_headings": [numbered_heading] if numbered_heading else [],
                        "continuation_mode": idx > 0 or not chunk_has_heading,
                        "chunk_has_heading": chunk_has_heading,
                        "previous_heading": prev_heading,
                        "next_heading": next_heading,
                    }
                )

        return jobs

    def _extract_numbered_headings(self, markdown: str) -> list[str]:
        headings = []
        for line in self._remove_fenced_code_blocks(markdown).split("\n"):
            match = re.match(r'^#{2,6}\s+(.+)$', line)
            if not match:
                continue
            title = self._strip_heading_attrs(match.group(1).strip())
            if title == "目录":
                continue
            if re.match(r'^\d', title):
                headings.append(title)
        return headings

    def _extract_error_codes(self, text: str) -> set[str]:
        """
        提取错误码（表格或普通文本行）。
        仅用于“错误码章节”对比，避免模型扩写大量不存在编码。
        """
        codes = set(re.findall(r'^\s*\|?\s*(\d{4,6})\s*(?:\||\s{2,})', text, flags=re.MULTILINE))
        return {code for code in codes if code.isdigit()}

    def _extract_json_blocks(self, text: str) -> list[str]:
        """提取 ```json fenced code block 内容。"""
        pattern = re.compile(r'```json\s*\n(.*?)\n```', re.S)
        return [m.group(1).strip() for m in pattern.finditer(text)]

    def _sanitize_json_like_text(self, text: str) -> str:
        """
        对 JSON-like 文本做轻量修复后用于解析：
        - 处理 NBSP/转义符
        - 去掉尾随逗号
        - 将带字母的裸值（如 1118xxxx5311）转为字符串
        """
        s = text.replace("\u00a0", " ").strip()
        s = s.replace('\\"', '"')
        s = s.replace('\\[', '[')
        s = s.replace('\\]', ']')
        s = re.sub(r',\s*([}\]])', r'\1', s)

        def quote_masked_literals(m):
            prefix, value, suffix = m.group(1), m.group(2), m.group(3)
            lower = value.lower()
            if lower in {"true", "false", "null"}:
                return m.group(0)
            if re.fullmatch(r'-?\d+(?:\.\d+)?', value):
                return m.group(0)
            return f'{prefix}"{value}"{suffix}'

        s = re.sub(
            r'(:\s*)([A-Za-z0-9_./:+-]*[A-Za-z][A-Za-z0-9_./:+-]*)(\s*[,}\]])',
            quote_masked_literals,
            s,
        )
        return s

    def _normalize_json_block(self, block_text: str) -> tuple[str, bool]:
        """返回 (规范化后的 JSON 字符串, 是否可解析)。"""
        candidate = self._sanitize_json_like_text(block_text)
        try:
            parsed = json.loads(candidate)
            return json.dumps(parsed, ensure_ascii=False, indent=2), True
        except Exception:
            return block_text.strip(), False

    def _replace_output_json_blocks_with_source(self, source_chunk: str, converted_chunk: str) -> str:
        """
        若源分片存在 JSON 代码块，则优先回填源 JSON（规范化后）到输出中，
        避免模型改写/补写返回体示例。
        """
        source_blocks = self._extract_json_blocks(source_chunk)
        if not source_blocks:
            return converted_chunk

        normalized_sources = []
        for block in source_blocks:
            normalized, ok = self._normalize_json_block(block)
            normalized_sources.append(normalized if ok else block.strip())

        pattern = re.compile(r'```json\s*\n(.*?)\n```', re.S)
        matches = list(pattern.finditer(converted_chunk))
        if not matches:
            appended = "\n\n".join([f"```json\n{blk}\n```" for blk in normalized_sources])
            if not converted_chunk.strip():
                return appended
            return converted_chunk.rstrip() + "\n\n" + appended

        replace_count = min(len(matches), len(normalized_sources))
        parts = []
        last_end = 0
        for idx, match in enumerate(matches):
            parts.append(converted_chunk[last_end:match.start()])
            if idx < replace_count:
                parts.append(f"```json\n{normalized_sources[idx]}\n```")
            else:
                parts.append(match.group(0))
            last_end = match.end()
        parts.append(converted_chunk[last_end:])
        if len(matches) < len(normalized_sources):
            missing = "\n\n".join(
                [f"```json\n{blk}\n```" for blk in normalized_sources[len(matches):]]
            )
            parts.append("\n\n" + missing)
        return "".join(parts)

    def _remove_fenced_code_blocks(self, text: str) -> str:
        """移除 fenced code block，避免把代码内的 # 误判为标题。"""
        cleaned = []
        in_code_block = False
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                in_code_block = not in_code_block
                continue
            if not in_code_block:
                cleaned.append(line)
        return "\n".join(cleaned)

    def _validate_chunk_output(
        self,
        source_chunk: str,
        converted_chunk: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        llm_meta: dict[str, Any],
    ) -> tuple[bool, str]:
        if llm_meta.get("truncated"):
            return False, f"模型输出被截断（finish_reason={llm_meta.get('finish_reason')}）"

        output = converted_chunk.strip()
        if not output:
            return False, "模型返回空内容"

        output_no_code = self._remove_fenced_code_blocks(output)
        heading_lines = re.findall(r'^\s*#{1,6}\s+.+$', output_no_code, flags=re.MULTILINE)
        if continuation_mode and heading_lines:
            return False, "续片输出包含标题行（continuation_mode=true）"

        allowed_norm = {self._normalize_heading_text(h) for h in allowed_headings if h}
        output_numbered = self._extract_numbered_headings(output)
        output_numbered_norm = [self._normalize_heading_text(h) for h in output_numbered]

        if output_numbered_norm:
            if continuation_mode:
                return False, "续片输出了编号标题"
            if not allowed_norm:
                return False, f"当前片段不允许编号标题，但输出了 {output_numbered}"
            for heading in output_numbered_norm:
                if heading not in allowed_norm:
                    return False, f"输出了不允许的标题: {heading}"
            if len(output_numbered_norm) > len(allowed_norm):
                return False, "输出标题数量超过允许范围"

        if not continuation_mode and allowed_norm and not output_numbered_norm:
            return False, "缺少必须的编号标题"

        source_json_blocks = self._extract_json_blocks(source_chunk)
        output_json_blocks = self._extract_json_blocks(output)
        if source_json_blocks:
            if len(output_json_blocks) != len(source_json_blocks):
                return False, (
                    f"JSON 代码块数量不一致（source={len(source_json_blocks)}, output={len(output_json_blocks)}）"
                )
        for idx, block in enumerate(output_json_blocks, start=1):
            _, ok = self._normalize_json_block(block)
            if not ok:
                return False, f"第 {idx} 个 JSON 代码块不是合法 JSON"

        # “错误码”片段增加子集校验，防止 100000+ 幻觉扩写
        if "错误码" in source_chunk:
            source_codes = self._extract_error_codes(source_chunk)
            output_codes = self._extract_error_codes(output)
            if source_codes and output_codes and not output_codes.issubset(source_codes):
                extras = sorted(output_codes - source_codes)[:5]
                return False, f"检测到输入中不存在的错误码: {extras}"

        return True, ""

    def _convert_chunk_with_retry(
        self,
        chunk: str,
        structure: dict,
        chunk_index: int,
        total_chunks: int,
        section_id: str,
        section_heading: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        chunk_has_heading: bool,
        previous_heading: str,
        next_heading: str,
    ) -> str:
        """分片转换 + 严格校验重试。"""
        last_error = ""
        for attempt in range(self.max_chunk_retries + 1):
            converted, meta = self._convert_chunk(
                chunk=chunk,
                structure=structure,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                section_id=section_id,
                section_heading=section_heading,
                allowed_headings=allowed_headings,
                continuation_mode=continuation_mode,
                chunk_has_heading=chunk_has_heading,
                previous_heading=previous_heading,
                next_heading=next_heading,
                retry_reason=last_error if attempt > 0 else "",
            )
            if re.match(r'^\s*```markdown\s*\n', converted):
                converted = re.sub(r'^\s*```markdown\s*\n', '', converted)
                converted = re.sub(r'\n```\s*$', '', converted)
            converted = self._replace_output_json_blocks_with_source(chunk, converted)
            valid, reason = self._validate_chunk_output(
                source_chunk=chunk,
                converted_chunk=converted,
                allowed_headings=allowed_headings,
                continuation_mode=continuation_mode,
                llm_meta=meta,
            )
            if valid:
                return converted

            last_error = reason
            logger.warning(
                "分片校验失败，准备重试: chunk=%s/%s section=%s attempt=%s/%s reason=%s",
                chunk_index,
                total_chunks,
                section_id,
                attempt + 1,
                self.max_chunk_retries + 1,
                reason,
            )

        raise RuntimeError(
            f"分片转换失败：第 {chunk_index}/{total_chunks} 片段在 {self.max_chunk_retries + 1} 次尝试后仍不合规，最后错误：{last_error}"
        )

    def _analyze_structure(self, content: str) -> dict:
        """调用 AI 分析文档结构"""
        prompt = ANALYZE_STRUCTURE_USER.format(content=content)

        try:
            response = self.llm.chat(
                ANALYZE_STRUCTURE_SYSTEM,
                prompt,
                context={"operation": "analyze_structure"},
            )
            # 去掉 ```json ``` 包裹
            response = re.sub(r'```json\s*', '', response)
            response = re.sub(r'```\s*', '', response)
            # 提取最外层 JSON 对象
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                json_str = json_match.group()
                # 尝试修复常见 JSON 问题：尾随逗号
                json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
                return json.loads(json_str)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"结构分析失败，使用默认结构: {e}")

        # 默认结构
        return {
            "doc_type": "api_doc",
            "heading_mapping": {},
            "has_toc": True,
            "has_json_examples": True,
        }

    def _convert_chunk(
        self,
        chunk: str,
        structure: dict,
        chunk_index: int,
        total_chunks: int,
        section_id: str,
        section_heading: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        chunk_has_heading: bool,
        previous_heading: str,
        next_heading: str,
        retry_reason: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """调用 AI 转换一个内容片段，并返回元信息用于校验。"""
        prompt = CONVERT_USER.format(
            structure=json.dumps(structure, ensure_ascii=False, indent=2),
            section_id=section_id or "(none)",
            section_heading=section_heading or "(none)",
            continuation_mode=str(continuation_mode).lower(),
            chunk_has_heading=str(chunk_has_heading).lower(),
            allowed_headings=", ".join(allowed_headings) if allowed_headings else "(none)",
            previous_heading=previous_heading or "(none)",
            next_heading=next_heading or "(none)",
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            content=chunk,
        )
        if retry_reason:
            prompt += f"\n\n上一次输出不符合约束，失败原因：{retry_reason}\n请严格重新输出完整片段。"

        response = self.llm.chat_with_meta(
            CONVERT_SYSTEM,
            prompt,
            context={
                "operation": "convert_chunk",
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "section_id": section_id,
            },
        )
        return response.get("content", ""), response

    def _extract_error_code_sets_by_section(self, text: str) -> list[set[str]]:
        """按“错误码”章节顺序提取错误码集合。"""
        sections = []
        current_heading = ""
        current_lines: list[str] = []

        for line in text.split("\n"):
            if re.match(r'^\s*#{1,6}\s+', line):
                if current_lines:
                    sections.append((current_heading, "\n".join(current_lines)))
                current_heading = re.sub(r'^\s*#{1,6}\s+', '', line).strip()
                current_heading = self._strip_heading_attrs(current_heading)
                current_lines = [line]
            else:
                if not current_lines:
                    current_lines = [line]
                else:
                    current_lines.append(line)

        if current_lines:
            sections.append((current_heading, "\n".join(current_lines)))

        code_sets = []
        for heading, section_text in sections:
            heading_plain = re.sub(r'^\d+(?:\.\d+)*\s+', '', heading).strip()
            if "错误码" in heading_plain:
                code_sets.append(self._extract_error_codes(section_text))
        return code_sets

    def _validate_final_output(self, raw_md: str, final_md: str, expected_headings: list[str]) -> None:
        """最终输出硬校验：标题完整性与错误码不扩写。"""
        issues = []

        # 1) 标题序列完整性校验
        if expected_headings:
            expected_norm = [self._normalize_heading_text(h) for h in expected_headings]
            actual = self._extract_numbered_headings(final_md)
            actual_norm = [self._normalize_heading_text(h) for h in actual]

            expected_counter = Counter(expected_norm)
            actual_counter = Counter(actual_norm)

            missing = []
            extras = []
            for heading, count in expected_counter.items():
                diff = count - actual_counter.get(heading, 0)
                if diff > 0:
                    missing.extend([heading] * diff)
            for heading, count in actual_counter.items():
                diff = count - expected_counter.get(heading, 0)
                if diff > 0:
                    extras.extend([heading] * diff)

            if missing:
                issues.append(f"缺失标题 {len(missing)} 个，例如: {missing[:self.max_validation_report_items]}")
            if extras:
                issues.append(f"新增/重复标题 {len(extras)} 个，例如: {extras[:self.max_validation_report_items]}")

        # 2) 文档主标题只允许 1 个
        h1_count = len(re.findall(r'^#\s+.+$', self._remove_fenced_code_blocks(final_md), flags=re.MULTILINE))
        if h1_count > 1:
            issues.append(f"文档一级标题重复: {h1_count} 个")

        # 3) 错误码章节不得扩写
        raw_code_sets = self._extract_error_code_sets_by_section(raw_md)
        final_code_sets = self._extract_error_code_sets_by_section(final_md)
        for idx, final_codes in enumerate(final_code_sets):
            if idx >= len(raw_code_sets):
                if final_codes:
                    issues.append(
                        f"错误码章节数量超出原文（第 {idx + 1} 节），新增代码示例: {sorted(final_codes)[:self.max_validation_report_items]}"
                    )
                continue
            raw_codes = raw_code_sets[idx]
            if raw_codes and final_codes and not final_codes.issubset(raw_codes):
                extras = sorted(final_codes - raw_codes)[:self.max_validation_report_items]
                issues.append(f"错误码章节第 {idx + 1} 节存在原文未出现编码: {extras}")

        # 4) JSON 代码块必须可解析（允许掩码字段做轻量修复后解析）
        invalid_json_indices = []
        for idx, block in enumerate(self._extract_json_blocks(final_md), start=1):
            _, ok = self._normalize_json_block(block)
            if not ok:
                invalid_json_indices.append(idx)
        if invalid_json_indices:
            issues.append(
                f"JSON 代码块格式错误: {invalid_json_indices[:self.max_validation_report_items]}"
            )

        if issues:
            raise RuntimeError("最终输出校验失败: " + "；".join(issues))

    def _generate_toc(self, markdown: str) -> str:
        """从最终 markdown 中提取标题并生成目录（跳过一级标题/文档标题）"""
        headings = []
        for line in markdown.split("\n"):
            match = re.match(r'^(#{2,6})\s+(.+)$', line)
            if match:
                level = len(match.group(1))
                title = self._strip_heading_attrs(match.group(2).strip())
                if title == "目录":
                    continue
                headings.append(f"{'  ' * (level - 2)}- {title}")

        if not headings:
            return ""

        headings_text = "\n".join(headings)

        try:
            prompt = GENERATE_TOC_USER.format(headings=headings_text)
            toc = self.llm.chat(
                GENERATE_TOC_SYSTEM,
                prompt,
                context={"operation": "generate_toc"},
            )
            return toc
        except Exception as e:
            logger.warning(f"AI 目录生成失败，使用简单目录: {e}")
            self._emit_event(
                {
                    "type": "toc_fallback",
                    "message": f"目录生成失败，已切换简单目录策略：{e}",
                }
            )
            return self._simple_toc(markdown)

    def _strip_heading_attrs(self, title: str) -> str:
        """去除标题中残留的 {#xxx} 等属性"""
        return re.sub(r'\s*\{#[^}]*\}\s*$', '', title).strip()

    def _simple_toc(self, markdown: str) -> str:
        """简单的目录生成（不依赖 AI），跳过一级标题和目录标题"""
        toc_lines = []
        for line in markdown.split("\n"):
            match = re.match(r'^(#{2,6})\s+(.+)$', line)
            if match:
                level = len(match.group(1))
                title = self._strip_heading_attrs(match.group(2).strip())
                if title == "目录":
                    continue
                anchor = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', title.lower())
                anchor = anchor.strip().replace(' ', '-')
                indent = "  " * (level - 2)
                toc_lines.append(f"{indent}- [{title}](#{anchor})")

        return "\n".join(toc_lines)

    def _insert_toc(self, markdown: str, toc: str) -> str:
        """在文档标题和副标题信息后、正文第一个章节标题前插入目录"""
        lines = markdown.split("\n")

        # 找到第一个一级标题 (# xxx)
        title_pos = -1
        for i, line in enumerate(lines):
            if line.startswith("# ") and not line.startswith("## "):
                title_pos = i
                break

        if title_pos < 0:
            title_pos = 0

        # 在一级标题之后，找到第一个二级及以下标题（## 开头）
        # TOC 插入在该标题之前
        insert_pos = title_pos + 1
        for i in range(title_pos + 1, len(lines)):
            if re.match(r'^#{2,6}\s+', lines[i]):
                insert_pos = i
                break

        # 如果插入位置前面已有 ---，就移除它避免重复
        check_pos = insert_pos - 1
        while check_pos >= 0 and lines[check_pos].strip() == "":
            check_pos -= 1
        has_separator_before = check_pos >= 0 and lines[check_pos].strip() == "---"

        if has_separator_before:
            toc_block = f"\n## 目录\n\n{toc}\n\n---\n"
        else:
            toc_block = f"\n---\n\n## 目录\n\n{toc}\n\n---\n"
        lines.insert(insert_pos, toc_block)

        return "\n".join(lines)

    def _fix_image_paths(self, markdown: str, mapping: dict) -> str:
        """修复图片路径引用"""
        result = markdown

        # 去掉 pandoc 的 width/height 属性
        result = re.sub(
            r'\{width="[^"]*"\s*height="[^"]*"\}',
            '',
            result
        )

        # 只在 markdown 图片语法 ![...](path) 中替换路径
        def replace_image_path(m):
            alt = m.group(1)
            path = m.group(2)

            # 用映射表替换（优先匹配长路径）
            for old_path, new_path in sorted(mapping.items(), key=lambda x: -len(x[0])):
                if old_path in path:
                    path = path.replace(old_path, new_path)
                    break

            # 通用修复：media/media/xxx → images/xxx
            path = re.sub(
                r'media/media/(\w+\.\w+)',
                lambda mm: f"{self.image_dir}/{mm.group(1)}",
                path,
            )

            # 防止 images/images/ 双重路径
            while f"{self.image_dir}/{self.image_dir}/" in path:
                path = path.replace(f"{self.image_dir}/{self.image_dir}/", f"{self.image_dir}/")

            # 去掉 images/ 之前的多余路径前缀（如 output/xxx/.work/images/xxx → images/xxx）
            img_dir_pos = path.find(f"{self.image_dir}/")
            if img_dir_pos > 0:
                path = path[img_dir_pos:]

            return f"![{alt}]({path})"

        result = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_image_path, result)

        return result

    def _find_content_start(self, raw_md: str) -> int:
        """找到正文开始位置（跳过目录区域）"""
        # 寻找第一个真正的标题（不是目录中的链接）
        patterns = [
            r'\n# .+\{#',     # pandoc 生成的带锚点标题
            r'\n# \d+',        # 数字编号标题
            r'\n# 引言',       # 常见的中文开头
            r'\n# Introduction',
        ]

        for pattern in patterns:
            match = re.search(pattern, raw_md)
            if match:
                return match.start()

        # fallback：跳过前 20% 或找到 "---" 分隔
        return 0

    def _clean_output(self, markdown: str) -> str:
        """清理 AI 输出"""
        # 去掉 AI 可能包裹的外层 ```markdown ``` 标记
        markdown = re.sub(r'^```markdown\s*\n', '', markdown)
        markdown = re.sub(r'\n```\s*$', '', markdown)

        # 合并被分片截断的相邻 JSON 代码块
        markdown = self._merge_broken_json_blocks(markdown)

        # 去掉连续多个空行
        markdown = re.sub(r'\n{4,}', '\n\n\n', markdown)

        return markdown.strip() + "\n"

    def _merge_broken_json_blocks(self, markdown: str) -> str:
        """合并被分片截断导致分裂的相邻 JSON 代码块"""
        # 匹配: ```json ... ``` 紧接着 ```json ... ```（中间只有空行）
        # 将它们合并为一个代码块
        pattern = r'```\s*\n\s*\n*```json\s*\n'
        while re.search(pattern, markdown):
            markdown = re.sub(pattern, '\n', markdown)
        return markdown
