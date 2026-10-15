"""验收规则（checks）的数据模型与校验。

一条检查（check）= 一个验收点，包含若干步骤（steps），在 baseline 与 candidate
两个环境各执行一遍，比较结果。

规则结构（JSON）::

    {
      "id": "read-other-order-denied",            # 唯一标识
      "name": "用户 A 不能读取用户 B 的订单",
      "category": "existing_behavior",            # existing_behavior | new_requirement
      "expectation": "alice 读取 bob 的订单 102 必须返回 403",
      "expected_change_ref": null,                # existing_behavior 可引用一条 new_requirement
      "steps": [
        {
          "name": "登录获取 token",
          "method": "POST",
          "path": "/api/login",
          "headers": {},                          # 可选
          "json": {"username": "alice", "password": "alice-password"},   # 可选
          "timeout": 5,                           # 可选，秒
          "extract": {                            # 可选，从响应中提取变量供后续步骤使用
            "token": {"source": "json", "path": "$.access_token", "sensitive": true}
          },
          "assert": [
            {"type": "status", "op": "equals", "value": 200},
            {"type": "json", "path": "$.access_token", "op": "exists"}
          ]
        }
      ]
    }

注意：只有经过用户确认的规则版本才会被运行使用；运行过程永远不会修改规则。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

CHECK_CATEGORIES = ("existing_behavior", "new_requirement")
CATEGORY_LABELS = {
    "existing_behavior": "保持原有行为",
    "new_requirement": "新需求验收",
}

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

ASSERT_TYPES: dict[str, set[str]] = {
    "status": {"equals", "in"},
    "header": {"exists", "not_exists", "equals", "contains", "not_contains", "regex"},
    "json": {
        "exists",
        "not_exists",
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "type",
        "regex",
        "length_equals",
        "greater_than",
        "less_than",
        "in",
    },
    "text": {"contains", "not_contains", "regex"},
}

_JSON_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXTRACT_SOURCES = {"json", "header", "status", "text"}


def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def validate_check(check: Any, *, index: int = 0) -> list[str]:
    """校验单条检查，返回错误信息列表（为空表示合法）。"""
    errors: list[str] = []
    prefix = f"检查[{index}]"

    if not isinstance(check, dict):
        return [f"{prefix}: 必须是 JSON 对象"]

    cid = check.get("id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        _err(errors, f"{prefix}: id 必须是 1-64 位小写字母/数字/._- 组成的字符串（当前: {cid!r}）")
        cid = None
    else:
        prefix = f"检查 {cid}"

    name = check.get("name")
    if not isinstance(name, str) or not name.strip():
        _err(errors, f"{prefix}: name 不能为空")

    category = check.get("category", "existing_behavior")
    if category not in CHECK_CATEGORIES:
        _err(errors, f"{prefix}: category 必须是 {CHECK_CATEGORIES} 之一（当前: {category!r}）")

    ref = check.get("expected_change_ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        _err(errors, f"{prefix}: expected_change_ref 必须是检查 id 字符串或 null")

    if not isinstance(check.get("expectation", ""), str):
        _err(errors, f"{prefix}: expectation 必须是字符串")

    steps = check.get("steps")
    if not isinstance(steps, list) or not steps:
        _err(errors, f"{prefix}: steps 必须是非空数组")
        return errors

    for si, step in enumerate(steps):
        sp = f"{prefix} 步骤[{si}]"
        if not isinstance(step, dict):
            _err(errors, f"{sp}: 必须是 JSON 对象")
            continue
        method = step.get("method")
        if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
            _err(errors, f"{sp}: method 必须是 {HTTP_METHODS} 之一（当前: {method!r}）")
        path = step.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            _err(errors, f"{sp}: path 必须以 / 开头（当前: {path!r}）")
        headers = step.get("headers")
        if headers is not None:
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                _err(errors, f"{sp}: headers 必须是「字符串 -> 字符串」的 JSON 对象")
        timeout = step.get("timeout")
        if timeout is not None and (not isinstance(timeout, (int, float)) or not (0.1 <= float(timeout) <= 60)):
            _err(errors, f"{sp}: timeout 必须在 0.1~60 秒之间")

        extract = step.get("extract")
        if extract is not None:
            if not isinstance(extract, dict):
                _err(errors, f"{sp}: extract 必须是对象")
            else:
                for var, spec in extract.items():
                    if not _VAR_NAME_RE.match(str(var)):
                        _err(errors, f"{sp}: 提取变量名 {var!r} 非法")
                        continue
                    if not isinstance(spec, dict):
                        _err(errors, f"{sp}: extract.{var} 必须是对象")
                        continue
                    source = spec.get("source", "json")
                    if source not in _EXTRACT_SOURCES:
                        _err(errors, f"{sp}: extract.{var}.source 必须是 {sorted(_EXTRACT_SOURCES)} 之一")
                    if source in ("json", "text") and not isinstance(spec.get("path"), str):
                        _err(errors, f"{sp}: extract.{var} 使用 source={source} 时必须提供 path")
                    if source == "text":
                        try:
                            re.compile(str(spec.get("path")))
                        except re.error:
                            _err(errors, f"{sp}: extract.{var}.path 不是合法正则")
                    if "sensitive" in spec and not isinstance(spec["sensitive"], bool):
                        _err(errors, f"{sp}: extract.{var}.sensitive 必须是布尔值")

        asserts = step.get("assert")
        if not isinstance(asserts, list) or not asserts:
            _err(errors, f"{sp}: assert 必须是非空数组（至少一条断言）")
            continue
        for ai, a in enumerate(asserts):
            ap = f"{sp} 断言[{ai}]"
            if not isinstance(a, dict):
                _err(errors, f"{ap}: 必须是 JSON 对象")
                continue
            atype = a.get("type")
            if atype not in ASSERT_TYPES:
                _err(errors, f"{ap}: type 必须是 {sorted(ASSERT_TYPES)} 之一（当前: {atype!r}）")
                continue
            op = a.get("op")
            if op not in ASSERT_TYPES[atype]:
                _err(errors, f"{ap}: {atype} 支持的 op 为 {sorted(ASSERT_TYPES[atype])}（当前: {op!r}）")
                continue
            if atype == "json" and not isinstance(a.get("path"), str):
                _err(errors, f"{ap}: json 断言必须提供 path")
            if atype == "json" and a.get("path") == "":
                _err(errors, f"{ap}: json 断言的 path 不能为空字符串")
            if atype == "header":
                if not isinstance(a.get("name"), str) or not a.get("name"):
                    _err(errors, f"{ap}: header 断言必须提供 name")
            if op in ("exists", "not_exists"):
                continue
            if "value" not in a:
                _err(errors, f"{ap}: op={op} 必须提供 value")
                continue
            if atype == "status" and op == "equals" and not isinstance(a["value"], int):
                _err(errors, f"{ap}: status.equals 的 value 必须是整数状态码")
            if atype == "status" and op == "in":
                if not isinstance(a["value"], list) or not a["value"] or not all(isinstance(v, int) for v in a["value"]):
                    _err(errors, f"{ap}: status.in 的 value 必须是整数数组")
            if atype == "json" and op == "type" and a["value"] not in _JSON_TYPES:
                _err(errors, f"{ap}: json.type 的 value 必须是 {sorted(_JSON_TYPES)} 之一")
            if atype == "json" and op == "length_equals" and not isinstance(a["value"], int):
                _err(errors, f"{ap}: json.length_equals 的 value 必须是整数")
            if atype == "json" and op in ("greater_than", "less_than") and not isinstance(a["value"], (int, float)):
                _err(errors, f"{ap}: json.{op} 的 value 必须是数字")
    return errors


def validate_checks(checks: Any) -> list[str]:
    """校验检查列表，额外检查 id 唯一性与 expected_change_ref 指向。"""
    if not isinstance(checks, list):
        return ["checks 必须是数组"]
    errors: list[str] = []
    for i, c in enumerate(checks):
        errors.extend(validate_check(c, index=i))
    ids = [c.get("id") for c in checks if isinstance(c, dict)]
    seen = set()
    for cid in ids:
        if cid in seen:
            errors.append(f"检查 id 重复: {cid}")
        seen.add(cid)
    by_id = {c.get("id"): c for c in checks if isinstance(c, dict)}
    for c in checks:
        if not isinstance(c, dict):
            continue
        ref = c.get("expected_change_ref")
        if ref:
            target = by_id.get(ref)
            if target is None:
                errors.append(f"检查 {c.get('id')}: expected_change_ref 指向不存在的检查 {ref}")
            elif target.get("category") != "new_requirement":
                errors.append(f"检查 {c.get('id')}: expected_change_ref 只能指向「新需求验收」类检查")
    return errors


# ------------------------------------------------------------------ API 模型


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
