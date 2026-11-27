"""HTTP 检查执行器：在单个环境上按顺序执行一条检查的所有步骤。

- 支持请求方法、路径、查询参数（params）、请求头、JSON 请求体或表单请求体（form）、超时。
- 支持从响应中提取变量（json / header / status / text-regex）供后续步骤引用。
- 状态码、响应头、JSON 字段、文本断言。
- 网络层错误（超时、连接失败、传输错误）归类为「无法判断」，绝不当作业务失败。
- 所有记录在写入前脱敏（Authorization/Cookie/密码/token/敏感查询参数与表单字段）。
"""

from __future__ import annotations

import json as jsonlib
import re
import time
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from .jsonpath import MISSING, get_path
from .redaction import REDACTED, is_sensitive_name, mask_text, redact_headers, redact_obj, redact_url
from .safety import SafetyError, validate_rendered_path

DEFAULT_TIMEOUT_S = 5.0
MAX_TEXT_STORE = 6000
MAX_JSON_STORE = 20000
MAX_HEADERS_STORE = 20

# 检查结果状态
PASSED = "passed"
FAILED = "failed"
UNVERIFIABLE = "unverifiable"

# 步骤错误类型 -> 中文说明
NETWORK_ERROR_LABELS = {
    "timeout": "请求超时",
    "connect": "无法连接服务",
    "transport": "网络传输错误",
    "cancel": "运行已取消",
    "config": "规则配置/安全错误",
}


class RenderError(Exception):
    """模板变量未定义或渲染失败。"""

    def __init__(self, name: str):
        super().__init__(f"变量 {{{{{name}}}}} 未定义")
        self.name = name


_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(value: Any, variables: dict) -> Any:
    """递归渲染 {{var}} 占位符。

    若整个字符串就是一个占位符，则返回变量的原始类型（数字/布尔等），
    否则做字符串插值。
    """
    if isinstance(value, str):
        m = _VAR_RE.fullmatch(value)
        if m:
            name = m.group(1)
            if name not in variables:
                raise RenderError(name)
            return variables[name]

        def _sub(mm: re.Match) -> str:
            name = mm.group(1)
            if name not in variables:
                raise RenderError(name)
            return str(variables[name])

        return _VAR_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    return value


