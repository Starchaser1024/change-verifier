"""目标环境安全校验：URL 合法性与「默认仅限本地环境 / 远程写入二次确认」守卫。

无论是 FastAPI 网页接口还是无头 CLI（CI 门禁），都共用同一套安全策略，
防止误向生产或远程业务环境发送测试请求（尤其是写入类请求）。
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class SafetyError(ValueError):
    """目标地址不合法或违反本地/远程安全限制。"""


def validate_target_url(url: str, label: str) -> str:
    """校验并返回去掉末尾斜杠的 http(s) URL；非法时抛出 SafetyError。"""
    if not isinstance(url, str) or not url.strip():
        raise SafetyError(f"{label}地址不能为空")
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SafetyError(f"{label}地址必须是合法的 http(s) URL：{url!r}")
    return cleaned.rstrip("/")


def is_loopback(host: Optional[str]) -> bool:
    """判断主机名是否为本机回环地址。"""
    if not host:
        return False
    normalized = host.lower().strip("[]")
    return normalized in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or normalized.startswith("127.")


def check_run_safety(
    baseline_url: str,
    candidate_url: str,
    checks: list[dict],
    *,
    allow_remote: bool = False,
    allow_remote_writes: bool = False,
) -> None:
    """执行前安全门禁：默认只允许本地目标；远程目标与远程写入需分别显式放行。"""
    b_url = validate_target_url(baseline_url, "修改前")
    c_url = validate_target_url(candidate_url, "修改后")
    targets = [("修改前", b_url), ("修改后", c_url)]
    remote_hosts = [(label, url) for label, url in targets if not is_loopback(urlparse(url).hostname)]

    if remote_hosts and not allow_remote:
        detail = "、".join(f"{label}({urlparse(url).hostname})" for label, url in remote_hosts)
        raise SafetyError(
            f"安全限制：{detail} 不是本地地址。默认只允许操作本地（127.0.0.1/localhost）环境，"
            "避免向真实业务系统写入测试数据。如确需远程环境，请在任务设置中显式开启「允许远程环境」（CLI 使用 --allow-remote）。"
        )

    if remote_hosts and allow_remote:
        has_unsafe = any(
            str(step.get("method", "")).upper() in UNSAFE_METHODS
            for check in checks
            for step in check.get("steps", [])
        )
        if has_unsafe and not allow_remote_writes:
            detail = "、".join(f"{label}({urlparse(url).hostname})" for label, url in remote_hosts)
            raise SafetyError(
                f"安全限制：规则包含写入类请求（{', '.join(UNSAFE_METHODS)}），目标是远程环境 {detail}。"
                "请显式开启「允许远程写入」（CLI 使用 --allow-remote-writes）后再运行，避免误写真实数据。"
            )
