"""FastAPI 路由汇总：任务/接口清单、不可变规则版本与运行报告。"""

from __future__ import annotations

from fastapi import APIRouter

from . import rules, runs, tasks

api_router = APIRouter()
api_router.include_router(tasks.router)
api_router.include_router(rules.router)
api_router.include_router(runs.router)
