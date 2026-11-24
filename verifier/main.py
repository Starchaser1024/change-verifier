"""改动验收器 FastAPI 应用装配与静态页面挂载。

业务路由按领域拆分在 `verifier/routes/` 下：
- `tasks.py`: 验收任务、演示任务与接口清单（含 OpenAPI 导入）
- `rules.py`: 不可变规则版本管理与可选 LLM 建议
- `runs.py`: 双环境验收运行、取消与脱敏报告导出
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import init_db
from .envfile import load_env
from .routes import api_router

STATIC_DIR = Path(__file__).parent / "static"

# 约定：本地配置（尤其模型 Key）统一放在项目根的 .env，不散落在 shell 里。
# 在应用模块导入时兜底读一次，保证任何启动方式都生效：
#   python -m verifier / change-verifier / uvicorn verifier.main:app / run_demo.py
# 真实环境变量优先于 .env；文件不存在时静默跳过。
load_env()


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用实例。"""
    application = FastAPI(title="改动验收器", version=__version__)
    init_db()
    application.include_router(api_router)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return application


app = create_app()
