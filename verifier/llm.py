"""可选的 LLM 辅助：根据需求与接口清单提出检查建议。

原则：
- 未配置模型 API Key 时，明确报错，绝不伪造模型输出。
- 模型输出只作为「建议」，必须经用户编辑并确认后才会保存为规则版本。
- 任何模型调用失败都会把错误原样暴露给用户。

环境变量（三类前缀都认，按 LLM_ → OPENAI_ → OPENCODE_ 的顺序取第一个非空值，
方便直接沿用各家 SDK 习惯的名字）：
- API Key：LLM_API_KEY / OPENAI_API_KEY / OPENCODE_API_KEY   任一存在即视为已配置
- 接口地址：LLM_BASE_URL / OPENAI_BASE_URL / OPENCODE_BASE_URL   默认 https://api.openai.com/v1
- 模型名：LLM_MODEL / OPENAI_MODEL / OPENCODE_MODEL   默认 gpt-4o-mini
- 超时：LLM_TIMEOUT / OPENAI_TIMEOUT / OPENCODE_TIMEOUT   默认 180 秒

这些变量可以写在项目根的 `.env` 里（见 verifier/envfile.py）；真实环境变量优先于 `.env`。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

import httpx

from . import envfile
from .models import ASSERT_TYPES, validate_checks

# 把校验器真正接受的 type/op 组合直接拼进提示词。模型很爱自造 eq / gte / max_length 这类
# 常见写法，一旦写错整条建议会被校验拦下、用户得逐条手改。白名单从 ASSERT_TYPES 生成，
# 保证提示词与校验器不会各自漂移。
ASSERT_OPS_HINT = "；".join(
    f"{atype} 只能用 {' / '.join(sorted(ops))}" for atype, ops in sorted(ASSERT_TYPES.items())
)

SYSTEM_PROMPT = (
    """你是一个谨慎的 API 验收规则助手。
用户会给出「修改需求」、接口清单和已有检查。你的任务只是提出可执行的验收检查建议。

要求：
1. 只能依据用户明确给出的需求与接口清单提出建议，不要臆造接口或业务规则。
2. 每条检查必须包含字段：id(小写字母/数字/.-_, 不超过64位)、name、category(existing_behavior 或 new_requirement)、
   expectation、steps。existing_behavior 表示「修改前应已正常的原有行为」，new_requirement 表示「修改后需要满足的新行为」。
3. steps 中每个步骤包含：name、method、path(以/开头)、headers(可选, 值可以是 {{变量}})、json(可选)、timeout(可选, 秒)、
   extract(可选, {变量名: {source: json|header|status|text, path: 提取路径, sensitive: true/false}})、
   assert(非空数组；元素形如 {"type": "status", "op": "equals", "value": 200} 或
   {"type": "json", "path": "$.items", "op": "exists"})。
"""
    + f"   断言只能使用下面列出的 op，**不要自造**（eq / gte / lte / max_length 这类都不存在）：{ASSERT_OPS_HINT}。\n"
    + """   比较「条数」「长度」必须用 length_* 系列：要表达「最多 N 条」写
   {"type": "json", "path": "$.items", "op": "length_less_than", "value": N+1}。
   greater_than / less_than 比的是**值本身**，用在数组或对象上永远不成立。
4. 不要编造登录凭据；如需登录，使用占位说明并在 expectation 中注明「凭据待用户填写」。
5. 提供 URL 中资源归属类检查时，要写清由谁访问什么资源、期望什么状态码。
6. 输出 JSON：{"checks": [...]}，不要输出 JSON 之外的内容。
7. 不要声称任何检查已经通过；你只是提出建议。"""
)


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


KEY_VARS = ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY")
BASE_URL_VARS = ("LLM_BASE_URL", "OPENAI_BASE_URL", "OPENCODE_BASE_URL")
MODEL_VARS = ("LLM_MODEL", "OPENAI_MODEL", "OPENCODE_MODEL")
TIMEOUT_VARS = ("LLM_TIMEOUT", "OPENAI_TIMEOUT", "OPENCODE_TIMEOUT")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
# 默认给得比较宽：推理模型会先产出大量 reasoning token，实测一次「AI 建议」约 50 秒，
# 原来 60 秒的上限会让它在负载稍高时随机超时。
DEFAULT_TIMEOUT = 180.0


def _first_set(names: tuple[str, ...]) -> Optional[str]:
    """按顺序返回第一个非空的环境变量值（去掉首尾空白）。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def llm_status() -> dict:
    key = _api_key()
    return {
        "available": bool(key),
        "base_url": base_url(),
        "model": model(),
        "timeout": timeout_seconds(),
        "source": key_source(),
        "hint": "已配置模型 Key，可用于生成检查建议"
        if key
        else "未配置模型 API Key（可在项目根的 .env 里填 LLM_API_KEY / OPENAI_API_KEY），请手动编写规则",
    }


def base_url() -> str:
    """接口地址：LLM_BASE_URL / OPENAI_BASE_URL / OPENCODE_BASE_URL，都没有则用默认。"""
    return (_first_set(BASE_URL_VARS) or DEFAULT_BASE_URL).rstrip("/")


def model() -> str:
    """模型名：LLM_MODEL / OPENAI_MODEL / OPENCODE_MODEL，都没有则用默认。"""
    return _first_set(MODEL_VARS) or DEFAULT_MODEL


def timeout_seconds() -> float:
    """单次模型调用超时（秒）：LLM_TIMEOUT / OPENAI_TIMEOUT / OPENCODE_TIMEOUT。

    解析不出正数就退回默认值（配置写错不该让功能直接不可用）。
    """
    raw = _first_set(TIMEOUT_VARS)
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def key_source() -> Optional[str]:
    """Key 来自哪里：`.env` / 环境变量 / None（未配置）。"""
    for name in KEY_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return ".env" if envfile.was_loaded(name) else "环境变量"
    return None


def _api_key() -> Optional[str]:
    return _first_set(KEY_VARS)


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

    api_base_url = base_url()
    api_model = model()
    api_timeout = timeout_seconds()

    user_payload = {
        "需求": requirement,
        "接口清单": endpoints,
        "已有检查（避免重复）": existing_checks or [],
    }
    body = {
        "model": api_model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        data = post_json(
            f"{api_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            payload=body,
            timeout=api_timeout,
        )
    except LLMError:
        raise
    except httpx.TimeoutException as e:
        raise LLMError(
            f"调用模型超时（{api_timeout:g} 秒，模型 {api_model}）：{type(e).__name__}。"
            "推理模型或输出较长时需要更久，可用 LLM_TIMEOUT 调大（例如 LLM_TIMEOUT=300），"
            "或换一个更快的模型。"
        ) from e
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

    return {"checks": valid, "warnings": warnings, "model": api_model}
