"""python -m verifier 启动改动验收器（默认 http://127.0.0.1:8622）。"""

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("VERIFIER_HOST", "127.0.0.1")
    port = int(os.environ.get("VERIFIER_PORT", "8622"))
    uvicorn.run("verifier.main:app", host=host, port=port, log_level="info")
