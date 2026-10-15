import threading

from verifier.executor import FAILED, PASSED, UNVERIFIABLE, execute_check

LOGIN = {
    "name": "登录",
    "method": "POST",
    "path": "/api/login",
    "json": {"username": "alice", "password": "alice-password"},
    "extract": {"token": {"source": "json", "path": "$.access_token", "sensitive": True}},
    "assert": [{"type": "status", "op": "equals", "value": 200}],
}


def run(check, base_url, cancel_event=None, index=0):
    return execute_check(check, "baseline", base_url, "testnonce", index, default_timeout=5, cancel_event=cancel_event)


def test_login_extract_and_use_token(baseline_server):
    check = {
        "id": "own-order",
        "name": "读取自己的订单",
        "steps": [
            LOGIN,
            {
                "name": "读取订单 101",
                "method": "GET",
                "path": "/api/orders/101",
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [
                    {"type": "status", "op": "equals", "value": 200},
                    {"type": "json", "path": "$.owner_id", "op": "equals", "value": 1},
                ],
            },
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == PASSED, result["reason"]
    # 敏感字段脱敏：请求体里的密码、响应里的 token
    login_step = result["steps"][0]
    assert login_step["request"]["json"]["password"] == "[REDACTED]"
    assert login_step["response"]["json"]["access_token"] == "[REDACTED]"
    assert login_step["extracted"]["token"] == "[REDACTED]"
    second = result["steps"][1]
    assert second["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert second["assertions"][0]["ok"] is True


def test_assertion_failure_records_evidence(baseline_server):
    check = {
        "id": "wrong-expectation",
        "name": "故意写错的期望",
        "steps": [
            LOGIN,
            {
                "name": "读取自己的订单，却期望 403",
                "method": "GET",
                "path": "/api/orders/101",
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [{"type": "status", "op": "equals", "value": 403}],
            },
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == FAILED
    failed = result["steps"][1]
    assert failed["assertions"][0]["ok"] is False
    assert failed["assertions"][0]["actual"] == "200"
    assert failed["response"]["status"] == 200  # 证据保留真实响应
    assert "403" in result["reason"]


def test_server_error_is_a_response_not_unverifiable(baseline_server):
    """500 是真实响应，应判为断言失败（原有问题），而不是无法判断。"""
    check = {
        "id": "missing-order",
        "name": "不存在的订单",
        "steps": [
            LOGIN,
            {
                "name": "读取不存在订单",
                "method": "GET",
                "path": "/api/orders/999999",
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [{"type": "status", "op": "equals", "value": 404}],
            },
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == FAILED
    assert result["steps"][1]["response"]["status"] == 500


def test_timeout_is_unverifiable(baseline_server):
    check = {
        "id": "slow",
        "name": "超时",
        "steps": [
            {
                "name": "慢接口",
                "method": "GET",
                "path": "/api/debug/slow?ms=1500",
                "timeout": 0.4,
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            }
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == UNVERIFIABLE
    assert result["steps"][0]["error"]["kind"] == "timeout"
    assert result["steps"][0]["response"] is None


def test_connection_refused_is_unverifiable(dead_url):
    check = {
        "id": "dead",
        "name": "服务不可达",
        "steps": [
            {"method": "GET", "path": "/healthz", "assert": [{"type": "status", "op": "equals", "value": 200}]}
        ],
    }
    result = run(check, dead_url)
    assert result["status"] == UNVERIFIABLE
    assert result["steps"][0]["error"]["kind"] == "connect"


def test_cancel_event_marks_unverifiable(baseline_server):
    ev = threading.Event()
    ev.set()
    check = {"id": "cancel-test", "name": "取消", "steps": [LOGIN]}
    result = run(check, baseline_server, cancel_event=ev)
    assert result["status"] == UNVERIFIABLE
    assert "取消" in result["reason"]


def test_undefined_variable_is_config_failure(baseline_server):
    check = {
        "id": "bad-template",
        "name": "变量未定义",
        "steps": [
            {
                "method": "GET",
                "path": "/api/orders",
                "headers": {"X-Token": "{{not_defined_anywhere}}"},
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            }
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == FAILED
    assert "未定义" in result["reason"]


def test_whole_placeholder_keeps_original_type(baseline_server):
    """断言里的 value={{变量}} 应保持原始类型（数字不会变成字符串）。"""
    check = {
        "id": "type-kept",
        "name": "类型保持",
        "steps": [
            LOGIN,
            {
                "name": "创建订单",
                "method": "POST",
                "path": "/api/orders",
                "headers": {"Authorization": "Bearer {{token}}"},
                "json": {"item": "thing", "quantity": 2},
                "extract": {"oid": {"source": "json", "path": "$.id", "sensitive": False}},
                "assert": [{"type": "status", "op": "in", "value": [200, 201]}],
            },
            {
                "name": "读回订单并比较 id 类型",
                "method": "GET",
                "path": "/api/orders/{{oid}}",
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [
                    {"type": "json", "path": "$.id", "op": "equals", "value": "{{oid}}"},
                    {"type": "json", "path": "$.id", "op": "type", "value": "integer"},
                ],
            },
        ],
    }
    result = run(check, baseline_server)
    assert result["status"] == PASSED, result["reason"]
    assert result["steps"][1]["assertions"][0]["ok"] is True
