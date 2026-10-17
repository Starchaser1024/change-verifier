import json

import pytest

from verifier import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in llm.KEY_VARS + llm.BASE_URL_VARS + llm.MODEL_VARS:
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
