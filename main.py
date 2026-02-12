#!/usr/bin/env python3
"""CLI 兼容入口，转发到 backend.cli。"""

from backend.cli import cli


if __name__ == "__main__":
    cli()
