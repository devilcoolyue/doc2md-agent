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


class TaskStoppedError(RuntimeError):
    """任务被用户停止。"""


class Doc2MDAgent:
    """文档转 Markdown 智能体"""

    def __init__(
        self,
        config: dict,
        event_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        stop_checker: Optional[Callable[[], bool]] = None,
    ):
        self.config = config
        self.event_callback = event_callback
        self.stop_checker = stop_checker
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
        self.allow_partial_on_chunk_failure = self.conv_config.get("allow_partial_on_chunk_failure", True)
        self.allow_partial_on_validation_failure = self.conv_config.get("allow_partial_on_validation_failure", True)
        self.min_content_token_coverage = float(self.conv_config.get("min_content_token_coverage", 0.82))
        self.min_content_char_ratio = float(self.conv_config.get("min_content_char_ratio", 0.62))
        self.content_guard_min_tokens = int(self.conv_config.get("content_guard_min_tokens", 20))

    def _emit_event(self, payload: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(payload)

    def _emit_logic_event(self, message: str, event_type: str = "pipeline_detail", **details: Any) -> None:
        payload: dict[str, Any] = {
            "type": event_type,
            "message": message,
        }
        for key, value in details.items():
            if value is not None:
                payload[key] = value
        self._emit_event(payload)

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

    def _check_stop(self) -> None:
        if self.stop_checker and self.stop_checker():
            self._emit_event(
                {
                    "type": "task_stop_detected",
                    "message": "检测到停止请求，正在终止转换流程",
                }
            )
            raise TaskStoppedError("任务已停止")

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
        self._check_stop()
        self._emit_event(
            {
                "type": "pipeline_started",
                "message": f"开始处理文档：{input_path.name}",
            }
        )
        self._emit_logic_event(
            (
                f"转换参数：chunk_size={self.chunk_size}, chunk_strategy={self.chunk_strategy}, "
                f"strict_mode={self.strict_mode}, max_chunk_retries={self.max_chunk_retries}, "
                f"generate_toc={self.generate_toc}, deterministic_toc={self.deterministic_toc}, "
                f"allow_partial_on_chunk_failure={self.allow_partial_on_chunk_failure}, "
                f"allow_partial_on_validation_failure={self.allow_partial_on_validation_failure}, "
                f"min_content_token_coverage={self.min_content_token_coverage}, "
                f"min_content_char_ratio={self.min_content_char_ratio}"
            ),
            event_type="pipeline_config",
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
        self._check_stop()
        raw_md, images = preprocessor.extract()
        self._check_stop()
        self._report_progress(
            progress_callback,
            "preprocess",
            2,
            4,
            f"预处理中：提取完成，正文约 {len(raw_md)} 字符",
        )
        self._emit_logic_event(
            f"pandoc 提取完成：正文 {len(raw_md)} 字符，行数 {raw_md.count(chr(10)) + 1}，图片 {len(images)} 张",
            event_type="preprocess_detail",
            raw_chars=len(raw_md),
            image_count=len(images),
        )

        # 预处理：将 pandoc 单列表格（含 JSON 等）转为代码块
        json_blocks_before_fix = len(self._extract_json_blocks(raw_md))
        raw_md = fix_pandoc_table_codeblocks(raw_md)
        json_blocks_after_fix = len(self._extract_json_blocks(raw_md))
        logger.info("已完成 pandoc 表格代码块修复")
        self._report_progress(progress_callback, "preprocess", 3, 4, "预处理中：修复表格中的代码块")
        self._emit_logic_event(
            (
                "表格代码块修复完成："
                f"json 代码块 {json_blocks_before_fix} -> {json_blocks_after_fix}"
            ),
            event_type="preprocess_detail",
            json_blocks_before=json_blocks_before_fix,
            json_blocks_after=json_blocks_after_fix,
        )

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
        self._emit_logic_event(
            f"图片路径映射完成：共 {len(image_mapping)} 条映射规则",
            event_type="preprocess_detail",
            mapping_count=len(image_mapping),
        )

        # ========== 第 2 步：结构分析（规则优先） ==========
        logger.info("=" * 50)
        logger.info("🔍 第 2 步：分析文档结构")
        logger.info("=" * 50)
        self._report_progress(progress_callback, "analyze", 0, 1, "结构分析中：规则提取目录与章节")

        expected_headings = self._extract_expected_headings_from_toc(raw_md)
        structure = self._build_rule_based_structure(raw_md, expected_headings)
        self._check_stop()

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
        self._emit_logic_event(
            (
                f"结构分析结果：doc_type={structure.get('doc_type', 'unknown')}，"
                f"toc_headings={len(expected_headings)}，"
                f"heading_mapping={len(structure.get('heading_mapping', {}))} 项"
            ),
            event_type="analyze_detail",
            doc_type=structure.get("doc_type", "unknown"),
            toc_heading_count=len(expected_headings),
            heading_mapping_count=len(structure.get("heading_mapping", {})),
        )
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
        self._check_stop()

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
        chunk_fallback_count = 0
        partial_preview_file = output_dir / ".partial_preview.md"
        partial_preview_file.write_text("", encoding="utf-8")
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
        continuation_chunks = sum(1 for job in chunk_jobs if job.get("continuation_mode"))
        heading_chunks = sum(1 for job in chunk_jobs if job.get("chunk_has_heading"))
        self._emit_logic_event(
            (
                f"分片规划：总分片 {len(chunk_jobs)}，"
                f"含标题分片 {heading_chunks}，续片 {continuation_chunks}"
            ),
            event_type="chunk_plan",
            chunk_count=len(chunk_jobs),
            heading_chunks=heading_chunks,
            continuation_chunks=continuation_chunks,
        )

        for i, job in enumerate(chunk_jobs):
            self._check_stop()
            chunk = job["content"]
            section_label = self._resolve_section_label(
                section_id=job["section_id"],
                section_heading=job.get("section_heading", ""),
            )
            logger.info(
                "正在转换第 %s/%s 个片段（section=%s, section_label=%s, continuation=%s, %s 字符）",
                i + 1,
                len(chunk_jobs),
                job["section_id"],
                section_label,
                job["continuation_mode"],
                len(chunk),
            )
            self._emit_logic_event(
                (
                    f"开始处理分片 {i + 1}/{len(chunk_jobs)}：章节={section_label}，section={job['section_id']}，"
                    f"continuation={job['continuation_mode']}，allowed_headings={job['allowed_headings'] or '(none)'}，"
                    f"chars={len(chunk)}"
                ),
                event_type="chunk_started",
                chunk_index=i + 1,
                total_chunks=len(chunk_jobs),
                section_id=job["section_id"],
                section_heading=job.get("section_heading", ""),
                section_label=section_label,
                continuation_mode=job["continuation_mode"],
                chunk_chars=len(chunk),
            )
            self._report_progress(
                progress_callback,
                "convert",
                i,
                len(chunk_jobs),
                f"AI 转换中：准备处理第 {i+1}/{len(chunk_jobs)} 个分片（{section_label}，{len(chunk)} 字符）",
            )
            converted, convert_meta = self._convert_chunk_with_retry(
                chunk=chunk,
                structure=structure,
                chunk_index=i + 1,
                total_chunks=len(chunk_jobs),
                section_id=job["section_id"],
                section_heading=job["section_heading"],
                section_label=section_label,
                allowed_headings=job["allowed_headings"],
                continuation_mode=job["continuation_mode"],
                chunk_has_heading=job["chunk_has_heading"],
                previous_heading=job["previous_heading"],
                next_heading=job["next_heading"],
            )
            converted_chunks.append(converted)
            partial_preview_md = self._build_partial_preview_markdown(converted_chunks)
            partial_preview_file.write_text(partial_preview_md, encoding="utf-8")
            if convert_meta.get("fallback_used"):
                chunk_fallback_count += 1
            self._emit_logic_event(
                (
                    f"分片 {i + 1}/{len(chunk_jobs)} 完成：章节={section_label}，attempts={convert_meta.get('attempts_used', 1)}，"
                    f"json_source={convert_meta.get('source_json_blocks', 0)}，"
                    f"json_repaired={convert_meta.get('repaired_json_blocks', 0)}，"
                    f"json_fallback={convert_meta.get('fallback_json_blocks', 0)}，"
                    f"chunk_fallback={bool(convert_meta.get('fallback_used', False))}"
                ),
                event_type="chunk_completed",
                chunk_index=i + 1,
                total_chunks=len(chunk_jobs),
                section_id=job["section_id"],
                section_heading=job.get("section_heading", ""),
                section_label=section_label,
                attempts_used=convert_meta.get("attempts_used", 1),
                source_json_blocks=convert_meta.get("source_json_blocks", 0),
                repaired_json_blocks=convert_meta.get("repaired_json_blocks", 0),
                fallback_json_blocks=convert_meta.get("fallback_json_blocks", 0),
                fallback_used=bool(convert_meta.get("fallback_used", False)),
                fallback_reason=convert_meta.get("fallback_reason"),
            )
            self._report_progress(
                progress_callback,
                "convert",
                i + 1,
                len(chunk_jobs),
                f"AI 转换中：已完成第 {i+1}/{len(chunk_jobs)} 个分片（{section_label}）",
            )

        self._check_stop()
        if chunk_fallback_count > 0:
            self._emit_logic_event(
                (
                    f"分片转换兜底：共有 {chunk_fallback_count}/{len(chunk_jobs)} 个分片在重试耗尽后"
                    "自动回退为保真内容并继续流程"
                ),
                event_type="chunk_fallback_summary",
                fallback_chunks=chunk_fallback_count,
                total_chunks=len(chunk_jobs),
            )

        # ========== 第 4 步：后处理 ==========
        logger.info("=" * 50)
        logger.info("📦 第 4 步：后处理和组装")
        logger.info("=" * 50)
        self._check_stop()

        # 合并所有片段
        full_md = "\n\n".join(converted_chunks)
        self._emit_logic_event(
            f"后处理：已合并 {len(converted_chunks)} 个分片，正文长度 {len(full_md)} 字符",
            event_type="postprocess_detail",
            merged_chunks=len(converted_chunks),
            merged_chars=len(full_md),
        )
        full_md = self._postprocess_markdown(
            markdown=full_md,
            expected_headings=expected_headings,
            image_mapping=image_mapping,
            progress_callback=progress_callback,
            allow_ai_toc=True,
        )
        self._emit_logic_event(
            f"后处理：格式清理完成，当前长度 {len(full_md)} 字符",
            event_type="postprocess_detail",
            cleaned_chars=len(full_md),
        )
        partial_preview_file.write_text(full_md, encoding="utf-8")

        validation_warning = ""
        if self.strict_mode:
            self._check_stop()
            self._emit_logic_event("执行严格校验：标题完整性、错误码集合、JSON 可解析性", event_type="validation_started")
            try:
                self._validate_final_output(raw_md=raw_md, final_md=full_md, expected_headings=expected_headings)
                self._emit_logic_event("严格校验通过", event_type="validation_passed")
            except Exception as exc:
                if not self.allow_partial_on_validation_failure:
                    raise
                validation_warning = str(exc)
                logger.warning("严格校验未通过，按兜底策略继续输出: %s", validation_warning)
                self._emit_logic_event(
                    f"严格校验未通过，按兜底策略继续输出：{validation_warning}",
                    event_type="validation_warning",
                    reason=validation_warning,
                )

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
        usage["chunk_fallbacks"] = chunk_fallback_count
        if validation_warning:
            usage["validation_warning"] = validation_warning
        usage["degraded"] = bool(chunk_fallback_count or validation_warning)
        self._report_progress(progress_callback, "done", 1, 1, "转换完成")
        completion_message = f"转换完成，输出文件：{output_file.name}"
        if chunk_fallback_count:
            completion_message += f"（含分片兜底 {chunk_fallback_count} 个）"
        if validation_warning:
            completion_message += "（严格校验降级放行）"
        self._emit_event(
            {
                "type": "pipeline_completed",
                "output_file": str(output_file),
                "llm_calls": usage.get("llm_calls", 0),
                "message": completion_message,
            }
        )
        self._emit_logic_event(
            (
                "用量汇总："
                f"llm_calls={usage.get('llm_calls', 0)}, "
                f"total_tokens={usage.get('total_tokens', 0)}, "
                f"total_cost={usage.get('currency', '')}{usage.get('total_cost', 0):.6f}"
            ),
            event_type="usage_summary",
        )
        return str(output_file), usage

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------

    def _build_partial_preview_markdown(self, converted_chunks: list[str]) -> str:
        """
        基于当前已完成分片生成“可读的实时预览”内容。
        后处理失败时降级为原始拼接，避免中断主流程。
        """
        raw_partial = "\n\n".join(converted_chunks).strip()
        if not raw_partial:
            return ""
        try:
            return self.postprocess_partial_markdown(raw_partial)
        except Exception as exc:
            logger.warning("实时预览后处理失败，降级为原始拼接内容: %s", exc)
            self._emit_logic_event(
                f"实时预览后处理失败，已降级为原始拼接内容：{exc}",
                event_type="partial_preview_warning",
                reason=str(exc),
            )
            return raw_partial + "\n"

    def postprocess_partial_markdown(self, markdown: str) -> str:
        """
        对停止任务时的阶段性内容做后处理，尽量输出可阅读的 partial Markdown。
        partial 场景不再调用 AI 目录生成，避免停止状态下再次触发模型调用。
        """
        return self._postprocess_markdown(
            markdown=markdown,
            expected_headings=None,
            image_mapping=None,
            progress_callback=None,
            allow_ai_toc=False,
        )

    def _postprocess_markdown(
        self,
        markdown: str,
        expected_headings: Optional[list[str]] = None,
        image_mapping: Optional[dict[str, str]] = None,
        progress_callback: Optional[Callable[..., None]] = None,
        allow_ai_toc: bool = True,
    ) -> str:
        full_md = markdown

        if image_mapping:
            full_md = self._fix_image_paths(full_md, image_mapping)
            self._emit_logic_event(
                f"后处理：图片路径修复完成，映射规则 {len(image_mapping)} 条",
                event_type="postprocess_detail",
                mapping_count=len(image_mapping),
            )

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
        full_md = full_md.replace('├─', '└─')

        # 将裸编号标题（如 2.1 xxx）提升为 Markdown 标题
        full_md = self._promote_plain_numbered_heading_lines(full_md, expected_headings=expected_headings)

        # 按编号规则统一标题层级（例如 2.1.3 与 2.1.5 保持同级）
        full_md = self._normalize_numbered_heading_levels(full_md)

        # 将“对象字段说明”子表并回主表，并用 └─ 展示层级
        full_md = self._merge_hierarchical_field_tables(full_md)

        # 处理少量残留的 pandoc 网格表行（并入上一张表）
        full_md = self._flatten_residual_grid_table_rows(full_md)

        # 将残留的 grid table 统一转为标准 Markdown 表格
        full_md = self._convert_residual_grid_tables(full_md)

        # 将 pandoc simple table（---- ---- 形态）转为 Markdown 表格
        full_md = self._convert_pandoc_simple_tables(full_md)

        # 将“名称 类型 必填 说明”这类纯文本伪表格转为 Markdown 表格
        full_md = self._convert_plain_text_tabular_blocks(full_md)
        # 表格换行说明并回上一行，避免出现仅保留“说明列”的空行
        full_md = self._merge_wrapped_description_rows_in_tables(full_md)
        # 将“名称|类型|必填”三列表扩展为四列表，并拆分必填列中挤压的说明文字
        full_md = self._expand_required_only_tables_with_description(full_md)

        # 同一张表内根据缩进/前缀识别子层级，并统一为 └─
        full_md = self._normalize_indented_hierarchy_in_tables(full_md)

        # 结合紧邻的 JSON 示例，修正参数表中的层级深度缩进
        full_md = self._normalize_hierarchy_from_nearby_json_examples(full_md)
        # 若缺少可用 JSON 示例，回退为基于对象行的层级推断
        full_md = self._normalize_hierarchy_with_object_row_fallback(full_md)

        # 接口文档常见标签统一为加粗前缀（如 请求方式：/接口描述：）
        full_md = self._normalize_api_label_lines(full_md)

        # 补齐未包裹代码块的 curl 命令
        full_md = self._wrap_curl_commands_in_code_blocks(full_md)

        # 尝试将可解析 JSON 代码块统一规范化并美化
        full_md = self._normalize_json_fenced_blocks(full_md)

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
            if allow_ai_toc and not self.deterministic_toc:
                self._check_stop()
                toc = self._generate_toc(full_md)
            else:
                toc = self._simple_toc(full_md)
            full_md = self._insert_toc(full_md, toc)
            self._report_progress(progress_callback, "toc", 1, 1, "后处理中：目录已插入文档")

        # 清理 AI 输出中可能残留的 markdown 代码块标记
        return self._clean_output(full_md)

    def _resolve_section_label(self, section_id: str, section_heading: str) -> str:
        heading = (section_heading or "").strip()
        if heading:
            return heading
        sid = (section_id or "").strip()
        if sid.startswith("preamble-"):
            return f"前置内容({sid})"
        if sid:
            return sid
        return "未识别章节"

    def _normalize_heading_text(self, heading: str) -> str:
        """标题比较归一化：忽略空白差异。"""
        return re.sub(r'\s+', '', heading.strip())

    def _extract_section_id(self, numbered_heading: str) -> str:
        match = re.match(r'^(\d+(?:\.\d+)*)\s+', numbered_heading.strip())
        return match.group(1) if match else ""

    def _strip_heading_number_prefix(self, heading: str) -> str:
        return re.sub(r'^\d+(?:\.\d+)*\s+', '', heading.strip())

    def _heading_level_from_section_id(self, section_id: str) -> int:
        if not section_id:
            return 2
        return min(6, len(section_id.split(".")) + 1)

    def _heading_level_from_numbered_heading(self, numbered_heading: str) -> int:
        section_id = self._extract_section_id(numbered_heading)
        return self._heading_level_from_section_id(section_id)

    def _extract_numbered_heading_candidate(self, line: str) -> tuple[str, int] | None:
        """
        从一行文本中提取“编号标题候选”。
        返回 `(标准化标题文本, 目标标题层级)`，如 `("2.1 接口格式", 3)`。
        """
        stripped = self._strip_heading_attrs(line.strip())
        if not stripped:
            return None
        if stripped.startswith(("|", "-", "*", "+", ">", "[", "![", "```")):
            return None
        if self._looks_like_table_delimiter_line(stripped):
            return None

        match = re.match(r'^(\d+(?:\.\d+)*)\s*([、.．\)）])?\s+(.+)$', stripped)
        if not match:
            return None

        section_id = match.group(1).strip()
        delimiter = (match.group(2) or "").strip()
        title_body = match.group(3).strip()
        if not title_body:
            return None

        # 排除“1. xxx / 2) xxx”这类有序列表项，避免误提升为章节标题
        if delimiter in {".", "．", ")", "）"} and "." not in section_id:
            return None

        # 错误码/流水号等长数字通常不是章节标题
        top_level = section_id.split(".")[0]
        if len(top_level) >= 3:
            return None
        if "." not in section_id:
            try:
                section_num = int(section_id)
                if section_num <= 0 or section_num > 30:
                    return None
            except ValueError:
                return None

        # 避免把明显的正文句子误判为标题
        if len(title_body) > 80 and title_body.endswith(("。", "；", ";")):
            return None

        heading_text = f"{section_id} {title_body}"
        return heading_text, self._heading_level_from_section_id(section_id)

    def _looks_like_table_delimiter_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if re.fullmatch(r'[+:=\-\| ]{5,}', stripped):
            return True
        if not (stripped.startswith("|") and stripped.endswith("|")):
            return False
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            return False
        return all(bool(re.fullmatch(r':?-{3,}:?', cell or '---')) for cell in cells)

    def _promote_plain_numbered_heading_lines(
        self,
        markdown: str,
        expected_headings: Optional[list[str]] = None,
    ) -> str:
        """
        将未带 `#` 的编号标题行提升为 Markdown 标题。
        示例：`2.1 接口格式` -> `### 2.1 接口格式`
        """
        lines = markdown.split("\n")
        promoted: list[str] = []
        in_code_block = False
        expected_norm_set = {
            self._normalize_heading_text(h)
            for h in (expected_headings or [])
            if h and h.strip()
        }

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                promoted.append(line)
                continue

            if in_code_block:
                promoted.append(line)
                continue

            if re.match(r'^\s*#{1,6}\s+', line):
                promoted.append(line)
                continue

            candidate = self._extract_numbered_heading_candidate(line)
            if not candidate:
                promoted.append(line)
                continue

            heading_text, level = candidate
            if expected_norm_set and self._normalize_heading_text(heading_text) not in expected_norm_set:
                promoted.append(line)
                continue

            prev_line = lines[idx - 1].strip() if idx > 0 else ""
            next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            context_ok = (
                not prev_line
                or not next_line
                or prev_line == "---"
                or next_line.startswith("#")
                or next_line.startswith("|")
                or next_line.startswith("```")
            )
            if not context_ok:
                promoted.append(line)
                continue

            promoted.append(f"{'#' * level} {heading_text}")

        return "\n".join(promoted)

    def _normalize_numbered_heading_levels(self, markdown: str) -> str:
        """
        统一编号标题层级：
        - 1 -> ##
        - 1.1 -> ###
        - 1.1.1 -> ####
        """
        lines = markdown.split("\n")
        in_code_block = False
        normalized: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                normalized.append(line)
                continue

            if in_code_block:
                normalized.append(line)
                continue

            match = re.match(r'^\s*#{1,6}\s+(.+)$', line)
            if not match:
                normalized.append(line)
                continue

            candidate = self._extract_numbered_heading_candidate(match.group(1))
            if not candidate:
                normalized.append(line)
                continue

            title, level = candidate
            normalized.append(f"{'#' * level} {title}")

        return "\n".join(normalized)

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

    def _extract_fenced_code_blocks(self, text: str) -> list[dict[str, Any]]:
        """提取 fenced code block（含语言标记和内容）。"""
        pattern = re.compile(r'```([^\n`]*)\s*\n(.*?)\n```', re.S)
        blocks: list[dict[str, Any]] = []
        for match in pattern.finditer(text):
            blocks.append(
                {
                    "lang": (match.group(1) or "").strip().lower(),
                    "content": match.group(2).strip(),
                    "start": match.start(),
                    "end": match.end(),
                }
            )
        return blocks

    def _extract_json_candidate_code_blocks(self, text: str) -> list[dict[str, Any]]:
        """
        提取“JSON 候选代码块”：
        - ```json
        - 无语言但内容以 { / [ 开头（用于 JSON 降级为普通代码块后的校验）
        """
        candidates = []
        for block in self._extract_fenced_code_blocks(text):
            content = str(block.get("content", "")).strip()
            if not content:
                continue
            lang = str(block.get("lang", "")).lower()
            if lang == "json":
                candidates.append(block)
                continue
            if not lang and content.startswith(("{", "[")):
                candidates.append(block)
        return candidates

    def _json_error_text(self, err: Exception) -> str:
        if isinstance(err, json.JSONDecodeError):
            return f"{err.msg} (line {err.lineno}, col {err.colno})"
        return str(err)

    def _fence_code_block(self, content: str, language: str = "") -> str:
        """根据内容自动选择围栏长度，避免与正文冲突。"""
        fence = "````" if "```" in content else "```"
        lang_part = language.strip()
        if lang_part:
            return f"{fence}{lang_part}\n{content}\n{fence}"
        return f"{fence}\n{content}\n{fence}"

    def _build_json_fallback_block(self, block_text: str, reason: str, source: str) -> str:
        safe_reason = (reason or "无法识别具体原因").replace("\n", " ").strip()
        if len(safe_reason) > 180:
            safe_reason = safe_reason[:177] + "..."
        notice = (
            "> 以下json格式可能有问题，请检查。\n"
            f"> 来源：{source}；自动修复失败原因：{safe_reason}\n\n"
        )
        return notice + self._fence_code_block(block_text.strip(), language="")

    def _strip_json_comments(self, text: str) -> str:
        s = re.sub(r'/\*[\s\S]*?\*/', '', text)
        s = re.sub(r'^\s*//.*$', '', s, flags=re.MULTILINE)
        return s

    def _quote_unquoted_json_keys(self, text: str) -> str:
        s = re.sub(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_.-]*)(\s*:)', r'\1"\2"\3', text)
        s = re.sub(r'^(\s*)([A-Za-z_][A-Za-z0-9_.-]*)(\s*:)', r'\1"\2"\3', s, flags=re.MULTILINE)
        return s

    def _replace_single_quoted_strings(self, text: str) -> str:
        pattern = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")
        return pattern.sub(lambda m: '"' + m.group(1).replace('"', '\\"') + '"', text)

    def _insert_missing_json_commas(self, text: str) -> str:
        s = text
        s = re.sub(
            r'("([^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\btrue\b|\bfalse\b|\bnull\b|[}\]])\s*(?="([^"\\]|\\.)*"\s*:)',
            r'\1, ',
            s,
        )
        s = re.sub(r'(\})\s*(\{)', r'\1,\n\2', s)
        s = re.sub(r'(\])\s*(\[)', r'\1,\n\2', s)
        return s

    def _balance_json_brackets(self, text: str) -> tuple[str, bool]:
        """补全缺失的括号/方括号，并删除多余闭合符。"""
        open_to_close = {"{": "}", "[": "]"}
        close_to_open = {"}": "{", "]": "["}
        stack: list[str] = []
        result: list[str] = []
        in_string = False
        escaped = False
        changed = False

        for ch in text:
            if in_string:
                result.append(ch)
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                result.append(ch)
                continue
            if ch in open_to_close:
                stack.append(ch)
                result.append(ch)
                continue
            if ch in close_to_open:
                if stack and stack[-1] == close_to_open[ch]:
                    stack.pop()
                    result.append(ch)
                else:
                    changed = True
                continue
            result.append(ch)

        if in_string:
            result.append('"')
            changed = True

        while stack:
            opener = stack.pop()
            result.append(open_to_close[opener])
            changed = True

        return "".join(result), changed

    def _strip_mailto_artifacts_in_json(self, text: str) -> str:
        """
        清理 docx/pandoc 造成的 mailto 污染：
        ["a@b.com"](mailto:a@b.com) -> "a@b.com"
        """
        pattern = re.compile(r'\[\s*([^\]]+?)\s*[，,]?\s*\]\(\s*mailto:[^)]+\)', flags=re.IGNORECASE)

        def repl(match: re.Match[str]) -> str:
            value = match.group(1).strip()
            value = value.strip('"').strip("'").rstrip("，,")
            return f'"{value}"'

        return pattern.sub(repl, text)

    def _remove_invalid_json_escapes(self, text: str) -> str:
        """
        去除 JSON 中非法转义（如 \\*、\\_），保留合法转义。
        """
        return re.sub(r'\\([^"\\/bfnrtu])', r'\1', text)

    def _collapse_double_escaped_quotes_in_strings(self, text: str) -> str:
        """
        将字符串中的 `\\\"` 压缩为 `\"`，避免把引号错误地双重转义。
        仅在 JSON 字符串内处理，避免影响结构字符。
        """
        result: list[str] = []
        in_string = False
        escaped = False
        i = 0

        while i < len(text):
            ch = text[i]

            if not in_string:
                result.append(ch)
                if ch == '"':
                    in_string = True
                    escaped = False
                i += 1
                continue

            if ch == '\\' and i + 2 < len(text) and text[i + 1] == '\\' and text[i + 2] == '"':
                result.append('\\')
                result.append('"')
                i += 3
                escaped = False
                continue

            result.append(ch)
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1

        return "".join(result)

    def _sanitize_json_like_text(self, text: str) -> str:
        """
        对 JSON-like 文本做轻量修复后用于解析：
        - 处理 NBSP/转义符
        - 去掉尾随逗号
        - 将带字母的裸值（如 1118xxxx5311）转为字符串
        """
        s = text.replace("\u00a0", " ").replace("\ufeff", "").strip()
        s = (
            s.replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
            .replace("：", ":")
            .replace("，", ",")
        )
        s = self._strip_mailto_artifacts_in_json(s)
        s = s.replace('\\[', '[')
        s = s.replace('\\]', ']')
        s = self._collapse_double_escaped_quotes_in_strings(s)
        s = self._remove_invalid_json_escapes(s)
        s = re.sub(r',\s*([}\]])', r'\1', s)

        def quote_masked_literals(m):
            prefix = m.group(1)
            value = m.group(2)
            suffix = m.group(3) if m.lastindex and m.lastindex >= 3 else ""
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
        s = re.sub(
            r'(:\s*)([A-Za-z0-9_./:+-]*[A-Za-z][A-Za-z0-9_./:+-]*)(\s*)(?=\n\s*["\']?[A-Za-z_][A-Za-z0-9_.-]*["\']?\s*:)',
            quote_masked_literals,
            s,
        )
        return s

    def _smart_fill_json_like_text(self, text: str) -> tuple[str, list[str]]:
        """尝试自动补全常见 JSON 语法缺陷。"""
        s = text
        actions: list[str] = []

        no_comments = self._strip_json_comments(s)
        if no_comments != s:
            actions.append("移除注释")
            s = no_comments

        quoted_keys = self._quote_unquoted_json_keys(s)
        if quoted_keys != s:
            actions.append("补全未加引号的 key")
            s = quoted_keys

        single_quoted = self._replace_single_quoted_strings(s)
        if single_quoted != s:
            actions.append("将单引号字符串改为双引号")
            s = single_quoted

        with_commas = self._insert_missing_json_commas(s)
        if with_commas != s:
            actions.append("补全疑似缺失的逗号")
            s = with_commas

        s = re.sub(r',\s*([}\]])', r'\1', s)

        balanced, changed = self._balance_json_brackets(s)
        if changed:
            actions.append("补全括号/方括号")
            s = balanced

        return s.strip(), actions

    def _normalize_json_block_with_diagnostics(self, block_text: str) -> dict[str, Any]:
        raw = block_text.strip()
        candidates: list[tuple[str, str, list[str]]] = []
        seen = set()

        def add_candidate(stage: str, candidate: str, actions: list[str]) -> None:
            normalized_candidate = candidate.strip()
            key = (stage, normalized_candidate)
            if key in seen:
                return
            seen.add(key)
            candidates.append((stage, normalized_candidate, actions))

        add_candidate("raw", raw, [])
        sanitized = self._sanitize_json_like_text(raw)
        add_candidate("sanitize", sanitized, ["基础清洗"])
        smart_filled, smart_actions = self._smart_fill_json_like_text(sanitized)
        add_candidate("smart_fill", smart_filled, smart_actions)

        last_error = "未知错误"
        for stage, candidate, actions in candidates:
            try:
                parsed = json.loads(candidate)
                return {
                    "ok": True,
                    "normalized": json.dumps(parsed, ensure_ascii=False, indent=2),
                    "strategy": stage,
                    "actions": actions,
                    "is_repaired": stage != "raw",
                    "error": "",
                }
            except Exception as exc:
                last_error = self._json_error_text(exc)

        return {
            "ok": False,
            "normalized": raw,
            "strategy": "fallback",
            "actions": smart_actions,
            "is_repaired": False,
            "error": last_error,
        }

    def _normalize_json_block(self, block_text: str) -> tuple[str, bool]:
        """返回 (规范化后的 JSON 字符串, 是否可解析)。"""
        diagnostic = self._normalize_json_block_with_diagnostics(block_text)
        return diagnostic["normalized"], bool(diagnostic["ok"])

    def _replace_output_json_blocks_with_source_and_report(
        self, source_chunk: str, converted_chunk: str
    ) -> tuple[str, dict[str, Any]]:
        """
        若源分片存在 JSON 代码块，则优先回填源内容：
        - 可修复/可解析：以 ```json 输出
        - 无法修复：降级为普通代码块，并加提示
        """
        source_blocks = self._extract_json_blocks(source_chunk)
        report: dict[str, Any] = {
            "source_json_blocks": len(source_blocks),
            "repaired_json_blocks": 0,
            "fallback_json_blocks": 0,
            "fallback_reasons": [],
        }
        if not source_blocks:
            return converted_chunk, report

        rendered_sources: list[str] = []
        for idx, block in enumerate(source_blocks, start=1):
            diagnostic = self._normalize_json_block_with_diagnostics(block)
            if diagnostic["ok"]:
                if diagnostic["is_repaired"]:
                    report["repaired_json_blocks"] += 1
                rendered_sources.append(self._fence_code_block(diagnostic["normalized"], language="json"))
            else:
                reason = diagnostic["error"] or "无法解析"
                report["fallback_json_blocks"] += 1
                report["fallback_reasons"].append(f"source#{idx}: {reason}")
                rendered_sources.append(self._build_json_fallback_block(block, reason, source="原文"))

        pattern = re.compile(r'```json\s*\n(.*?)\n```', re.S)
        matches = list(pattern.finditer(converted_chunk))
        if not matches:
            appended = "\n\n".join(rendered_sources)
            if not converted_chunk.strip():
                return appended, report
            return converted_chunk.rstrip() + "\n\n" + appended, report

        replace_count = min(len(matches), len(rendered_sources))
        parts: list[str] = []
        last_end = 0
        for idx, match in enumerate(matches):
            parts.append(converted_chunk[last_end:match.start()])
            if idx < replace_count:
                parts.append(rendered_sources[idx])
            else:
                parts.append(match.group(0))
            last_end = match.end()
        parts.append(converted_chunk[last_end:])
        if len(matches) < len(rendered_sources):
            missing = "\n\n".join(rendered_sources[len(matches):])
            parts.append("\n\n" + missing)
        return "".join(parts), report

    def _replace_output_json_blocks_with_source(self, source_chunk: str, converted_chunk: str) -> str:
        replaced, _ = self._replace_output_json_blocks_with_source_and_report(source_chunk, converted_chunk)
        return replaced

    def _sanitize_output_json_blocks_with_report(self, converted_chunk: str) -> tuple[str, dict[str, Any]]:
        """
        对输出中的 ```json 代码块做二次规范化：
        - 能解析则格式化
        - 仍无法解析则降级为普通代码块并提示
        """
        pattern = re.compile(r'```json\s*\n(.*?)\n```', re.S)
        matches = list(pattern.finditer(converted_chunk))
        report = {
            "output_json_blocks": len(matches),
            "output_json_repaired": 0,
            "output_json_fallback": 0,
            "fallback_reasons": [],
        }
        if not matches:
            return converted_chunk, report

        parts: list[str] = []
        last_end = 0
        for idx, match in enumerate(matches, start=1):
            parts.append(converted_chunk[last_end:match.start()])
            diagnostic = self._normalize_json_block_with_diagnostics(match.group(1))
            if diagnostic["ok"]:
                if diagnostic["is_repaired"]:
                    report["output_json_repaired"] += 1
                parts.append(self._fence_code_block(diagnostic["normalized"], language="json"))
            else:
                reason = diagnostic["error"] or "无法解析"
                report["output_json_fallback"] += 1
                report["fallback_reasons"].append(f"output#{idx}: {reason}")
                parts.append(self._build_json_fallback_block(match.group(1), reason, source="模型输出"))
            last_end = match.end()

        parts.append(converted_chunk[last_end:])
        return "".join(parts), report

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

    def _strip_heading_lines_outside_code_blocks(self, text: str) -> tuple[str, list[str]]:
        """
        删除代码块外的 Markdown 标题行。
        用于 continuation 续片自动修复模型重复输出的章节标题。
        """
        sanitized_lines: list[str] = []
        removed_headings: list[str] = []
        in_code_block = False

        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                sanitized_lines.append(line)
                continue

            if not in_code_block and re.match(r'^\s*#{1,6}\s+.+$', line):
                removed_headings.append(stripped)
                continue

            sanitized_lines.append(line)

        sanitized = "\n".join(sanitized_lines)
        sanitized = re.sub(r'\n{3,}', '\n\n', sanitized).strip()
        return sanitized, removed_headings

    def _ensure_allowed_heading_in_chunk(
        self,
        converted_chunk: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        chunk_has_heading: bool,
    ) -> tuple[str, bool]:
        """
        对非续片强制编号标题：
        - 若标题文本匹配（含“去编号”匹配），替换为目标编号+目标层级
        - 若缺失标题且源分片含标题，则补一行目标标题
        """
        if continuation_mode or not allowed_headings:
            return converted_chunk, False

        target_heading = (allowed_headings[0] or "").strip()
        if not target_heading:
            return converted_chunk, False

        target_norm = self._normalize_heading_text(target_heading)
        target_plain_norm = self._normalize_heading_text(self._strip_heading_number_prefix(target_heading))
        target_level = self._heading_level_from_numbered_heading(target_heading)
        target_line = f"{'#' * target_level} {target_heading}"

        lines = converted_chunk.split("\n")
        in_code_block = False
        heading_indices: list[int] = []

        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block:
                continue

            heading_match = re.match(r'^\s*#{1,6}\s+(.+)$', line)
            if not heading_match:
                continue

            heading_indices.append(idx)
            title = self._strip_heading_attrs(heading_match.group(1).strip())
            title_norm = self._normalize_heading_text(title)
            title_plain_norm = self._normalize_heading_text(self._strip_heading_number_prefix(title))

            if title_norm == target_norm or title_plain_norm == target_plain_norm:
                if line.strip() != target_line:
                    lines[idx] = target_line
                    return "\n".join(lines), True
                return converted_chunk, False

        if heading_indices:
            first_idx = heading_indices[0]
            if lines[first_idx].strip() != target_line:
                lines[first_idx] = target_line
                return "\n".join(lines), True
            return converted_chunk, False

        if chunk_has_heading:
            content = converted_chunk.strip()
            if not content:
                return target_line, True
            return f"{target_line}\n\n{content}", True

        return converted_chunk, False

    def _normalize_text_for_content_guard(self, line: str) -> str:
        """用于主体内容保真校验的轻量文本归一化。"""
        s = line.strip()
        if not s:
            return ""

        s = re.sub(r'^\s*#{1,6}\s+', '', s)
        s = re.sub(r'^\s*[-*+]\s+', '', s)
        s = re.sub(r'^\s*\d+\.\s+', '', s)
        s = re.sub(r'\{#[^}]*\}', '', s)
        s = s.replace("```json", "```").replace("```bash", "```")
        s = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', r'\1 \2', s)
        s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', s)
        s = s.replace("|", " ")
        s = re.sub(r'[`*_~]', '', s)
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    def _extract_content_tokens_for_guard(self, text: str) -> list[str]:
        tokens: list[str] = []
        for line in text.split("\n"):
            normalized = self._normalize_text_for_content_guard(line)
            if not normalized:
                continue
            if self._looks_like_table_delimiter_line(normalized):
                continue
            for token in re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_.:/+\-]{1,}|[0-9]{2,}', normalized):
                tokens.append(token.lower())
        return tokens

    def _check_content_preservation(self, source_chunk: str, converted_chunk: str) -> tuple[bool, str]:
        """
        主体内容保真校验：
        - token 覆盖率过低：判定为删减风险
        - 归一化字符长度比例过低：判定为删减风险
        """
        source_tokens = self._extract_content_tokens_for_guard(source_chunk)
        if not source_tokens:
            return True, ""

        output_tokens = set(self._extract_content_tokens_for_guard(converted_chunk))
        unique_source_tokens = list(dict.fromkeys(source_tokens))
        matched_count = sum(1 for token in unique_source_tokens if token in output_tokens)
        token_coverage = matched_count / max(1, len(unique_source_tokens))

        source_plain = " ".join(
            normalized
            for normalized in (self._normalize_text_for_content_guard(line) for line in source_chunk.split("\n"))
            if normalized
        )
        output_plain = " ".join(
            normalized
            for normalized in (self._normalize_text_for_content_guard(line) for line in converted_chunk.split("\n"))
            if normalized
        )
        char_ratio = len(output_plain) / max(1, len(source_plain))

        if len(unique_source_tokens) >= self.content_guard_min_tokens and token_coverage < self.min_content_token_coverage:
            missing = [token for token in unique_source_tokens if token not in output_tokens][:6]
            return (
                False,
                (
                    "主体内容疑似被删减：token 覆盖率过低 "
                    f"({token_coverage:.2%} < {self.min_content_token_coverage:.2%})，"
                    f"缺失示例: {missing}"
                ),
            )

        if len(source_plain) >= 120 and char_ratio < self.min_content_char_ratio:
            return (
                False,
                (
                    "主体内容疑似被删减：有效字符占比过低 "
                    f"({char_ratio:.2%} < {self.min_content_char_ratio:.2%})"
                ),
            )

        return True, ""

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

            for line in output_no_code.split("\n"):
                heading_match = re.match(r'^\s*(#{1,6})\s+(\d+(?:\.\d+)*\s+.+)$', line)
                if not heading_match:
                    continue
                level = len(heading_match.group(1))
                heading_text = self._strip_heading_attrs(heading_match.group(2).strip())
                expected_level = self._heading_level_from_numbered_heading(heading_text)
                if level != expected_level:
                    return False, (
                        f"编号标题层级不符合规则: {heading_text}（expected_h={expected_level}, actual_h={level}）"
                    )

        if not continuation_mode and allowed_norm and not output_numbered_norm:
            return False, "缺少必须的编号标题"

        source_json_blocks = self._extract_json_blocks(source_chunk)
        output_json_blocks = self._extract_json_blocks(output)
        if not source_json_blocks and output_json_blocks:
            return False, "源片段不含 JSON 代码块，禁止新增 JSON 代码块"
        if source_json_blocks:
            output_json_candidates = self._extract_json_candidate_code_blocks(output)
            if len(output_json_candidates) < len(source_json_blocks):
                return False, (
                    "JSON 代码块数量不一致（source="
                    f"{len(source_json_blocks)}, output_candidates={len(output_json_candidates)}）"
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

        preserved, preserve_reason = self._check_content_preservation(source_chunk, output)
        if not preserved:
            return False, preserve_reason

        return True, ""

    def _convert_chunk_with_retry(
        self,
        chunk: str,
        structure: dict,
        chunk_index: int,
        total_chunks: int,
        section_id: str,
        section_heading: str,
        section_label: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        chunk_has_heading: bool,
        previous_heading: str,
        next_heading: str,
    ) -> tuple[str, dict[str, Any]]:
        """分片转换 + 严格校验重试。"""
        last_error = ""
        for attempt in range(self.max_chunk_retries + 1):
            self._check_stop()
            attempt_no = attempt + 1
            self._emit_logic_event(
                (
                    f"分片 {chunk_index}/{total_chunks} 第 {attempt_no}/{self.max_chunk_retries + 1} 次尝试："
                    f"章节={section_label}，section={section_id}, continuation={continuation_mode}, "
                    f"allowed_headings={allowed_headings or '(none)'}"
                ),
                event_type="chunk_attempt_started",
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                attempt=attempt_no,
                max_attempts=self.max_chunk_retries + 1,
                section_id=section_id,
                section_heading=section_heading or None,
                section_label=section_label,
            )
            converted, meta = self._convert_chunk(
                chunk=chunk,
                structure=structure,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                section_id=section_id,
                section_heading=section_heading,
                section_label=section_label,
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

            converted, source_json_report = self._replace_output_json_blocks_with_source_and_report(chunk, converted)
            converted, output_json_report = self._sanitize_output_json_blocks_with_report(converted)

            fallback_reasons = source_json_report.get("fallback_reasons", []) + output_json_report.get("fallback_reasons", [])
            merged_meta = {
                "attempts_used": attempt_no,
                "source_json_blocks": source_json_report.get("source_json_blocks", 0),
                "repaired_json_blocks": (
                    source_json_report.get("repaired_json_blocks", 0)
                    + output_json_report.get("output_json_repaired", 0)
                ),
                "fallback_json_blocks": (
                    source_json_report.get("fallback_json_blocks", 0)
                    + output_json_report.get("output_json_fallback", 0)
                ),
                "fallback_reasons": fallback_reasons,
            }

            if continuation_mode:
                converted, removed_headings = self._strip_heading_lines_outside_code_blocks(converted)
                if removed_headings:
                    merged_meta["removed_heading_lines"] = len(removed_headings)
                    self._emit_logic_event(
                        (
                            f"分片 {chunk_index}/{total_chunks} 自动修复：续片移除了 {len(removed_headings)} 行标题"
                        ),
                        event_type="continuation_heading_stripped",
                        chunk_index=chunk_index,
                        total_chunks=total_chunks,
                        attempt=attempt_no,
                        section_id=section_id,
                        section_heading=section_heading or None,
                        section_label=section_label,
                        removed_heading_lines=len(removed_headings),
                        removed_headings=removed_headings[:3],
                    )

            converted, heading_fixed = self._ensure_allowed_heading_in_chunk(
                converted_chunk=converted,
                allowed_headings=allowed_headings,
                continuation_mode=continuation_mode,
                chunk_has_heading=chunk_has_heading,
            )
            if heading_fixed:
                merged_meta["normalized_heading"] = True
                self._emit_logic_event(
                    (
                        f"分片 {chunk_index}/{total_chunks} 自动修复：已规范编号标题与层级"
                    ),
                    event_type="heading_normalized",
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    attempt=attempt_no,
                    section_id=section_id,
                    section_heading=section_heading or None,
                    section_label=section_label,
                )

            if merged_meta["source_json_blocks"] or output_json_report.get("output_json_blocks", 0):
                self._emit_logic_event(
                    (
                        f"分片 {chunk_index}/{total_chunks} JSON 处理：章节={section_label}，source={merged_meta['source_json_blocks']}，"
                        f"repaired={merged_meta['repaired_json_blocks']}，"
                        f"fallback={merged_meta['fallback_json_blocks']}"
                    ),
                    event_type="json_block_processed",
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    attempt=attempt_no,
                    section_id=section_id,
                    section_heading=section_heading or None,
                    section_label=section_label,
                    source_json_blocks=merged_meta["source_json_blocks"],
                    repaired_json_blocks=merged_meta["repaired_json_blocks"],
                    fallback_json_blocks=merged_meta["fallback_json_blocks"],
                    fallback_reasons=fallback_reasons[:5] if fallback_reasons else None,
                )

            valid, reason = self._validate_chunk_output(
                source_chunk=chunk,
                converted_chunk=converted,
                allowed_headings=allowed_headings,
                continuation_mode=continuation_mode,
                llm_meta=meta,
            )
            if valid:
                self._emit_logic_event(
                    f"分片 {chunk_index}/{total_chunks} 校验通过（章节={section_label}，attempt={attempt_no}）",
                    event_type="chunk_validation_passed",
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    attempt=attempt_no,
                    section_id=section_id,
                    section_heading=section_heading or None,
                    section_label=section_label,
                )
                return converted, merged_meta

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
            self._emit_logic_event(
                (
                    f"分片 {chunk_index}/{total_chunks} 校验失败（attempt={attempt_no}/{self.max_chunk_retries + 1}）："
                    f"章节={section_label}，{reason}"
                ),
                event_type="chunk_validation_failed",
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                attempt=attempt_no,
                max_attempts=self.max_chunk_retries + 1,
                section_id=section_id,
                section_heading=section_heading or None,
                section_label=section_label,
                reason=reason,
            )

        if not self.allow_partial_on_chunk_failure:
            raise RuntimeError(
                "分片转换失败：第 "
                f"{chunk_index}/{total_chunks} 片段（章节={section_label}）在 "
                f"{self.max_chunk_retries + 1} 次尝试后仍不合规，最后错误：{last_error}"
            )

        # 兜底：回退原文分片并继续流程，避免在长文末尾失败导致整单报错。
        fallback_chunk, source_json_report = self._replace_output_json_blocks_with_source_and_report(chunk, chunk)
        fallback_chunk, output_json_report = self._sanitize_output_json_blocks_with_report(fallback_chunk)

        removed_headings: list[str] = []
        if continuation_mode:
            fallback_chunk, removed_headings = self._strip_heading_lines_outside_code_blocks(fallback_chunk)

        fallback_chunk, fallback_heading_fixed = self._ensure_allowed_heading_in_chunk(
            converted_chunk=fallback_chunk,
            allowed_headings=allowed_headings,
            continuation_mode=continuation_mode,
            chunk_has_heading=chunk_has_heading,
        )

        fallback_valid, fallback_validation_reason = self._validate_chunk_output(
            source_chunk=chunk,
            converted_chunk=fallback_chunk,
            allowed_headings=allowed_headings,
            continuation_mode=continuation_mode,
            llm_meta={"truncated": False},
        )

        fallback_reasons = source_json_report.get("fallback_reasons", []) + output_json_report.get("fallback_reasons", [])
        fallback_meta: dict[str, Any] = {
            "attempts_used": self.max_chunk_retries + 1,
            "source_json_blocks": source_json_report.get("source_json_blocks", 0),
            "repaired_json_blocks": (
                source_json_report.get("repaired_json_blocks", 0)
                + output_json_report.get("output_json_repaired", 0)
            ),
            "fallback_json_blocks": (
                source_json_report.get("fallback_json_blocks", 0)
                + output_json_report.get("output_json_fallback", 0)
            ),
            "fallback_reasons": fallback_reasons,
            "fallback_used": True,
            "fallback_source": "source_chunk",
            "fallback_reason": last_error or "达到最大重试次数",
        }
        if removed_headings:
            fallback_meta["removed_heading_lines"] = len(removed_headings)
        if fallback_heading_fixed:
            fallback_meta["normalized_heading"] = True
        if not fallback_valid:
            fallback_meta["fallback_validation_reason"] = fallback_validation_reason

        self._emit_logic_event(
            (
                f"分片 {chunk_index}/{total_chunks} 启用兜底：章节={section_label}，"
                f"最后错误={last_error or '未知'}，fallback_valid={fallback_valid}"
            ),
            event_type="chunk_fallback_used",
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            section_id=section_id,
            section_heading=section_heading or None,
            section_label=section_label,
            reason=last_error or "达到最大重试次数",
            fallback_valid=fallback_valid,
            fallback_validation_reason=fallback_validation_reason if not fallback_valid else None,
            removed_heading_lines=len(removed_headings) if removed_headings else 0,
            fallback_reasons=fallback_reasons[:5] if fallback_reasons else None,
        )
        return fallback_chunk, fallback_meta

    def _analyze_structure(self, content: str) -> dict:
        """调用 AI 分析文档结构"""
        self._check_stop()
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
        section_label: str,
        allowed_headings: list[str],
        continuation_mode: bool,
        chunk_has_heading: bool,
        previous_heading: str,
        next_heading: str,
        retry_reason: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """调用 AI 转换一个内容片段，并返回元信息用于校验。"""
        self._check_stop()
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
                "section_heading": section_heading,
                "section_label": section_label,
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
        self._check_stop()
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
                title = self._strip_heading_attrs(match.group(2).strip())
                if title == "目录":
                    continue
                section_id = self._extract_section_id(title)
                if section_id:
                    level = self._heading_level_from_section_id(section_id)
                else:
                    level = len(match.group(1))
                anchor = re.sub(r'[^\w\u4e00-\u9fff\s-]', '', title.lower())
                anchor = anchor.strip().replace(' ', '-')
                indent = "  " * (level - 2)
                toc_lines.append(f"{indent}- [{title}](#{anchor})")

        return "\n".join(toc_lines)

    def _insert_toc(self, markdown: str, toc: str) -> str:
        """在文档标题和副标题信息后、正文第一个章节标题前插入目录"""
        if not toc.strip():
            return markdown

        cleaned_markdown = self._remove_existing_toc_blocks(markdown)
        lines = cleaned_markdown.split("\n")
        numbered_heading_pattern = re.compile(r'^\s*#{1,6}\s+\d+(?:\.\d+)*\s+')

        # 优先插在第一个编号章节前，避免目录插到正文中段
        insert_pos = -1
        for i, line in enumerate(lines):
            if numbered_heading_pattern.match(line):
                insert_pos = i
                break

        # 兜底策略：沿用旧逻辑，避免无编号文档插入失败
        if insert_pos < 0:
            title_pos = -1
            for i, line in enumerate(lines):
                if line.startswith("# ") and not line.startswith("## "):
                    title_pos = i
                    break
            if title_pos < 0:
                title_pos = 0
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

    def _remove_existing_toc_blocks(self, markdown: str) -> str:
        """
        清理已存在的目录块，避免同一份文档多次后处理时重复插入目录。
        目录块判定规则：
        - 标题为“目录”
        - 后续主体为 Markdown 链接列表（支持嵌套缩进）
        """
        lines = markdown.split("\n")
        heading_pattern = re.compile(r'^\s*#{1,6}\s+目录\s*$')
        toc_item_pattern = re.compile(r'^\s*[-*+]\s+\[[^\]]+\]\(#.+\)\s*$')

        i = 0
        removed_any = False
        while i < len(lines):
            if not heading_pattern.match(lines[i]):
                i += 1
                continue

            # 识别目录正文范围
            j = i + 1
            saw_toc_item = False
            while j < len(lines):
                stripped = lines[j].strip()
                if not stripped:
                    j += 1
                    continue
                if stripped == "---":
                    j += 1
                    break
                if toc_item_pattern.match(lines[j]):
                    saw_toc_item = True
                    j += 1
                    continue
                if re.match(r'^\s*#{1,6}\s+', lines[j]):
                    break
                # 非目录项文本，避免误删正文中的“目录”章节
                if not saw_toc_item:
                    break
                break

            if not saw_toc_item:
                i += 1
                continue

            start = i
            end = j

            # 尽量连同目录前的分隔线一起移除
            k = start - 1
            while k >= 0 and not lines[k].strip():
                k -= 1
            if k >= 0 and lines[k].strip() == "---":
                start = k

            # 目录后如果紧跟空行，顺带收缩
            while end < len(lines) and not lines[end].strip():
                end += 1

            lines = lines[:start] + lines[end:]
            removed_any = True
            i = max(start - 1, 0)

        if not removed_any:
            return markdown
        return "\n".join(lines)

    def _is_markdown_table_line(self, line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2

    def _split_markdown_table_row(self, line: str) -> list[str]:
        stripped = line.strip().strip("|")
        return [cell.strip() for cell in stripped.split("|")]

    def _is_markdown_table_separator(self, row: list[str]) -> bool:
        if not row:
            return False
        return all(bool(re.fullmatch(r':?-{3,}:?', cell.strip())) for cell in row)

    def _parse_markdown_table_block(self, block_lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
        if len(block_lines) < 2:
            return None

        header = self._split_markdown_table_row(block_lines[0])
        separator = self._split_markdown_table_row(block_lines[1])
        if len(header) != len(separator) or not self._is_markdown_table_separator(separator):
            return None

        rows: list[list[str]] = []
        for line in block_lines[2:]:
            row = self._split_markdown_table_row(line)
            if len(row) < len(header):
                row += [""] * (len(header) - len(row))
            elif len(row) > len(header):
                row = row[:len(header)]
            rows.append(row)
        return header, rows

    def _render_markdown_table(self, header: list[str], rows: list[list[str]]) -> list[str]:
        def normalize_cell_for_render(cell: str) -> str:
            value = (cell or "").rstrip()
            # 层级符号前的缩进用于表达树层次，不能 strip 掉前导空白
            if re.match(r'^\s*[└├]─', value):
                return value
            return value.strip()

        lines = [
            f"| {' | '.join(header)} |",
            f"| {' | '.join(':---' for _ in header)} |",
        ]
        for row in rows:
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            elif len(row) > len(header):
                row = row[:len(header)]
            lines.append(f"| {' | '.join(normalize_cell_for_render(cell) for cell in row)} |")
        return lines

    def _normalize_field_name_for_match(self, text: str) -> str:
        normalized = text.strip()
        normalized = re.sub(r'^\s*#{1,6}\s+', '', normalized)
        normalized = normalized.strip("*` ").strip()
        normalized = normalized.lstrip("└─").lstrip("├─").strip()
        normalized = normalized.replace("[]", "")
        normalized = re.sub(r'\s+', '', normalized)
        return normalized

    def _row_matches_parent_field(self, row_name: str, parent_name: str) -> bool:
        row_norm = self._normalize_field_name_for_match(row_name)
        parent_norm = self._normalize_field_name_for_match(parent_name)
        if not row_norm or not parent_norm:
            return False
        if row_norm == parent_norm:
            return True
        if row_norm.endswith(f".{parent_norm}"):
            return True
        if row_norm.endswith(f"_{parent_norm}"):
            return True
        return False

    def _format_hierarchical_child_name(self, parent_name: str, child_name: str) -> str:
        clean_child = child_name.strip().lstrip("└─").lstrip("├─").strip()
        parent_clean = self._normalize_field_name_for_match(parent_name)
        if not clean_child:
            return ""

        # 若子表字段带有父级前缀（如 data.userId），去掉前缀后再转层级样式
        raw_variants = [parent_clean, parent_clean.replace(".", ""), parent_name.strip("` ").strip()]
        for parent in raw_variants:
            if not parent:
                continue
            for prefix in (f"{parent}.", f"{parent}[].", f"{parent}_"):
                if clean_child.startswith(prefix):
                    clean_child = clean_child[len(prefix):].lstrip(".").strip()
                    break

        if not clean_child:
            return ""
        return f"└─{clean_child}"

    def _extract_hierarchical_table_parent(self, line: str) -> str:
        stripped = line.strip()
        if not stripped:
            return ""
        stripped = re.sub(r'^\s*#{1,6}\s+', '', stripped).strip()
        if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
            stripped = stripped[2:-2].strip()
        patterns = [
            re.compile(
                r'^(?:请求参数|返回参数|入参|出参|request|response)?\s*'
                r'(?:`)?([A-Za-z0-9_\-\u4e00-\u9fff.\[\]]+)(?:`)?\s*'
                r'(?:对象|参数|字段)?\s*(?:字段|参数)?\s*(?:说明|结构|详情|定义)\s*[:：]?$',
                flags=re.IGNORECASE,
            ),
            re.compile(
                r'^(?:`)?([A-Za-z0-9_\-\u4e00-\u9fff.\[\]]+)(?:`)?\s*'
                r'(?:对象)?\s*(?:字段|参数)\s*[:：]$',
                flags=re.IGNORECASE,
            ),
        ]
        for pattern in patterns:
            match = pattern.match(stripped)
            if match:
                return match.group(1).strip()
        return ""

    def _pick_table_name_column(self, header: list[str]) -> int:
        for idx, title in enumerate(header):
            if any(key in title for key in ("名称", "字段", "参数", "Name", "Field")):
                return idx
        return 0

    def _pick_table_type_column(self, header: list[str]) -> int:
        for idx, title in enumerate(header):
            if any(key in title for key in ("类型", "Type", "type")):
                return idx
        return 1 if len(header) > 1 else 0

    def _looks_like_object_type(self, value: str) -> bool:
        raw = (value or "").strip()
        if not raw:
            return False
        compact = raw.lower().replace(" ", "")
        return ("object" in compact) or ("对象" in raw) or ("map<" in compact)

    def _extract_raw_table_row_cells(self, line: str, expected_cols: int) -> list[str]:
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            return []
        cells = stripped[1:-1].split("|")
        if len(cells) < expected_cols:
            cells += [""] * (expected_cols - len(cells))
        elif len(cells) > expected_cols:
            cells = cells[:expected_cols]
        return cells

    def _normalize_indented_hierarchy_in_tables(self, markdown: str) -> str:
        """
        同一张表内识别“子字段缩进”并规范为 `└─字段名`：
        - 识别名称列前导空白更深的行
        - 识别 `parent.child` / `parent[].child` 前缀行
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_markdown_table_line(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and self._is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1

            parsed = self._parse_markdown_table_block(block)
            if not parsed:
                result.extend(block)
                continue

            header, rows = parsed
            if not rows:
                result.extend(block)
                continue

            name_col = self._pick_table_name_column(header)
            type_col = self._pick_table_type_column(header)
            raw_cells_rows = [
                self._extract_raw_table_row_cells(line, len(header))
                for line in block[2:]
            ]

            leading_levels: list[int] = []
            for row_idx, row in enumerate(rows):
                if len(row) <= name_col:
                    continue
                name = row[name_col].strip()
                if not name or name.startswith(("└─", "├─")):
                    continue
                raw_name = ""
                if row_idx < len(raw_cells_rows) and len(raw_cells_rows[row_idx]) > name_col:
                    raw_name = raw_cells_rows[row_idx][name_col]
                leading = len(raw_name) - len(raw_name.lstrip(" \t\u00a0\u3000"))
                leading_levels.append(leading)

            baseline_leading = min(leading_levels) if leading_levels else 0

            changed = False
            active_parent = ""

            for row_idx, row in enumerate(rows):
                if len(row) <= name_col:
                    continue
                name = row[name_col].strip()
                if not name:
                    continue

                row_type = row[type_col].strip() if type_col < len(row) else ""
                raw_name = ""
                if row_idx < len(raw_cells_rows) and len(raw_cells_rows[row_idx]) > name_col:
                    raw_name = raw_cells_rows[row_idx][name_col]
                leading = len(raw_name) - len(raw_name.lstrip(" \t\u00a0\u3000"))

                if name.startswith(("└─", "├─")):
                    clean_existing = name.lstrip("└─").lstrip("├─").strip()
                    if self._looks_like_object_type(row_type):
                        active_parent = clean_existing
                    continue

                child_by_indent = leading > baseline_leading
                child_by_prefix = False
                normalized_name = self._normalize_field_name_for_match(name)
                if active_parent:
                    parent_norm = self._normalize_field_name_for_match(active_parent)
                    child_by_prefix = (
                        normalized_name.startswith(f"{parent_norm}.")
                        or normalized_name.startswith(f"{parent_norm}[].")
                        or normalized_name.startswith(f"{parent_norm}_")
                    )

                if child_by_indent or child_by_prefix:
                    formatted = self._format_hierarchical_child_name(active_parent or name, name)
                    if formatted and formatted != row[name_col]:
                        row[name_col] = formatted
                        changed = True
                    if self._looks_like_object_type(row_type):
                        active_parent = row[name_col].lstrip("└─").strip()
                    continue

                if self._looks_like_object_type(row_type):
                    active_parent = name
                else:
                    active_parent = ""

            if changed:
                result.extend(self._render_markdown_table(header, rows))
            else:
                result.extend(block)

        return "\n".join(result)

    def _clean_table_field_name(self, value: str) -> str:
        text = (value or "").strip().strip("*` ")
        text = text.replace("&nbsp;", " ")
        text = re.sub(r'^\s*[└├]─\s*', '', text)
        return text.strip()

    def _extract_leaf_field_name(self, value: str) -> str:
        clean = self._clean_table_field_name(value)
        if not clean:
            return ""
        compact = clean.replace("[]", "")
        if "." in compact:
            parts = [seg.strip() for seg in compact.split(".") if seg.strip()]
            if len(parts) >= 2:
                return parts[-1]
        return clean

    def _extract_json_key_sequence(self, value: Any, depth: int = 1) -> list[tuple[str, int]]:
        keys: list[tuple[str, int]] = []
        if isinstance(value, dict):
            for key, child in value.items():
                key_text = str(key).strip()
                if key_text:
                    keys.append((key_text, depth))
                keys.extend(self._extract_json_key_sequence(child, depth + 1))
        elif isinstance(value, list):
            # 数组结构只展开首个对象样本，避免重复键导致层级误匹配
            for item in value:
                if isinstance(item, (dict, list)):
                    keys.extend(self._extract_json_key_sequence(item, depth))
                    break
        return keys

    def _find_nearby_json_example(self, lines: list[str], start_idx: int) -> Any | None:
        """
        在当前表格后方（同章节内）寻找首个可解析的 JSON 示例。
        """
        search_end = min(len(lines), start_idx + 220)
        i = start_idx
        while i < search_end:
            if i > start_idx and re.match(r'^\s*#{2,6}\s+', lines[i]):
                break

            stripped = lines[i].strip()
            fence_match = re.match(r'^(`{3,})([A-Za-z0-9_-]*)\s*$', stripped)
            if not fence_match:
                i += 1
                continue

            fence = fence_match.group(1)
            lang = (fence_match.group(2) or "").strip().lower()
            j = i + 1
            body_lines: list[str] = []
            closed = False
            while j < search_end:
                if re.match(rf'^{re.escape(fence)}\s*$', lines[j].strip()):
                    closed = True
                    break
                body_lines.append(lines[j])
                j += 1

            if closed:
                i = j + 1
            else:
                # partial 文件可能在 JSON 代码块中截断，允许将剩余内容作为候选 JSON 继续尝试
                i = search_end

            body = "\n".join(body_lines).strip()
            if not body:
                continue
            if lang not in {"", "json"} and not body.startswith(("{", "[")):
                continue

            normalized, ok = self._normalize_json_block(body)
            if not ok:
                continue
            try:
                return json.loads(normalized)
            except Exception:
                continue
        return None

    def _match_table_row_depths_with_json(
        self,
        row_names: list[str],
        json_keys: list[tuple[str, int]],
    ) -> list[int | None]:
        depths: list[int | None] = []
        pointer = 0
        for row_name in row_names:
            clean = self._clean_table_field_name(row_name)
            if not clean:
                depths.append(None)
                continue

            candidates = {
                self._normalize_field_name_for_match(clean),
                self._normalize_field_name_for_match(self._extract_leaf_field_name(clean)),
            }
            candidates = {c for c in candidates if c}
            if not candidates:
                depths.append(None)
                continue

            found_depth: int | None = None
            found_index = -1
            for idx in range(max(pointer - 2, 0), len(json_keys)):
                key, depth = json_keys[idx]
                key_norm = self._normalize_field_name_for_match(str(key))
                if key_norm in candidates:
                    found_depth = depth
                    found_index = idx
                    break

            depths.append(found_depth)
            if found_index >= 0:
                pointer = found_index + 1

        return depths

    def _format_hierarchical_name_by_depth(self, raw_name: str, depth: int) -> str:
        field_name = self._extract_leaf_field_name(raw_name)
        if not field_name:
            return raw_name
        if depth <= 1:
            return field_name
        indent = "  " * max(depth - 2, 0)
        return f"{indent}└─{field_name}"

    def _extract_marker_depth(self, value: str) -> int:
        match = re.match(r'^(\s*)[└├]─', (value or ""))
        if not match:
            return 0
        spaces = len(match.group(1))
        return 2 + max(spaces // 2, 0)

    def _pick_table_desc_column(self, header: list[str], name_col: int, type_col: int) -> int:
        keywords = ("说明", "描述", "意义", "description", "desc", "示例", "example")
        for idx, title in enumerate(header):
            if idx in {name_col, type_col}:
                continue
            lower = title.strip().lower()
            if any(key in lower for key in keywords):
                return idx
        for idx in range(len(header)):
            if idx not in {name_col, type_col}:
                return idx
        return -1

    def _is_parent_candidate_row(
        self,
        row: list[str],
        name_col: int,
        type_col: int,
        desc_col: int,
        has_next_marker_row: bool,
    ) -> bool:
        if len(row) <= name_col:
            return False
        name = row[name_col]
        if not re.search(r'[└├]─', name):
            return False

        row_type = row[type_col].strip() if type_col < len(row) else ""
        if self._looks_like_object_type(row_type):
            return True
        if "array" in row_type.lower() or "列表" in row_type or "数组" in row_type:
            return True

        if not has_next_marker_row:
            return False

        # 部分模型会把父节点类型丢失为留空，此时用“类型空 + 说明空”作为弱判定。
        if row_type:
            return False
        desc = row[desc_col].strip() if desc_col >= 0 and desc_col < len(row) else ""
        return desc == ""

    def _normalize_hierarchy_from_nearby_json_examples(self, markdown: str) -> str:
        """
        参考表格后方的 JSON 返回示例，修正参数表中 `└─` 的层级缩进。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_markdown_table_line(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and self._is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1

            parsed = self._parse_markdown_table_block(block)
            if not parsed:
                result.extend(block)
                continue

            header, rows = parsed
            if not rows:
                result.extend(block)
                continue

            name_col = self._pick_table_name_column(header)
            row_names = [
                row[name_col] if len(row) > name_col else ""
                for row in rows
            ]
            meaningful_rows = [name for name in row_names if name.strip()]
            if len(meaningful_rows) < 3:
                result.extend(block)
                continue

            json_obj = self._find_nearby_json_example(lines, i)
            if json_obj is None:
                result.extend(block)
                continue

            json_keys = self._extract_json_key_sequence(json_obj, depth=1)
            if len(json_keys) < 3:
                result.extend(block)
                continue

            depths = self._match_table_row_depths_with_json(row_names, json_keys)
            matched_depths = [depth for depth in depths if depth is not None]
            if not matched_depths:
                result.extend(block)
                continue

            # 匹配率过低时跳过，避免关联到无关 JSON 示例
            if len(matched_depths) < max(2, len(meaningful_rows) // 3):
                result.extend(block)
                continue

            changed = False
            for row_idx, row in enumerate(rows):
                if len(row) <= name_col:
                    continue
                depth = depths[row_idx]
                if depth is None:
                    continue
                formatted = self._format_hierarchical_name_by_depth(row[name_col], depth)
                if formatted != row[name_col]:
                    row[name_col] = formatted
                    changed = True

            if changed:
                result.extend(self._render_markdown_table(header, rows))
            else:
                result.extend(block)

        return "\n".join(result)

    def _normalize_hierarchy_with_object_row_fallback(self, markdown: str) -> str:
        """
        在缺少可用 JSON 示例时，仅基于同表中的对象父行推断层级。
        仅在“当前几乎全是一层 `└─`”时触发，避免覆盖已有正确层级。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_markdown_table_line(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and self._is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1

            parsed = self._parse_markdown_table_block(block)
            if not parsed:
                result.extend(block)
                continue

            header, rows = parsed
            if not rows:
                result.extend(block)
                continue

            name_col = self._pick_table_name_column(header)
            type_col = self._pick_table_type_column(header)
            desc_col = self._pick_table_desc_column(header, name_col, type_col)
            raw_cells_rows = [
                self._extract_raw_table_row_cells(line, len(header))
                for line in block[2:]
            ]

            marker_depths = [
                self._extract_marker_depth(
                    raw_cells_rows[idx][name_col]
                    if idx < len(raw_cells_rows) and len(raw_cells_rows[idx]) > name_col
                    else row[name_col]
                )
                for idx, row in enumerate(rows)
                if len(row) > name_col and (
                    re.search(
                        r'[└├]─',
                        raw_cells_rows[idx][name_col]
                        if idx < len(raw_cells_rows) and len(raw_cells_rows[idx]) > name_col
                        else row[name_col],
                    )
                )
            ]
            if len(marker_depths) < 3:
                result.extend(block)
                continue

            # 只有全部或几乎全部同层时才尝试回退推断
            if max(marker_depths) - min(marker_depths) > 0:
                result.extend(block)
                continue

            inferred_depths: list[int | None] = [None] * len(rows)
            parent_stack: list[dict[str, Any]] = []
            changed = False

            for row_idx, row in enumerate(rows):
                if len(row) <= name_col:
                    continue

                name = row[name_col]
                is_marker = bool(re.search(r'[└├]─', name))
                row_type = row[type_col].strip() if type_col < len(row) else ""

                if not is_marker:
                    if self._looks_like_object_type(row_type):
                        parent_stack = [{"depth": 1, "has_children": False}]
                    else:
                        parent_stack = []
                    continue

                has_next_marker_row = False
                for next_idx in range(row_idx + 1, len(rows)):
                    if len(rows[next_idx]) <= name_col:
                        continue
                    if re.search(r'[└├]─', rows[next_idx][name_col]):
                        has_next_marker_row = True
                        break

                parent_candidate = self._is_parent_candidate_row(
                    row=row,
                    name_col=name_col,
                    type_col=type_col,
                    desc_col=desc_col,
                    has_next_marker_row=has_next_marker_row,
                )

                if parent_candidate:
                    if not parent_stack:
                        depth = 2
                    else:
                        top = parent_stack[-1]
                        if not top["has_children"]:
                            depth = top["depth"] + 1
                        else:
                            depth = top["depth"]
                            parent_stack.pop()
                            while parent_stack and parent_stack[-1]["depth"] >= depth:
                                parent_stack.pop()
                        if parent_stack:
                            parent_stack[-1]["has_children"] = True
                    parent_stack.append({"depth": depth, "has_children": False})
                else:
                    if parent_stack:
                        depth = parent_stack[-1]["depth"] + 1
                        parent_stack[-1]["has_children"] = True
                    else:
                        depth = 2

                inferred_depths[row_idx] = depth
                raw_name = (
                    raw_cells_rows[row_idx][name_col]
                    if row_idx < len(raw_cells_rows) and len(raw_cells_rows[row_idx]) > name_col
                    else name
                )
                current_depth = self._extract_marker_depth(raw_name)
                if current_depth and current_depth != depth:
                    changed = True

            if not changed:
                result.extend(block)
                continue

            for row_idx, depth in enumerate(inferred_depths):
                if depth is None or len(rows[row_idx]) <= name_col:
                    continue
                rows[row_idx][name_col] = self._format_hierarchical_name_by_depth(rows[row_idx][name_col], depth)

            result.extend(self._render_markdown_table(header, rows))

        return "\n".join(result)

    def _merge_hierarchical_field_tables(self, markdown: str) -> str:
        """
        合并“主表 + xxx 对象字段说明子表”：
        在同一张表内以 `└─` 标记子字段，避免分段描述。
        """
        lines = markdown.split("\n")
        i = 0

        while i < len(lines):
            parent_name = self._extract_hierarchical_table_parent(lines[i])
            if not parent_name:
                i += 1
                continue

            prev_end = i - 1
            while prev_end >= 0 and not lines[prev_end].strip():
                prev_end -= 1
            if prev_end < 0 or not self._is_markdown_table_line(lines[prev_end]):
                i += 1
                continue

            prev_start = prev_end
            while prev_start - 1 >= 0 and self._is_markdown_table_line(lines[prev_start - 1]):
                prev_start -= 1

            next_start = i + 1
            while next_start < len(lines) and not lines[next_start].strip():
                next_start += 1
            if next_start >= len(lines) or not self._is_markdown_table_line(lines[next_start]):
                i += 1
                continue

            next_end = next_start
            while next_end + 1 < len(lines) and self._is_markdown_table_line(lines[next_end + 1]):
                next_end += 1

            parent_table = self._parse_markdown_table_block(lines[prev_start:prev_end + 1])
            child_table = self._parse_markdown_table_block(lines[next_start:next_end + 1])
            if not parent_table or not child_table:
                i += 1
                continue

            parent_header, parent_rows = parent_table
            child_header, child_rows = child_table
            if len(parent_header) != len(child_header) or not child_rows:
                i += 1
                continue

            name_col = self._pick_table_name_column(parent_header)
            existing_name_keys = {
                self._normalize_field_name_for_match(row[name_col])
                for row in parent_rows
                if len(row) > name_col and row[name_col].strip()
            }
            parent_exists = any(
                self._row_matches_parent_field(row[name_col], parent_name)
                for row in parent_rows
                if len(row) > name_col
            )
            if not parent_exists:
                i += 1
                continue

            merged_rows = list(parent_rows)
            for row in child_rows:
                if len(row) <= name_col:
                    continue
                child_name = row[name_col].strip()
                if not child_name:
                    continue
                row = list(row)
                formatted_name = self._format_hierarchical_child_name(parent_name, child_name)
                if not formatted_name:
                    continue
                key = self._normalize_field_name_for_match(formatted_name)
                if key in existing_name_keys:
                    continue
                existing_name_keys.add(key)
                row[name_col] = formatted_name
                merged_rows.append(row)

            merged_table_lines = self._render_markdown_table(parent_header, merged_rows)
            lines = lines[:prev_start] + merged_table_lines + lines[next_end + 1:]
            i = prev_start + len(merged_table_lines)

        return "\n".join(lines)

    def _flatten_residual_grid_table_rows(self, markdown: str) -> str:
        """
        处理残留的 pandoc 网格表片段（如 `|   +----+` / `|   | field |`）：
        将其子字段行并入上一张 Markdown 表，并使用 `└─` 标记层级。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        def prev_non_empty_is_table() -> bool:
            idx = len(result) - 1
            while idx >= 0 and not result[idx].strip():
                idx -= 1
            return idx >= 0 and self._is_markdown_table_line(result[idx])

        while i < len(lines):
            line = lines[i]
            if not re.match(r'^\|\s*\+-+', line):
                result.append(line)
                i += 1
                continue

            block: list[str] = []
            while i < len(lines):
                current = lines[i]
                if not current.strip():
                    block.append(current)
                    i += 1
                    break
                if current.startswith("|") or re.match(r'^\+-+', current):
                    block.append(current)
                    i += 1
                    continue
                break

            if not prev_non_empty_is_table():
                result.extend(block)
                continue

            child_rows: list[tuple[str, str, str]] = []
            for row_line in block:
                # 目标行形态：|   | field | type | desc |
                row_match = re.match(r'^\|\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$', row_line)
                if not row_match:
                    continue
                name = row_match.group(1).strip()
                typ = row_match.group(2).strip()
                desc = row_match.group(3).strip()
                if not name:
                    continue
                child_rows.append((name, typ, desc))

            if not child_rows:
                result.extend(block)
                continue

            while result and not result[-1].strip():
                result.pop()
            table_end = len(result) - 1
            while table_end >= 0 and not result[table_end].strip():
                table_end -= 1
            table_start = table_end
            while table_start - 1 >= 0 and self._is_markdown_table_line(result[table_start - 1]):
                table_start -= 1
            parsed_prev = (
                self._parse_markdown_table_block(result[table_start:table_end + 1])
                if table_end >= table_start >= 0
                else None
            )

            if parsed_prev:
                prev_header, _ = parsed_prev
                name_col = self._pick_table_name_column(prev_header)
                type_col = self._pick_table_type_column(prev_header)
                desc_col = self._pick_table_desc_column(prev_header, name_col, type_col)
                if desc_col < 0:
                    desc_col = len(prev_header) - 1
                for name, typ, desc in child_rows:
                    clean_name = name.lstrip("└─").lstrip("├─").strip()
                    row_cells = [""] * len(prev_header)
                    if name_col < len(row_cells):
                        row_cells[name_col] = f"└─{clean_name}"
                    if type_col < len(row_cells):
                        row_cells[type_col] = typ
                    if desc_col < len(row_cells):
                        row_cells[desc_col] = desc
                    result.append(f"| {' | '.join(cell.strip() for cell in row_cells)} |")
            else:
                for name, typ, desc in child_rows:
                    clean_name = name.lstrip("└─").lstrip("├─").strip()
                    result.append(f"| └─{clean_name} | {typ} | {desc} |")

            if i < len(lines) and not lines[i - 1].strip():
                result.append("")

        return "\n".join(result)

    def _split_grid_row_cells(self, row_line: str) -> list[str]:
        stripped = row_line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            return []
        return [cell.strip() for cell in stripped.strip("|").split("|")]

    def _infer_grid_table_header(self, row_cells: list[list[str]]) -> list[str]:
        for cells in row_cells:
            non_empty = [cell for cell in cells if cell.strip()]
            if len(non_empty) >= 2:
                return non_empty
        return []

    def _project_grid_row_to_columns(self, cells: list[str], output_cols: int) -> tuple[list[str], int]:
        if output_cols <= 0:
            return [], 0
        if len(cells) < output_cols:
            projected = cells + [""] * (output_cols - len(cells))
            return projected, 0
        extra = max(0, len(cells) - output_cols)
        projected = cells[-output_cols:]
        return projected, extra

    def _join_table_cell_text(self, base: str, addition: str) -> str:
        left = (base or "").strip()
        right = (addition or "").strip()
        if not left:
            return right
        if not right:
            return left
        if left.endswith(("：", ":", "；", ";", "，", ",", "。", "、", "/", " ")) or right.startswith(("（", "(", ":", "：")):
            return f"{left}{right}"
        return f"{left} {right}"

    def _is_wrapped_description_row(self, row: list[str], desc_col: int) -> bool:
        if not row or desc_col < 0 or desc_col >= len(row):
            return False
        desc = row[desc_col].strip()
        if not desc:
            return False
        return all(not row[idx].strip() for idx in range(len(row)) if idx != desc_col)

    def _convert_residual_grid_tables(self, markdown: str) -> str:
        """
        将残留 grid table（+---- 边框）转换为标准 Markdown 表格：
        - 保留原始列数（3 列/4 列等）
        - 依据前导空列推断层级（用于 `records -> accountId` 等）
        - 合并“仅说明列有内容”的换行行
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            line = lines[i]
            if not re.match(r'^\+[:=\-]+', line):
                result.append(line)
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and (lines[i].startswith("+") or lines[i].startswith("|")):
                block.append(lines[i])
                i += 1

            raw_rows: list[list[str]] = []
            for row_line in block:
                if not row_line.startswith("|"):
                    continue
                cells = self._split_grid_row_cells(row_line)
                if not cells:
                    continue
                if all(re.fullmatch(r'[:=\-+ ]*', cell or "") for cell in cells):
                    continue
                if all(not cell.strip() for cell in cells):
                    continue
                raw_rows.append(cells)

            if len(raw_rows) < 2:
                result.extend(block)
                continue

            header_candidates = self._infer_grid_table_header(raw_rows)
            if len(header_candidates) < 2:
                result.extend(block)
                continue
            header = header_candidates[:6]
            output_cols = len(header)
            if output_cols < 2:
                result.extend(block)
                continue

            rows: list[list[str]] = []
            row_depth_hints: list[int] = []
            header_consumed = False

            for cells in raw_rows:
                non_empty = [cell for cell in cells if cell.strip()]
                if not header_consumed and non_empty == header_candidates:
                    header_consumed = True
                    continue

                projected, extra_depth = self._project_grid_row_to_columns(cells, output_cols)
                if not projected or not any(cell.strip() for cell in projected):
                    continue
                rows.append(projected)
                row_depth_hints.append(extra_depth)

            if not rows:
                result.extend(block)
                continue

            name_col = self._pick_table_name_column(header)
            type_col = self._pick_table_type_column(header)
            desc_col = self._pick_table_desc_column(header, name_col, type_col)
            if desc_col < 0:
                desc_col = len(header) - 1

            normalized_rows: list[list[str]] = []
            for row, depth_hint in zip(rows, row_depth_hints):
                current = list(row)
                # 3 列结构通常是对象字段表，空前导列可映射为层级深度
                if output_cols <= 3 and depth_hint > 0 and name_col < len(current) and current[name_col].strip():
                    current[name_col] = self._format_hierarchical_name_by_depth(
                        current[name_col],
                        depth=depth_hint + 1,
                    )
                normalized_rows.append(current)

            merged_rows: list[list[str]] = []
            for row in normalized_rows:
                if self._is_wrapped_description_row(row, desc_col):
                    if merged_rows:
                        merged_rows[-1][desc_col] = self._join_table_cell_text(merged_rows[-1][desc_col], row[desc_col])
                    continue
                merged_rows.append(row)

            if not merged_rows:
                result.extend(block)
                continue

            result.extend(self._render_markdown_table(header, merged_rows))
            if i < len(lines) and not lines[i].strip():
                result.append(lines[i])
                i += 1

        return "\n".join(result)

    def _split_loose_table_cells(self, line: str, prefer_wide_gap: bool = False) -> list[str]:
        stripped = line.strip()
        if not stripped:
            return []

        if "\t" in stripped:
            by_tab = [cell.strip() for cell in re.split(r'\t+', stripped) if cell.strip()]
            if len(by_tab) >= 2:
                return by_tab

        if prefer_wide_gap:
            by_wide_gap = [cell.strip() for cell in re.split(r'\s{2,}', stripped) if cell.strip()]
            if len(by_wide_gap) >= 2:
                return by_wide_gap

        by_wide_gap = [cell.strip() for cell in re.split(r'\s{2,}', stripped) if cell.strip()]
        if len(by_wide_gap) >= 2:
            return by_wide_gap

        return [cell.strip() for cell in re.split(r'\s+', stripped) if cell.strip()]

    def _fit_table_cells(self, cells: list[str], expected_cols: int) -> list[str]:
        if expected_cols <= 0:
            return []
        if len(cells) < expected_cols:
            return []
        if len(cells) > expected_cols:
            head = cells[:expected_cols - 1]
            tail = " ".join(cells[expected_cols - 1:]).strip()
            cells = head + [tail]
        return [cell.strip() for cell in cells]

    def _is_pandoc_simple_table_border(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.startswith(("+", "|")):
            return False
        segments = [seg for seg in re.split(r'\s+', stripped) if seg]
        if len(segments) < 2:
            return False
        return all(bool(re.fullmatch(r':?-{3,}:?', seg)) for seg in segments)

    def _convert_pandoc_simple_tables(self, markdown: str) -> str:
        """
        将 pandoc simple table（由 `----- -----` 包围）转换为标准 Markdown 表格。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_pandoc_simple_table_border(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            start = i
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines):
                result.extend(lines[start:i])
                break

            header_cells = self._split_loose_table_cells(lines[i], prefer_wide_gap=True)
            if len(header_cells) < 2:
                result.extend(lines[start:i + 1])
                i += 1
                continue

            expected_cols = len(header_cells)
            i += 1
            rows: list[list[str]] = []
            end_found = False

            while i < len(lines):
                current = lines[i]
                if self._is_pandoc_simple_table_border(current):
                    end_found = True
                    i += 1
                    break
                if not current.strip():
                    i += 1
                    continue
                cells = self._split_loose_table_cells(current, prefer_wide_gap=True)
                fitted = self._fit_table_cells(cells, expected_cols)
                if not fitted:
                    break
                rows.append(fitted)
                i += 1

            if end_found and rows:
                result.extend(self._render_markdown_table(header_cells, rows))
                continue

            result.extend(lines[start:i])

        return "\n".join(result)

    def _looks_like_plain_table_header(self, cells: list[str]) -> bool:
        if len(cells) < 3:
            return False
        header_keywords = (
            "名称",
            "类型",
            "必填",
            "说明",
            "字段",
            "参数",
            "错误码",
            "意义",
            "描述",
            "Name",
            "Type",
            "Required",
            "Description",
        )
        hit_count = 0
        for cell in cells:
            if any(keyword in cell for keyword in header_keywords):
                hit_count += 1
        return hit_count >= 2

    def _convert_plain_text_tabular_blocks(self, markdown: str) -> str:
        """
        将“名称 类型 必填 说明”这类纯文本伪表格转换为 Markdown 表格。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0
        in_code_block = False

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("```"):
                in_code_block = not in_code_block
                result.append(line)
                i += 1
                continue

            if in_code_block:
                result.append(line)
                i += 1
                continue

            if (
                not stripped
                or stripped.startswith(("#", "-", "*", ">", "|", "+"))
                or re.match(r'^\d+\.\s+', stripped)
            ):
                result.append(line)
                i += 1
                continue

            header_cells = self._split_loose_table_cells(stripped)
            if not self._looks_like_plain_table_header(header_cells):
                result.append(line)
                i += 1
                continue

            expected_cols = len(header_cells)
            rows: list[list[str]] = []
            j = i + 1

            # 允许表头后有空行
            while j < len(lines) and not lines[j].strip():
                j += 1

            while j < len(lines):
                current = lines[j]
                current_stripped = current.strip()
                if not current_stripped:
                    break
                if current_stripped.startswith(("```", "#", "-", "*", ">", "|", "+")):
                    break
                if re.match(r'^\d+\.\s+', current_stripped):
                    break

                row_cells = self._split_loose_table_cells(current_stripped)
                fitted = self._fit_table_cells(row_cells, expected_cols)
                if not fitted:
                    break
                rows.append(fitted)
                j += 1

            if not rows:
                result.append(line)
                i += 1
                continue

            result.extend(self._render_markdown_table(header_cells, rows))
            i = j

        return "\n".join(result)

    def _merge_wrapped_description_rows_in_tables(self, markdown: str) -> str:
        """
        合并 Markdown 表格中“仅说明列有值”的换行行，避免出现空字段占位行。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_markdown_table_line(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and self._is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1

            parsed = self._parse_markdown_table_block(block)
            if not parsed:
                result.extend(block)
                continue

            header, rows = parsed
            if not rows:
                result.extend(block)
                continue

            name_col = self._pick_table_name_column(header)
            type_col = self._pick_table_type_column(header)
            desc_col = self._pick_table_desc_column(header, name_col, type_col)
            if desc_col < 0:
                desc_col = len(header) - 1

            changed = False
            merged_rows: list[list[str]] = []

            for row in rows:
                if not any(cell.strip() for cell in row):
                    changed = True
                    continue
                if self._is_wrapped_description_row(row, desc_col):
                    if merged_rows:
                        merged_rows[-1][desc_col] = self._join_table_cell_text(merged_rows[-1][desc_col], row[desc_col])
                        changed = True
                        continue
                merged_rows.append(row)

            if changed:
                result.extend(self._render_markdown_table(header, merged_rows))
            else:
                result.extend(block)

        return "\n".join(result)

    def _expand_required_only_tables_with_description(self, markdown: str) -> str:
        """
        将 `名称|类型|必填` 三列表扩展为 `名称|类型|必填|说明` 四列表。
        同时把“必填列中混入的说明文本”拆到说明列。
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        while i < len(lines):
            if not self._is_markdown_table_line(lines[i]):
                result.append(lines[i])
                i += 1
                continue

            block: list[str] = []
            while i < len(lines) and self._is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1

            parsed = self._parse_markdown_table_block(block)
            if not parsed:
                result.extend(block)
                continue

            header, rows = parsed
            if len(header) != 3:
                result.extend(block)
                continue

            required_col = next((idx for idx, title in enumerate(header) if "必填" in title), -1)
            has_desc_col = any(("说明" in title or "描述" in title) for title in header)
            if required_col < 0 or has_desc_col:
                result.extend(block)
                continue

            new_header = header + ["说明"]
            new_rows: list[list[str]] = []
            changed = False

            for row in rows:
                current = list(row)
                if len(current) < 3:
                    current += [""] * (3 - len(current))
                required_text = current[required_col].strip()
                desc_text = ""

                match = re.match(r'^(是/否|否/是|是|否)(.*)$', required_text)
                if match:
                    required_value = match.group(1).strip()
                    desc_text = match.group(2).strip()
                    if required_value != current[required_col]:
                        current[required_col] = required_value
                        changed = True
                    if desc_text:
                        changed = True

                new_rows.append(current + [desc_text])

            if changed or rows:
                result.extend(self._render_markdown_table(new_header, new_rows))
            else:
                result.extend(block)

        return "\n".join(result)

    def _normalize_api_label_lines(self, markdown: str) -> str:
        """
        规范接口文档中的标签行格式，例如：
        `请求方式：GET` -> `**请求方式：** GET`
        """
        lines = markdown.split("\n")
        result: list[str] = []
        in_code_block = False
        label_pattern = re.compile(
            r'^\s*(接口地址|请求方式|接口描述|请求参数|返回参数|请求示例|返回示例|响应示例|接口说明|注意事项|注意)\s*[：:]\s*(.*)$'
        )
        bold_label_pattern = re.compile(
            r'^\s*\*\*(接口地址|请求方式|接口描述|请求参数|返回参数|请求示例|返回示例|响应示例|接口说明|注意事项|注意)\s*[：:]\*\*\s*(.*)$'
        )

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                result.append(line)
                continue

            if in_code_block or not stripped or stripped.startswith(("#", "|")):
                result.append(line)
                continue

            bold_match = bold_label_pattern.match(line)
            if bold_match:
                label = bold_match.group(1).strip()
                value = bold_match.group(2).strip()
                if value:
                    result.append(f"**{label}：** {value}")
                else:
                    result.append(f"**{label}：**")
                continue

            match = label_pattern.match(line)
            if not match:
                result.append(line)
                continue

            label = match.group(1).strip()
            value = match.group(2).strip()
            if value:
                result.append(f"**{label}：** {value}")
            else:
                result.append(f"**{label}：**")

        return "\n".join(result)

    def _is_curl_continuation_line(self, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        return bool(
            re.match(r'^\\?--[A-Za-z0-9_-]+', stripped)
            or re.match(r'^\\?-[A-Za-z]\b', stripped)
        )

    def _wrap_curl_commands_in_code_blocks(self, markdown: str) -> str:
        """
        修复/补齐 curl 示例代码块：
        - 将裸露的 curl 命令包裹为 ```bash
        - 若已有代码块只包住首行，自动吸收后续续行参数
        """
        lines = markdown.split("\n")
        result: list[str] = []
        i = 0

        def extend_with_continuations(block_lines: list[str], start_idx: int) -> tuple[list[str], int]:
            idx = start_idx
            while idx < len(lines):
                next_line = lines[idx]
                next_stripped = next_line.strip()

                if next_stripped.startswith("```"):
                    break

                last_non_empty = next((item for item in reversed(block_lines) if item.strip()), "")
                prev_continues = last_non_empty.rstrip().endswith("\\")

                if not next_stripped:
                    lookahead = idx + 1
                    while lookahead < len(lines) and not lines[lookahead].strip():
                        lookahead += 1

                    if (
                        prev_continues
                        and lookahead < len(lines)
                        and self._is_curl_continuation_line(lines[lookahead])
                    ):
                        block_lines.extend(lines[idx:lookahead])
                        block_lines.append(lines[lookahead].rstrip())
                        idx = lookahead + 1
                        continue
                    break

                if prev_continues and self._is_curl_continuation_line(next_line):
                    block_lines.append(next_line.rstrip())
                    idx += 1
                    continue

                # 少量样本中第一行未以 \ 结尾，但后续行仍是参数行，兜底吸收
                if len(block_lines) == 1 and self._is_curl_continuation_line(next_line):
                    block_lines.append(next_line.rstrip())
                    idx += 1
                    continue

                break

            return block_lines, idx

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            fence_match = re.match(r'^(`{3,})([A-Za-z0-9_-]*)\s*$', stripped)
            if fence_match:
                fence = fence_match.group(1)
                j = i + 1
                block_lines: list[str] = []
                while j < len(lines):
                    if re.match(rf'^{re.escape(fence)}\s*$', lines[j].strip()):
                        break
                    block_lines.append(lines[j].rstrip())
                    j += 1

                # 非闭合围栏，保留原样避免破坏文档
                if j >= len(lines):
                    result.append(line)
                    result.extend(block_lines)
                    break

                closing_line = lines[j]
                if any(re.match(r'^\s*curl\b', block_line) for block_line in block_lines):
                    block_lines, next_idx = extend_with_continuations(block_lines, j + 1)
                    result.append(line)
                    result.extend(block_lines)
                    result.append(closing_line)
                    i = next_idx
                    continue

                result.append(line)
                result.extend(block_lines)
                result.append(closing_line)
                i = j + 1
                continue

            if not re.match(r'^\s*curl\b', line):
                result.append(line)
                i += 1
                continue

            block_lines = [line.rstrip()]
            block_lines, i = extend_with_continuations(block_lines, i + 1)

            result.append("```bash")
            result.extend(block_lines)
            result.append("```")

        return "\n".join(result)

    def _normalize_json_fenced_blocks(self, markdown: str) -> str:
        """
        将可解析的围栏代码块统一为格式化的 ```json，提升可读性。
        仅处理空语言或 json 语言围栏，避免误改 bash 等示例。
        """
        pattern = re.compile(r'```([A-Za-z0-9_-]*)\s*\n([\s\S]*?)\n```')

        def repl(match: re.Match[str]) -> str:
            lang = (match.group(1) or "").strip().lower()
            if lang not in {"", "json"}:
                return match.group(0)

            body = match.group(2).strip()
            if not body:
                return match.group(0)

            normalized, ok = self._normalize_json_block(body)
            if not ok:
                return match.group(0)

            try:
                parsed = json.loads(normalized)
            except Exception:
                return match.group(0)

            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            return self._fence_code_block(pretty, language="json")

        return pattern.sub(repl, markdown)

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

        # 最终兜底：规范化/降级残留 JSON 代码块，避免最终校验失败
        markdown, json_report = self._sanitize_output_json_blocks_with_report(markdown)
        if json_report.get("output_json_blocks", 0):
            self._emit_logic_event(
                (
                    "最终 JSON 清理："
                    f"checked={json_report.get('output_json_blocks', 0)}，"
                    f"repaired={json_report.get('output_json_repaired', 0)}，"
                    f"fallback={json_report.get('output_json_fallback', 0)}"
                ),
                event_type="json_final_cleanup",
                checked_blocks=json_report.get("output_json_blocks", 0),
                repaired_blocks=json_report.get("output_json_repaired", 0),
                fallback_blocks=json_report.get("output_json_fallback", 0),
                fallback_reasons=json_report.get("fallback_reasons", [])[:5] or None,
            )

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
