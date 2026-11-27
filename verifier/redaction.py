"""敏感信息脱敏。

原则：证据里保留「哪些字段被脱敏」的结构，但不保留真实值。
- Authorization / Cookie / Set-Cookie 等请求响应头整体替换为 [REDACTED]。
- JSON/文本中匹配敏感名字的字段值替换为 [REDACTED]。
- 运行中动态提取出来的敏感变量值（如登录 token），在后续证据中做字面替换。
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

REDACTED = "[REDACTED]"

_SENSITIVE_NAME_RE = re.compile(
    r"(password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization|credential|cookie|session)",
    re.IGNORECASE,
)

_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
}


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME_RE.search(str(name)))


def redact_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if str(k).lower() in _SENSITIVE_HEADERS or is_sensitive_name(str(k)):
            out[str(k)] = REDACTED
        else:
            out[str(k)] = str(v)
    return out


def mask_text(text: str, secret_values: Iterable[str] = ()) -> str:
    """把出现过的敏感变量值从文本中替换掉。"""
    if not isinstance(text, str):
        return text
    masked = text
    for value in secret_values:
        if value and len(str(value)) >= 1:
            masked = masked.replace(str(value), REDACTED)
    return masked


def redact_obj(obj: Any, secret_values: Iterable[str] = (), _depth: int = 0) -> Any:
    """递归脱敏：敏感键名替换值；字符串中的敏感变量值做字面替换。"""
    if _depth > 12:
        return "<嵌套过深，已省略>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_sensitive_name(str(k)):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact_obj(v, secret_values, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, secret_values, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj, secret_values)
    return obj


def redact_url(url: str, secret_values: Iterable[str] = ()) -> str:
    """移除 URL 凭据并对路径与查询字符串中的敏感键名及动态机密值脱敏。"""
    if not isinstance(url, str):
        return url
    secrets_list = [str(s) for s in secret_values if s]
    try:
        parts = urlsplit(url)
    except ValueError:
        cleaned = "".join(ch if (ord(ch) >= 0x20 and ord(ch) != 0x7F) else "?" for ch in url)
        return mask_text(cleaned, secrets_list)

    netloc = parts.netloc
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"

    safe_path = mask_text(parts.path, secrets_list)
    safe_query = parts.query
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        encoded_pairs: list[str] = []
        for k, v in pairs:
            safe_k = quote(mask_text(k, secrets_list), safe="[]")
            if is_sensitive_name(k):
                safe_v = REDACTED
            else:
                safe_v = quote(mask_text(v, secrets_list), safe="[]")
            encoded_pairs.append(f"{safe_k}={safe_v}")
        safe_query = "&".join(encoded_pairs)

    safe_fragment = mask_text(parts.fragment, secrets_list) if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, safe_path, safe_query, safe_fragment))
