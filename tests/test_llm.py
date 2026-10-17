import json

import httpx
import pytest

from verifier import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in llm.KEY_VARS + llm.BASE_URL_VARS + llm.MODEL_VARS + llm.TIMEOUT_VARS:
        monkeypatch.delenv(name, raising=False)


def test_status_without_key():
    status = llm.llm_status()
    assert status["available"] is False
    assert "未配置" in status["hint"]


def test_status_defaults_when_nothing_set():
    status = llm.llm_status()
    assert status["base_url"] == "https://api.openai.com/v1"
    assert status["model"] == "gpt-4o-mini"


def test_openai_prefixed_names_are_accepted(monkeypatch):
    """沿用各家 SDK 习惯的 OPENAI_* 名字也要生效（否则地址/模型会被静默忽略）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://third-party.example.com/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")

    status = llm.llm_status()
    assert status["available"] is True
    assert status["base_url"] == "https://third-party.example.com/v1", "末尾斜杠要归一化"
    assert status["model"] == "gpt-4o"

    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["url"] = url
        captured["model"] = payload["model"]
        return {"choices": [{"message": {"content": json.dumps({"checks": []})}}]}

    llm.suggest_checks("需求", [], [], post_json=fake_post)
    assert captured["url"] == "https://third-party.example.com/v1/chat/completions"
    assert captured["model"] == "gpt-4o"


def test_llm_prefix_wins_over_openai_prefix(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-llm")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "llm-model")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")

    assert llm._api_key() == "sk-llm"
    assert llm.base_url() == "https://llm.example.com/v1"
    assert llm.model() == "llm-model"


def test_blank_alias_falls_through_to_next(monkeypatch):
    """空串 / 纯空白不算配置，应继续往后找。"""
    monkeypatch.setenv("LLM_BASE_URL", "   ")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "  ")

    assert llm.base_url() == "https://openai.example.com/v1"
    assert llm.model() == "gpt-4o-mini"


def test_timeout_default_is_generous():
    """默认超时要够推理模型用（实测一次建议约 50 秒，60 秒上限会随机超时）。"""
    assert llm.timeout_seconds() == llm.DEFAULT_TIMEOUT
    assert llm.DEFAULT_TIMEOUT >= 120


def test_timeout_can_be_configured(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "300")
    assert llm.timeout_seconds() == 300.0
    assert llm.llm_status()["timeout"] == 300.0

    monkeypatch.setenv("LLM_TIMEOUT", "  ")
    monkeypatch.setenv("OPENAI_TIMEOUT", "45.5")
    assert llm.timeout_seconds() == 45.5


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_bad_timeout_falls_back_to_default(monkeypatch, bad):
    """配置写错不该让功能直接不可用，退回默认值即可。"""
    monkeypatch.setenv("LLM_TIMEOUT", bad)
    assert llm.timeout_seconds() == llm.DEFAULT_TIMEOUT


def test_suggest_uses_configured_timeout(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TIMEOUT", "123")
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["timeout"] = timeout
        return {"choices": [{"message": {"content": json.dumps({"checks": []})}}]}

    llm.suggest_checks("需求", [], [], post_json=fake_post)
    assert captured["timeout"] == 123.0


def test_system_prompt_enumerates_assert_ops_from_validator():
    """提示词里的 op 白名单必须与校验器一致：模型写出的 op 一旦不被接受，整条建议会被拦下。"""
    from verifier.models import ASSERT_TYPES

    for atype, ops in ASSERT_TYPES.items():
        assert f"{atype} 只能用" in llm.SYSTEM_PROMPT, f"提示词没列出 {atype} 允许的 op"
        for op in ops:
            assert op in llm.SYSTEM_PROMPT, f"提示词漏了 {atype}.{op}"
    assert "不要自造" in llm.SYSTEM_PROMPT


def test_timeout_error_explains_the_knob(monkeypatch):
    """超时要给出可操作的提示，而不是一句 ReadTimeout。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    def fake_post(url, headers, payload, timeout):
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(llm.LLMError) as e:
        llm.suggest_checks("需求", [], [], post_json=fake_post)
    message = str(e.value)
    assert "超时" in message
    assert "LLM_TIMEOUT" in message


def test_suggest_without_key_raises_not_configured():
    with pytest.raises(llm.LLMNotConfigured) as e:
        llm.suggest_checks("需求", [])
    assert "未配置模型 API Key" in str(e.value)


def test_suggest_with_key_parses_model_output(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    suggested = {
        "checks": [
            {
                "id": "login-ok",
                "name": "登录成功",
                "category": "existing_behavior",
                "expectation": "登录返回 token",
                "steps": [
                    {
                        "method": "POST",
                        "path": "/api/login",
                        "assert": [{"type": "status", "op": "equals", "value": 200}],
                    }
                ],
            }
        ]
    }

    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return {"choices": [{"message": {"content": json.dumps(suggested, ensure_ascii=False)}}]}

    result = llm.suggest_checks("需求", [{"method": "POST", "path": "/api/login"}], [], post_json=fake_post)
    assert result["model"] == "test-model"
    assert result["checks"][0]["id"] == "login-ok"
    assert result["warnings"] == []
    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["model"] == "test-model"


def test_suggest_marks_invalid_suggestion(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    bad_check = {"id": "no-steps", "name": "缺步骤", "category": "existing_behavior"}

    def fake_post(url, headers, payload, timeout):
        content = json.dumps({"checks": [bad_check]}, ensure_ascii=False)
        return {"choices": [{"message": {"content": content}}]}

    result = llm.suggest_checks("需求", [], [], post_json=fake_post)
    assert len(result["checks"]) == 1
    assert result["checks"][0]["_suggestion_errors"]
    assert result["warnings"]


def test_suggest_bad_json_raises(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    def fake_post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "这不是 JSON"}}]}

    with pytest.raises(llm.LLMError):
        llm.suggest_checks("需求", [], [], post_json=fake_post)


def test_suggest_http_error_raises(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    def fake_post(url, headers, payload, timeout):
        raise llm.LLMError("模型接口返回 500: server error")

    with pytest.raises(llm.LLMError) as e:
        llm.suggest_checks("需求", [], [], post_json=fake_post)
    assert "500" in str(e.value)
