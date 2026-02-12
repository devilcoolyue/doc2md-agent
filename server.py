#!/usr/bin/env python3
"""Web 服务兼容入口，转发到 backend.server。"""

from backend.server import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.server:app", host="0.0.0.0", port=9999, reload=False)
