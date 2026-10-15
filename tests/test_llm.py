import json

import pytest

from verifier import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_status_without_key():
    status = llm.llm_status()
    assert status["available"] is False
    assert "未配置" in status["hint"]


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
