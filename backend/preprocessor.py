"""
文档预处理器
负责：docx → 粗糙 markdown 提取、图片提取、内容分片
"""

import os
import re
import shutil
import subprocess
import logging
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)


class DocPreprocessor:
    """文档预处理：提取内容和图片"""

    def __init__(self, input_path: str, work_dir: str, image_dir: str = "images"):
        self.input_path = Path(input_path)
        self.work_dir = Path(work_dir)
        self.image_dir = image_dir
        self.raw_md_path = self.work_dir / "raw_extract.md"
        self.pandoc_image_dir = self.work_dir / "pandoc_images"

        # 确保工作目录存在
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def check_pandoc(self) -> bool:
        """检查 pandoc 是否可用"""
        try:
            result = subprocess.run(
                ["pandoc", "--version"],
                capture_output=True, text=True
            )
            version = result.stdout.split("\n")[0]
            logger.info(f"检测到 {version}")
            return True
        except FileNotFoundError:
            logger.error("未找到 pandoc，请先安装: https://pandoc.org/installing.html")
            return False

    def extract(self) -> Tuple[str, List[str]]:
        """
        从 docx 提取 markdown 和图片
        :return: (markdown文本, 图片路径列表)
        """
        if not self.check_pandoc():
            raise RuntimeError("pandoc 未安装")

        if not self.input_path.exists():
            raise FileNotFoundError(f"输入文件不存在: {self.input_path}")

        suffix = self.input_path.suffix.lower()
        if suffix == ".doc":
            logger.info("检测到 .doc 格式，尝试用 LibreOffice 转换...")
            self._convert_doc_to_docx()

        logger.info(f"正在提取: {self.input_path}")

        # 调用 pandoc 提取
        cmd = [
            "pandoc",
            str(self.input_path),
            "-t", "markdown",
            "--wrap=none",
            "--extract-media", str(self.pandoc_image_dir),
            "-o", str(self.raw_md_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"pandoc 错误: {result.stderr}")
            raise RuntimeError(f"pandoc 提取失败: {result.stderr}")

        # 读取提取的 markdown
        raw_md = self.raw_md_path.read_text(encoding="utf-8")
        logger.info(f"提取完成，共 {len(raw_md)} 字符，{raw_md.count(chr(10))} 行")

        # 收集提取的图片
        images = self._collect_images()
        logger.info(f"提取到 {len(images)} 张图片")

        return raw_md, images

    def _convert_doc_to_docx(self):
        """用 LibreOffice 将 .doc 转为 .docx"""
        try:
            subprocess.run([
                "libreoffice", "--headless", "--convert-to", "docx",
                "--outdir", str(self.work_dir),
                str(self.input_path)
            ], capture_output=True, check=True)
            self.input_path = self.work_dir / self.input_path.with_suffix(".docx").name
        except Exception as e:
            raise RuntimeError(f".doc 转换失败，请安装 LibreOffice: {e}")

    def _collect_images(self) -> List[str]:
        """收集 pandoc 提取的图片并整理到 images/ 目录"""
        images = []

        # pandoc 通常提取到 pandoc_images/media/ 下
        search_dirs = [
            self.pandoc_image_dir,
            self.pandoc_image_dir / "media",
        ]

        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".svg", ".webp"}

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for f in sorted(search_dir.rglob("*")):
                if f.suffix.lower() in image_exts:
                    images.append(str(f))

        return images

    def organize_images(self, output_dir: Path, images: List[str]) -> dict:
        """
        将图片整理到输出目录的 images/ 下
        :return: 旧路径 → 新相对路径 的映射
        """
        img_output = output_dir / self.image_dir
        img_output.mkdir(parents=True, exist_ok=True)

        path_mapping = {}

        for img_path in images:
            src = Path(img_path)
            dst = img_output / src.name

            # 如果文件名冲突，加序号
            counter = 1
            while dst.exists():
                dst = img_output / f"{src.stem}_{counter}{src.suffix}"
                counter += 1

            shutil.copy2(src, dst)

            # 记录映射关系（pandoc 输出中的相对路径 → 新相对路径）
            # pandoc 可能输出 pandoc_images/media/image4.png 这样的路径
            old_relative = str(src.relative_to(self.work_dir)) if src.is_relative_to(self.work_dir) else src.name
            new_relative = f"{self.image_dir}/{dst.name}"
            path_mapping[old_relative] = new_relative

            # 也映射可能的其他引用形式
            path_mapping[src.name] = new_relative
            # pandoc 常见格式: media/media/imageX.png
            for pattern in [f"media/media/{src.name}", f"media/{src.name}"]:
                path_mapping[pattern] = new_relative

        logger.info(f"已整理 {len(images)} 张图片到 {img_output}")
        return path_mapping


