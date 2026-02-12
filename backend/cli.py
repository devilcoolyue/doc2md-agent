#!/usr/bin/env python3
"""
Doc2MD Agent - 命令行入口

用法:
    python main.py convert  接口说明书.docx  -o output/
    python main.py convert  接口说明书.docx  -o output/ --provider openai
    python main.py convert  接口说明书.docx  -o output/ --provider ollama
    python main.py providers                              # 列出支持的 AI 提供商
"""

import os
import sys
import logging
import shutil
import click
from pathlib import Path
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from backend.agent import Doc2MDAgent
from backend.config_loader import load_config

console = Console()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
logger = logging.getLogger(__name__)


@click.group()
def cli():
    """Doc2MD Agent - 智能文档转 Markdown 工具"""
    pass


@cli.command()
@click.argument("input_file", type=click.Path(exists=True))
@click.option("-o", "--output", "output_dir", default=None, help="输出目录（默认: ./output）")
@click.option("-c", "--config", "config_path", default=None, help="配置文件路径")
@click.option("-p", "--provider", "provider", default=None,
              type=click.Choice(["openai", "anthropic", "deepseek", "zhipu", "qwen", "ollama", "custom"]),
              help="AI 提供商（覆盖配置文件）")
@click.option("--pack/--no-pack", default=True, help="是否打包为 tar.gz")
def convert(input_file: str, output_dir: str, config_path: str, provider: str, pack: bool):
    """转换 docx 文件为美观的 Markdown"""

    input_path = Path(input_file)

    # 默认输出目录
    if output_dir is None:
        output_dir = f"./output/{input_path.stem}"

    output_path = Path(output_dir)

    # 显示启动信息
    console.print(Panel.fit(
        f"[bold cyan]Doc2MD Agent[/]\n\n"
        f"📄 输入: {input_path}\n"
        f"📂 输出: {output_path}",
        title="文档智能转换",
        border_style="blue",
    ))

    # 加载配置
    config = load_config(config_path, provider)

    provider_name = config.get("provider", "unknown")
    model_name = config.get("providers", {}).get(provider_name, {}).get("model", "unknown")
    console.print(f"🤖 AI 提供商: [bold green]{provider_name}[/]  模型: [bold]{model_name}[/]\n")

    # 检查 API Key
    api_key = config.get("providers", {}).get(provider_name, {}).get("api_key", "")
    if not api_key or api_key.startswith("sk-xxx"):
        env_key = os.environ.get("DOC2MD_API_KEY", "")
        if not env_key:
            console.print("[bold red]❌ 错误: 未配置 API Key[/]")
            console.print("请在 config.yaml 中配置，或设置环境变量:")
            console.print(f"  export DOC2MD_API_KEY='your-key-here'")
            sys.exit(1)

    # 执行转换
    try:
        agent = Doc2MDAgent(config)
        output_file, usage = agent.convert(str(input_path), str(output_path))

        # 打包
        if pack:
            archive_name = f"{input_path.stem}"
            archive_path = shutil.make_archive(
                base_name=str(output_path.parent / archive_name),
                format="gztar",
                root_dir=str(output_path.parent),
                base_dir=output_path.name,
            )
            console.print(f"\n📦 打包完成: {archive_path}")

        # 显示 token 用量
        if usage and usage.get("total_tokens", 0) > 0:
            usage_table = Table(title="Token 用量统计", border_style="cyan")
            usage_table.add_column("项目", style="bold")
            usage_table.add_column("数值", justify="right")
            usage_table.add_row("输入 tokens", f"{usage['prompt_tokens']:,}")
            usage_table.add_row("输出 tokens", f"{usage['completion_tokens']:,}")
            usage_table.add_row("总计 tokens", f"[bold]{usage['total_tokens']:,}[/]")
            currency = usage.get("currency", "$")
            cost = usage.get("total_cost", 0.0)
            usage_table.add_row("费用估算", f"[bold green]{currency}{cost:.4f}[/]")
            console.print()
            console.print(usage_table)
            # 未在配置文件中自定义价格时，提示内置定价仅供参考
            pricing_conf = config.get("providers", {}).get(provider_name, {}).get("pricing")
            if not pricing_conf:
                console.print("[dim]* 费用基于内置定价表估算，仅供参考[/]")

        console.print(Panel.fit(
            f"[bold green]✅ 转换成功！[/]\n\n"
            f"Markdown: {output_file}\n"
            f"输出目录: {output_path}",
            border_style="green",
        ))

    except Exception as e:
        console.print(f"\n[bold red]❌ 转换失败: {e}[/]")
        logger.exception("详细错误信息")
        sys.exit(1)


@cli.command()
def providers():
    """列出支持的 AI 提供商"""
    table = Table(title="支持的 AI 提供商")
    table.add_column("名称", style="cyan bold")
    table.add_column("默认模型", style="green")
    table.add_column("API 格式", style="yellow")
    table.add_column("说明")

    data = [
        ("openai",    "gpt-4o",          "OpenAI",       "OpenAI 官方"),
        ("anthropic", "claude-sonnet-4-20250514", "Anthropic",    "Anthropic Claude"),
        ("deepseek",  "deepseek-chat",   "OpenAI 兼容",  "深度求索，性价比高"),
        ("zhipu",     "glm-4-plus",      "OpenAI 兼容",  "智谱 AI（GLM 系列）"),
        ("qwen",      "qwen-max",        "OpenAI 兼容",  "通义千问"),
        ("ollama",    "qwen2.5:32b",     "OpenAI 兼容",  "本地部署，无需 API Key"),
        ("custom",    "自定义",           "OpenAI 兼容",  "任意 OpenAI 兼容接口"),
    ]

    for name, model, api_type, desc in data:
        table.add_row(name, model, api_type, desc)

    console.print(table)
    console.print("\n使用方式:")
    console.print("  python main.py convert doc.docx -p deepseek")
    console.print("  python main.py convert doc.docx -p ollama")


@cli.command()
def init():
    """生成默认配置文件"""
    config_file = Path("config.yaml")
    example_file = Path(__file__).resolve().parent.parent / "config.example.yaml"

    if config_file.exists():
        if not click.confirm("config.yaml 已存在，是否覆盖?"):
            return

    if example_file.exists():
        shutil.copy(example_file, config_file)
    else:
        # 内联生成
        config_file.write_text(
            "# Doc2MD Agent 配置\n"
            "# 详见 config.example.yaml\n\n"
            "provider: deepseek\n\n"
            "providers:\n"
            "  deepseek:\n"
            '    api_key: "sk-xxx"\n'
            '    base_url: "https://api.deepseek.com/v1"\n'
            '    model: "deepseek-chat"\n'
            "    max_tokens: 16000\n",
            encoding="utf-8",
        )

    console.print(f"[green]✅ 已生成配置文件: {config_file}[/]")
    console.print("请编辑 config.yaml 填入你的 API Key")


if __name__ == "__main__":
    cli()
