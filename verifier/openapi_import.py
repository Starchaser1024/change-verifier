"""导入 OpenAPI 文件（URL 或直接粘贴 JSON），提取接口清单。

只提取方法与路径，用于辅助编写规则；不会自动生成「永远通过」的假检查。
"""

from __future__ import annotations

import json
from urllib.parse import urljoin

import httpx

from .safety import check_spec_url_safety

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
MAX_SPEC_REDIRECTS = 5


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


def fetch_spec(url: str, *, allow_remote: bool = False, timeout: float = 10.0) -> dict:
    current_url = check_spec_url_safety(url, allow_remote=allow_remote)
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        for hop in range(MAX_SPEC_REDIRECTS + 1):
            resp = client.get(current_url)
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    break
                if hop >= MAX_SPEC_REDIRECTS:
                    raise ValueError(f"OpenAPI 地址重定向次数过多（超过 {MAX_SPEC_REDIRECTS} 次）")
                next_url = urljoin(current_url, location.strip())
                current_url = check_spec_url_safety(
                    next_url, allow_remote=allow_remote, label="OpenAPI 重定向目标"
                )
                continue
            break

    resp.raise_for_status()
    try:
        data = json.loads(resp.text)
    except json.JSONDecodeError as e:
        raise ValueError(f"无法解析 OpenAPI 内容（仅支持标准 OpenAPI JSON 格式）: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("OpenAPI 内容必须是 JSON 对象")
    return data
