"""HTTP API 请求负载模型（FastAPI 路由入参定义）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    requirement: str = ""
    baseline_url: str
    candidate_url: str
    allow_remote: bool = False
    allow_remote_writes: bool = False


class TaskPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    requirement: Optional[str] = None
    baseline_url: Optional[str] = None
    candidate_url: Optional[str] = None
    allow_remote: Optional[bool] = None
    allow_remote_writes: Optional[bool] = None


class EndpointIn(BaseModel):
    method: str
    path: str
    summary: str = ""


class OpenApiImportIn(BaseModel):
    spec_url: Optional[str] = None
    spec: Optional[dict] = None


class RuleVersionIn(BaseModel):
    checks: list[dict]
    note: str = ""
    confirm: bool = False


class RunIn(BaseModel):
    rule_version_id: Optional[int] = None


class SuggestIn(BaseModel):
    endpoint_summaries: Optional[list[str]] = None
