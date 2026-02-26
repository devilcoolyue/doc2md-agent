"""
共享配置加载器，供 CLI 和 Web 服务共用。
"""

import os
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def load_config(config_path: str = None, provider_override: str = None) -> dict:
    """加载配置文件并应用 provider 覆盖。"""
    if config_path is None:
        search_paths = [
            Path("config.yaml"),
            Path("config.yml"),
            Path.home() / ".doc2md" / "config.yaml",
        ]
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break

    if config_path and Path(config_path).exists():
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
        logger.info("已加载配置: %s", config_path)
    else:
        config = {
            "provider": "deepseek",
            "providers": {
                "deepseek": {
                    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                    "max_tokens": 16000,
                    "temperature": 0,
                }
            },
            "conversion": {
                "chunk_size": 8000,
                "chunk_strategy": "section",
                "generate_toc": True,
                "deterministic_toc": True,
                "strict_mode": True,
                "max_chunk_retries": 2,
                "allow_partial_on_chunk_failure": True,
                "allow_partial_on_validation_failure": True,
                "min_content_token_coverage": 0.82,
                "min_content_char_ratio": 0.62,
                "content_guard_min_tokens": 20,
                "image_dir": "images",
            },
        }
        logger.warning("未找到配置文件，使用默认配置（deepseek）")

    if provider_override:
        config["provider"] = provider_override

    return config
