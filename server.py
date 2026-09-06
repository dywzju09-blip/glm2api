"""GLM Studio 启动入口：python server.py"""

from __future__ import annotations

import os

import uvicorn

from glmstudio.app import create_app

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