def fix_pandoc_table_codeblocks(text: str) -> str:
    """
    将 pandoc 提取的单列表格（+---+ | content | 格式）中包含的
    JSON / 代码内容，转换为标准 Markdown 代码块。

    pandoc 会把 Word 中的文本框或单格表格提取为：
    +-------+
    | {     |
    | "a":1 |
    | }     |
    +-------+
    本函数将其识别并转为 ```json ... ``` 格式。
    """
    lines = text.split("\n")
    result = []
    i = 0

    # 单列表格边框：允许 pandoc 对齐语法中的冒号（如 +:-----+）
    single_col_border = re.compile(r'^\+[=:\-]+\+$')

    while i < len(lines):
        line = lines[i]

        # 只匹配单列表格边框：仅首尾两个 +，中间全是 - 或 =
        # 多列表格如 +---+---+---+ 中间有额外的 +，不匹配
        if single_col_border.match(line.strip()):
            # 记住起始位置，解析失败时可以回退
            start_i = i
            table_content_lines = []
            i += 1
            while i < len(lines):
                row = lines[i]
                # 同样只匹配单列结束边框
                if single_col_border.match(row.strip()):
                    i += 1
                    break  # 表格结束
                # 提取 | ... | 中的内容
                cell_match = re.match(r'^\|\s?(.*?)\s*\|$', row)
                if cell_match:
                    cell_text = cell_match.group(1).rstrip()
                    table_content_lines.append(cell_text)
                elif row.strip() == '|' or row.strip() == '':
                    table_content_lines.append('')
                else:
                    # 不是表格行，回退
                    table_content_lines = None
                    break
                i += 1

            if table_content_lines is not None:
                # 去掉 pandoc 表格中的多余空行
                table_content_lines = [l for l in table_content_lines if l.strip() != '']
                content = "\n".join(table_content_lines)
                content = content.replace("\u00a0", " ")
                # 去掉转义的引号
                content = content.replace('\\"', '"')
                content = content.replace('\\[', '[')
                content = content.replace('\\]', ']')

                # 判断是否为 JSON 内容
                stripped = content.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    result.append(f"```json\n{content}\n```")
                elif stripped.startswith('curl ') or stripped.startswith('curl\n'):
                    result.append(f"```bash\n{content}\n```")
                else:
                    # 不确定类型，作为普通代码块
                    result.append(f"```\n{content}\n```")
            else:
                # 解析失败，回退到起始行之后重新处理，避免丢失行
                result.append(lines[start_i])
                i = start_i + 1
            continue

        result.append(line)
        i += 1

    return "\n".join(result)


def split_content(text: str, chunk_size: int = 8000) -> List[str]:
    """
    按章节边界智能分片
    优先在标题行处切分，避免截断段落和代码块
    """
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_size = 0
    in_code_block = False  # 跟踪是否在代码块内

    for line in lines:
        line_size = len(line) + 1  # +1 for newline

        # 检测代码块开始/结束
        if line.strip().startswith("```"):
            in_code_block = not in_code_block

        # 如果当前块已经够大，且不在代码块内，才允许切分
        if not in_code_block and current_size + line_size > chunk_size and current_size > 0:
            if line.startswith("#") or (current_size > chunk_size * 0.8):
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_size = 0

        current_chunk.append(line)
        current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    logger.info(f"内容分为 {len(chunks)} 个片段")
    return chunks
