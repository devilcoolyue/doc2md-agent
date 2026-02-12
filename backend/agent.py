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

        # ========== 第 2 步：AI 分析结构 ==========
        logger.info("=" * 50)
        logger.info("🔍 第 2 步：AI 分析文档结构")
        logger.info("=" * 50)
        self._report_progress(progress_callback, "analyze", 0, 1, "结构分析中：准备调用大模型")

        # 取前 3000 字符给 AI 分析（通常包含标题和目录）
        analyze_content = raw_md[:3000]
        structure = self._analyze_structure(analyze_content)
        logger.info(f"文档类型: {structure.get('doc_type', 'unknown')}")
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

        chunks = split_content(content_body, self.chunk_size)
        converted_chunks = []
        planned_llm_calls = 1 + len(chunks) + (1 if self.generate_toc else 0)
        self._emit_event(
            {
                "type": "llm_plan",
                "planned_calls": planned_llm_calls,
                "chunk_count": len(chunks),
                "message": f"正文已分为 {len(chunks)} 个片段，预计调用大模型 {planned_llm_calls} 次",
            }
        )

        for i, chunk in enumerate(chunks):
            logger.info(f"正在转换第 {i+1}/{len(chunks)} 个片段 ({len(chunk)} 字符)...")
            self._report_progress(
                progress_callback,
                "convert",
                i,
                len(chunks),
                f"AI 转换中：准备处理第 {i+1}/{len(chunks)} 个分片（{len(chunk)} 字符）",
            )
            converted = self._convert_chunk(
                chunk=chunk,
                structure=structure,
                chunk_index=i + 1,
                total_chunks=len(chunks),
            )
            converted_chunks.append(converted)
            self._report_progress(
                progress_callback,
                "convert",
                i + 1,
                len(chunks),
                f"AI 转换中：已完成第 {i+1}/{len(chunks)} 个分片",
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
            toc = self._generate_toc(full_md)
            # 在标题后插入目录
            full_md = self._insert_toc(full_md, toc)
            self._report_progress(progress_callback, "toc", 1, 1, "后处理中：目录已插入文档")

        # 清理 AI 输出中可能残留的 markdown 代码块标记
        full_md = self._clean_output(full_md)

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

    def _convert_chunk(self, chunk: str, structure: dict, chunk_index: int, total_chunks: int) -> str:
        """调用 AI 转换一个内容片段"""
        prompt = CONVERT_USER.format(
            structure=json.dumps(structure, ensure_ascii=False, indent=2),
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            content=chunk,
        )

        response = self.llm.chat(
            CONVERT_SYSTEM,
            prompt,
            context={
                "operation": "convert_chunk",
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
            },
        )
        return response

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
