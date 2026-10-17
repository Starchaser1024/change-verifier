"""可选的 LLM 辅助：根据需求与接口清单提出检查建议。

原则：
- 未配置模型 API Key 时，明确报错，绝不伪造模型输出。
- 模型输出只作为「建议」，必须经用户编辑并确认后才会保存为规则版本。
- 任何模型调用失败都会把错误原样暴露给用户。

环境变量：
- LLM_API_KEY / OPENAI_API_KEY / OPENCODE_API_KEY   任一存在即视为已配置
- LLM_BASE_URL   默认 https://api.openai.com/v1
- LLM_MODEL      默认 gpt-4o-mini

这些变量可以写在项目根的 `.env` 里（见 verifier/envfile.py）；真实环境变量优先于 `.env`。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

import httpx

from . import envfile
from .models import validate_checks

SYSTEM_PROMPT = """你是一个谨慎的 API 验收规则助手。
用户会给出「修改需求」、接口清单和已有检查。你的任务只是提出可执行的验收检查建议。

要求：
1. 只能依据用户明确给出的需求与接口清单提出建议，不要臆造接口或业务规则。
2. 每条检查必须包含字段：id(小写字母/数字/.-_, 不超过64位)、name、category(existing_behavior 或 new_requirement)、
   expectation、steps。existing_behavior 表示「修改前应已正常的原有行为」，new_requirement 表示「修改后需要满足的新行为」。
3. steps 中每个步骤包含：name、method、path(以/开头)、headers(可选, 值可以是 {{变量}})、json(可选)、timeout(可选, 秒)、
   extract(可选, {变量名: {source: json|header|status|text, path: 提取路径, sensitive: true/false}})、
   assert(非空数组；元素形如 {"type": "status|json|header|text", "op": "...", "value": ..., "path": "..."})。
4. 不要编造登录凭据；如需登录，使用占位说明并在 expectation 中注明「凭据待用户填写」。
5. 提供 URL 中资源归属类检查时，要写清由谁访问什么资源、期望什么状态码。
6. 输出 JSON：{"checks": [...]}，不要输出 JSON 之外的内容。
7. 不要声称任何检查已经通过；你只是提出建议。"""


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


def llm_status() -> dict:
    key = _api_key()
    return {
        "available": bool(key),
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
        "source": key_source(),
        "hint": "已配置模型 Key，可用于生成检查建议"
        if key
        else "未配置模型 API Key（可在项目根的 .env 里填 LLM_API_KEY，或用环境变量），请手动编写规则",
    }


def key_source() -> Optional[str]:
    """Key 来自哪里：`.env` / 环境变量 / None（未配置）。"""
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return ".env" if envfile.was_loaded(name) else "环境变量"
    return None


def _api_key() -> Optional[str]:
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """可注入的 HTTP 调用（测试中可替换）。"""
    resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code >= 400:
        raise LLMError(f"模型接口返回 {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def suggest_checks(
    requirement: str,
    endpoints: list[dict],
    existing_checks: list[dict] | None = None,
    post_json: Callable[..., dict] = _post_json,
) -> dict:
    """调用 LLM 生成检查建议。返回 {"checks": [...], "warnings": [...], "model": str}。"""
    key = _api_key()
    if not key:
        raise LLMNotConfigured(
            "未配置模型 API Key（可在项目根的 .env 里填 LLM_API_KEY，或设置环境变量 LLM_API_KEY / OPENAI_API_KEY）。"
            "你可以手动编写规则，或点击「加载演示规则」参考示例；核心验收流程不依赖模型。"
        )

    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    user_payload = {
        "需求": requirement,
        "接口清单": endpoints,
        "已有检查（避免重复）": existing_checks or [],
    }
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = post_json(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload=body,
            timeout=60,
        )
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001 - 网络异常统一暴露
        raise LLMError(f"调用模型失败：{type(e).__name__}: {e}") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"模型响应格式异常：{json.dumps(data, ensure_ascii=False)[:500]}") from e

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型输出不是合法 JSON：{content[:500]}") from e

    raw_checks = parsed.get("checks") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_checks, list):
        raise LLMError(f"模型输出缺少 checks 数组：{json.dumps(parsed, ensure_ascii=False)[:500]}")

    # 与已有检查合并后整体校验；不合法的建议会带上错误信息供用户修改
    warning_prefix = "模型建议"
    warnings: list[str] = []
    valid: list[dict] = []
    for i, check in enumerate(raw_checks):
        errors = validate_checks((existing_checks or []) + valid + [check])
        if errors:
            warnings.append(f"{warning_prefix} #{i + 1} 未通过校验，已保留供修改：{'；'.join(errors)}")
            if isinstance(check, dict):
                check = dict(check)
                check["_suggestion_errors"] = errors
                valid.append(check)
        else:
            valid.append(check)

    return {"checks": valid, "warnings": warnings, "model": model}
