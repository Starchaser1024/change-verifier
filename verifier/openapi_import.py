"""导入 OpenAPI 文件（URL 或直接粘贴 JSON），提取接口清单。

只提取方法与路径，用于辅助编写规则；不会自动生成「永远通过」的假检查。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def extract_endpoints(spec: dict) -> list[dict]:
    if not isinstance(spec, dict):
        raise ValueError("OpenAPI 内容必须是 JSON 对象")
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("不是有效的 OpenAPI 文件：缺少 paths 字段")
    endpoints: list[dict] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            summary = ""
            if isinstance(op, dict):
                summary = str(op.get("summary") or op.get("operationId") or op.get("description") or "")
            endpoints.append({"method": method.upper(), "path": str(path), "summary": summary.strip()})
    endpoints.sort(key=lambda e: (e["path"], e["method"]))
    return endpoints


def fetch_spec(url: str, timeout: float = 10.0) -> dict:
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    content_type = (resp.headers.get("content-type") or "").lower()
    if "json" in content_type or url.lower().endswith((".json",)):
        return resp.json()
    # 一些服务直接返回 JSON 但 content-type 不标准
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ValueError(f"无法解析 OpenAPI 内容（既不是 JSON/yaml 内容的标准格式）: {e}") from e
