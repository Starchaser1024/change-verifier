"""python -m verifier 启动改动验收器（默认 http://127.0.0.1:8622）。

启动时自动读取项目根的 `.env`（如 LLM_API_KEY / 端口），真实环境变量优先于 `.env`。
"""

import os

import uvicorn

from .envfile import load_env

if __name__ == "__main__":
    loaded = load_env()
    if loaded:
        print(f"[verifier] 已从 .env 读取：{', '.join(sorted(loaded))}")
    host = os.environ.get("VERIFIER_HOST", "127.0.0.1")
    port = int(os.environ.get("VERIFIER_PORT", "8622"))
    uvicorn.run("verifier.main:app", host=host, port=port, log_level="info")