def url_without_credentials(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return "".join(ch if (ord(ch) >= 0x20 and ord(ch) != 0x7F) else "?" for ch in url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _shorten(value: Any, limit: int = 300) -> str:
    if value is MISSING:
        return "<字段不存在>"
    try:
        s = jsonlib.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(value)
    if len(s) > limit:
        return s[:limit] + "…"
    return s


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def evaluate_assertion(a: dict, resp: httpx.Response, resp_json: Any, resp_text: str) -> dict:
    """执行单条断言，返回 {desc, ok, expected, actual}。"""
    atype = a["type"]
    op = a["op"]
    expected = a.get("value")
    actual: Any = MISSING
    ok = False

    if atype == "status":
        actual = resp.status_code
        desc = f"状态码 {op} {expected}"
        if op == "equals":
            ok = actual == expected
        elif op == "in":
            ok = actual in expected
    elif atype == "header":
        name = a["name"]
        actual = resp.headers.get(name)
        desc = f"响应头 {name} {op} {expected}"
        if op == "exists":
            ok = actual is not None
        elif op == "not_exists":
            ok = actual is None
        elif actual is None:
            ok = False
        elif op == "equals":
            ok = str(actual) == str(expected)
        elif op == "contains":
            ok = str(expected) in str(actual)
        elif op == "not_contains":
            ok = str(expected) not in str(actual)
        elif op == "regex":
            ok = re.search(str(expected), str(actual)) is not None
    elif atype == "json":
        path = a["path"]
        actual = get_path(resp_json, path)
        desc = f"响应 JSON {path} {op} {expected if op not in ('exists', 'not_exists') else ''}".rstrip()
        if op == "exists":
            ok = actual is not MISSING
        elif op == "not_exists":
            ok = actual is MISSING
        elif actual is MISSING:
            ok = False
        elif op == "equals":
            ok = actual == expected
        elif op == "not_equals":
            ok = actual != expected
        elif op == "contains":
            if isinstance(actual, str):
                ok = str(expected) in actual
            elif isinstance(actual, (list, tuple)):
                ok = expected in actual
            elif isinstance(actual, dict):
                ok = expected in actual
        elif op == "not_contains":
            if isinstance(actual, str):
                ok = str(expected) not in actual
            elif isinstance(actual, (list, tuple)):
                ok = expected not in actual
            elif isinstance(actual, dict):
                ok = expected not in actual
            else:
                ok = True
        elif op == "type":
            ok = _json_type(actual) == expected or (expected == "number" and _json_type(actual) in ("number", "integer"))
        elif op == "regex":
            ok = isinstance(actual, str) and re.search(str(expected), actual) is not None
        elif op == "length_equals":
            ok = isinstance(actual, (list, tuple, str, dict)) and len(actual) == expected
        elif op == "length_less_than":
            ok = isinstance(actual, (list, tuple, str, dict)) and len(actual) < expected
        elif op == "length_greater_than":
            ok = isinstance(actual, (list, tuple, str, dict)) and len(actual) > expected
        elif op == "greater_than":
            ok = isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > expected
        elif op == "less_than":
            ok = isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual < expected
        elif op == "in":
            ok = actual in expected if isinstance(expected, (list, tuple)) else False
    elif atype == "text":
        actual = resp_text if resp_text is not None else ""
        desc = f"响应文本 {op} {_shorten(expected)}"
        if op == "contains":
            ok = str(expected) in actual
        elif op == "not_contains":
            ok = str(expected) not in actual
        elif op == "regex":
            ok = re.search(str(expected), actual) is not None
    else:  # pragma: no cover - 校验器已拦截
        desc = f"未知断言类型 {atype}"

    return {
        "desc": desc,
        "ok": bool(ok),
        "expected": _shorten(expected),
        "actual": _shorten(actual),
    }


def _capture_response(resp: httpx.Response, elapsed_ms: int, secret_values: list[str]) -> tuple[dict, Any, str]:
    """把响应整理为脱敏后的证据。返回 (evidence, resp_json, resp_text)。"""
    stored_text: Optional[str] = None
    stored_json: Any = None
    is_json = False
    raw_text = resp.text
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" in ctype:
        try:
            parsed = resp.json()
            is_json = True
            stored_json = redact_obj(parsed, secret_values)
            serialized = jsonlib.dumps(stored_json, ensure_ascii=False)
            if len(serialized) > MAX_JSON_STORE:
                stored_json = {"_说明": "响应体过大，已截断", "_预览": serialized[:MAX_JSON_STORE]}
                is_json = True
        except Exception:
            is_json = False
    resp_json = None
    if is_json:
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = None
    if not is_json:
        masked = mask_text(raw_text, secret_values)
        truncated = len(masked) > MAX_TEXT_STORE
        stored_text = masked[:MAX_TEXT_STORE] + ("…[已截断]" if truncated else "")
    else:
        truncated = False

    headers = redact_headers(dict(resp.headers))
    if len(headers) > MAX_HEADERS_STORE:
        headers = dict(list(headers.items())[:MAX_HEADERS_STORE])
        headers["_说明"] = "响应头过多，已截断"

    evidence = {
        "status": resp.status_code,
        "headers": headers,
        "json": stored_json if is_json else None,
        "text": None if is_json else stored_text,
        "is_json": is_json,
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }
    return evidence, resp_json, raw_text


def execute_check(
    check: dict,
    env_label: str,
    base_url: str,
    run_nonce: str,
    check_index: int,
    default_timeout: float = DEFAULT_TIMEOUT_S,
    cancel_event=None,
) -> dict:
    """在单个环境上执行一条检查。

    返回:
        {
          "env": env_label,
          "status": passed | failed | unverifiable,
          "reason": str,
          "steps": [evidence...]
        }
    """
    variables: dict[str, Any] = {
        "nonce": f"{run_nonce}-{check_index}",
        "check_id": check.get("id"),
        "env": env_label,
    }
    secret_values: list[str] = []
    step_records: list[dict] = []
    status = PASSED
    reason = "全部步骤与断言通过"

    base = base_url.rstrip("/")

    def make_error_record(
        step: dict,
        kind: str,
        message: str,
        elapsed_ms: int = 0,
        *,
        rendered_path: Any = None,
        rendered_params: Any = None,
        rendered_form: Any = None,
    ) -> dict:
        raw_path = str(rendered_path if rendered_path is not None else step.get("path", ""))
        safe_path = redact_url(mask_text(raw_path, secret_values), secret_values)
        raw_url = safe_path if "://" in safe_path else (base + safe_path)
        return {
            "name": step.get("name") or f"{step.get('method')} {step.get('path')}",
            "request": {
                "method": step.get("method"),
                "path": safe_path,
                "url": redact_url(url_without_credentials(raw_url), secret_values),
                "params": redact_obj(
                    rendered_params if rendered_params is not None else step.get("params"),
                    secret_values,
                ),
                "headers": redact_headers(step.get("headers") or {}),
                "json": redact_obj(step.get("json"), secret_values),
                "form": redact_obj(
                    rendered_form if rendered_form is not None else step.get("form"),
                    secret_values,
                ),
            },
            "response": None,
            "assertions": [],
            "extracted": {},
            "error": {"kind": kind, "message": mask_text(message, secret_values), "label": NETWORK_ERROR_LABELS.get(kind, kind)},
        }

    with httpx.Client(base_url=base, follow_redirects=False) as client:
        for step in check["steps"]:
            step_name = step.get("name") or f"{step.get('method')} {step.get('path')}"
            if cancel_event is not None and cancel_event.is_set():
                status = UNVERIFIABLE
                reason = "运行已取消，检查未执行完"
                step_records.append(make_error_record(step, "cancel", "运行已取消，该步骤未执行"))
                break

            # 渲染模板
            try:
                method = str(step["method"]).upper()
                path = render(step["path"], variables)
                params = render(step["params"], variables) if step.get("params") is not None else None
                headers = render(step.get("headers") or {}, variables)
                body = render(step.get("json"), variables) if step.get("json") is not None else None
                form_body = render(step["form"], variables) if step.get("form") is not None else None
                # 断言里的 value 同样支持 {{变量}}（整个占位符时保持原始类型，如数字 id）
                rendered_asserts = []
                for a in step.get("assert", []):
                    a2 = dict(a)
                    if "value" in a2:
                        a2["value"] = render(a2["value"], variables)
                    rendered_asserts.append(a2)
            except RenderError as e:
                status = FAILED
                reason = f"规则配置错误：变量 {{{{{e.name}}}}} 未定义（通常由前置步骤提取失败或拼写错误导致）"
                step_records.append(make_error_record(step, "config", reason))
                break

            try:
                path = validate_rendered_path(path)
            except SafetyError as e:
                status = FAILED
                reason = f"规则配置/安全错误：步骤「{step_name}」{mask_text(str(e), secret_values)}"
                step_records.append(
                    make_error_record(
                        step,
                        "config",
                        reason,
                        rendered_path=path,
                        rendered_params=params,
                        rendered_form=form_body,
                    )
                )
                break

            if body is not None and form_body is not None:
                status = FAILED
                reason = f"规则配置错误：步骤「{step_name}」不能同时指定 json 与 form 请求体"
                step_records.append(
                    make_error_record(
                        step,
                        "config",
                        reason,
                        rendered_path=path,
                        rendered_params=params,
                        rendered_form=form_body,
                    )
                )
                break

            timeout = float(step.get("timeout", default_timeout))
            started = time.perf_counter()
            try:
                resp = client.request(
                    method,
                    path,
                    params=params,
                    headers=headers or None,
                    json=body,
                    data=form_body,
                    timeout=timeout,
                )
            except httpx.TimeoutException:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                status = UNVERIFIABLE
                reason = f"步骤「{step_name}」请求超时（{timeout}s），属于环境/执行问题，无法判断业务结果"
                step_records.append(
                    make_error_record(
                        step,
                        "timeout",
                        f"请求 {method} {path} 超时（{timeout}s）",
                        elapsed_ms,
                        rendered_path=path,
                        rendered_params=params,
                        rendered_form=form_body,
                    )
                )
                break
            except httpx.ConnectError as e:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                status = UNVERIFIABLE
                reason = f"步骤「{step_name}」无法连接 {base}，服务不可达，无法判断业务结果"
                step_records.append(
                    make_error_record(
                        step,
                        "connect",
                        f"连接失败: {e}",
                        elapsed_ms,
                        rendered_path=path,
                        rendered_params=params,
                        rendered_form=form_body,
                    )
                )
                break
            except httpx.HTTPError as e:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                status = UNVERIFIABLE
                reason = f"步骤「{step_name}」网络/协议错误：{e}"
                step_records.append(
                    make_error_record(
                        step,
                        "transport",
                        str(e),
                        elapsed_ms,
                        rendered_path=path,
                        rendered_params=params,
                        rendered_form=form_body,
                    )
                )
                break
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            evidence, resp_json, resp_text = _capture_response(resp, elapsed_ms, secret_values)
            record = {
                "name": step_name,
                "request": {
                    "method": method,
                    "path": redact_url(path, secret_values),
                    "url": redact_url(str(resp.request.url), secret_values),
                    "params": redact_obj(params, secret_values),
                    "headers": redact_headers(headers or {}),
                    "json": redact_obj(body, secret_values),
                    "form": redact_obj(form_body, secret_values),
                },
                "response": evidence,
                "assertions": [],
                "extracted": {},
                "error": None,
            }

            # 断言
            assertion_results = [evaluate_assertion(a, resp, resp_json, resp_text) for a in rendered_asserts]
            record["assertions"] = assertion_results
            failed_assertions = [ar for ar in assertion_results if not ar["ok"]]

            # 提取变量（即使断言失败也先提取，便于记录证据；失败即中断）
            extracted: dict[str, Any] = {}
            extract_error = None
            for var_name, spec in (step.get("extract") or {}).items():
                source = spec.get("source", "json")
                value: Any = MISSING
                if source == "json":
                    value = get_path(resp_json, spec.get("path", ""))
                elif source == "header":
                    value = resp.headers.get(spec.get("path") or spec.get("name") or "")
                elif source == "status":
                    value = resp.status_code
                elif source == "text":
                    m = re.search(str(spec.get("path")), resp_text or "")
                    value = m.group(1) if (m and m.groups()) else (m.group(0) if m else MISSING)
                if value is MISSING:
                    extract_error = f"未能从步骤「{step_name}」响应中提取变量 {var_name}（{source}:{spec.get('path', '')}）"
                    break
                variables[var_name] = value
                sensitive = spec.get("sensitive", True) or is_sensitive_name(var_name)
                extracted[var_name] = REDACTED if sensitive else value
                if sensitive:
                    secret_values.append(str(value))
            record["extracted"] = extracted
            step_records.append(record)

            if failed_assertions:
                first = failed_assertions[0]
                status = FAILED
                reason = f"步骤「{step_name}」断言失败：{first['desc']}（期望 {first['expected']}，实际 {first['actual']}）"
                break
            if extract_error:
                status = FAILED
                reason = extract_error
                record["error"] = {"kind": "extract", "message": extract_error, "label": "变量提取失败"}
                break

    return {"env": env_label, "status": status, "reason": reason, "steps": step_records}


def execute_check_against_envs(
    check: dict,
    baseline_url: str,
    candidate_url: str,
    run_nonce: str,
    check_index: int,
    default_timeout: float = DEFAULT_TIMEOUT_S,
    cancel_event=None,
) -> tuple[dict, dict]:
    """同一检查在两个环境上分别执行，使用相同的 nonce 保证测试数据一致。"""
    b = execute_check(check, "baseline", baseline_url, run_nonce, check_index, default_timeout, cancel_event)
    c = execute_check(check, "candidate", candidate_url, run_nonce, check_index, default_timeout, cancel_event)
    return b, c
