from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
import httpx
import pytest

from tests.conftest import free_port, start_server, wait_ready
from verifier.classification import build_summary, compare_check
from verifier.executor import FAILED, PASSED, UNVERIFIABLE, evaluate_assertion, execute_check
from verifier.models import validate_checks
from verifier.report import build_json_report, render_markdown

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


def _assert_on(payload, op, value, path="$.items"):
    """直接对一份假响应求值，不依赖示例服务的种子数据。"""
    resp = httpx.Response(200, json=payload)
    return evaluate_assertion({"type": "json", "path": path, "op": op, "value": value}, resp, payload, "")


@pytest.mark.parametrize(
    "op,value,expected_ok",
    [
        ("length_less_than", 3, True),  # 2 < 3
        ("length_less_than", 2, False),  # 2 < 2 不成立
        ("length_less_than", 1, False),
        ("length_greater_than", 1, True),  # 2 > 1
        ("length_greater_than", 2, False),
        ("length_greater_than", 3, False),
        ("length_equals", 2, True),
    ],
)
def test_array_length_comparisons(op, value, expected_ok):
    assert _assert_on({"items": [1, 2]}, op, value)["ok"] is expected_ok


def test_length_ops_work_on_strings_and_objects_too():
    """与 length_equals 保持一致：数组 / 字符串 / 对象都按长度比较。"""
    assert _assert_on({"name": "abcdef"}, "length_less_than", 7, path="$.name")["ok"] is True
    assert _assert_on({"name": "abcdef"}, "length_greater_than", 6, path="$.name")["ok"] is False
    assert _assert_on({"obj": {"a": 1, "b": 2}}, "length_greater_than", 1, path="$.obj")["ok"] is True


def test_length_ops_fail_when_value_is_not_a_collection():
    """数字没有「长度」这个概念，应判失败而不是崩掉。"""
    result = _assert_on({"count": 5}, "length_less_than", 10, path="$.count")
    assert result["ok"] is False


def test_length_ops_fail_on_missing_path():
    assert _assert_on({}, "length_less_than", 10)["ok"] is False


def test_less_than_on_an_array_always_fails():
    """文档化这个陷阱：less_than 比的是值本身，对数组永远不成立。

    「最多 N 条」必须写成 length_less_than —— 这正是 LLM 会写错、而校验器看不出来的那类语义错误。
    """
    assert _assert_on({"items": [1, 2]}, "less_than", 3)["ok"] is False
    assert _assert_on({"items": [1, 2]}, "length_less_than", 3)["ok"] is True


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


@contextmanager
def _payload_server(payload: dict, hits: list[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            hits.append(self.path)
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "step_path,extracted_value",
    [
        ("{{target}}", "https://example.com/evil"),
        ("{{target}}", "//example.com/evil"),
        ("/{{target}}", "/example.com/evil"),
        ("/redirect?next={{target}}", "https://example.com/evil"),
        ("{{target}}", "/api/orders\r\nX-Injected: 1"),
        ("{{target}}", "/api/orders https://example.com"),
        ("{{target}}", 12345),
        ("{{target}}", ""),
    ],
)
def test_rendered_step_path_blocks_base_url_escape_without_external_request(step_path, extracted_value):
    hits: list[str] = []
    check = {
        "id": "path-escape-guard",
        "name": "拦截动态渲染路径逃逸",
        "category": "existing_behavior",
        "steps": [
            {
                "name": "获取不可信的下一步地址",
                "method": "GET",
                "path": "/api/next",
                "extract": {"target": {"source": "json", "path": "$.next", "sensitive": False}},
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            },
            {
                "name": "跟随渲染后的路径",
                "method": "GET",
                "path": step_path,
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            },
        ],
    }
    assert validate_checks([check]) == []

    with _payload_server({"next": extracted_value}, hits) as base_url:
        result = run(check, base_url)

    assert result["status"] == FAILED
    assert hits == ["/api/next"], "第二步路径非法时必须在发送任何请求前中止"
    second = result["steps"][1]
    assert second["response"] is None
    assert second["error"]["kind"] == "config"


@pytest.mark.parametrize(
    "bad_static_path",
    [
        "//example.com/evil",
        "https://example.com/evil",
        "/api/orders\r\nX-Injected: 1",
        "/api/orders evil",
        "/redirect?url=http://example.com/evil",
    ],
)
def test_validate_checks_rejects_unsafe_static_step_paths(bad_static_path):
    check = {
        "id": "bad-path",
        "name": "非法静态路径",
        "category": "existing_behavior",
        "steps": [
            {
                "method": "GET",
                "path": bad_static_path,
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            }
        ],
    }
    errors = validate_checks([check])
    assert errors, f"expected validation error for path={bad_static_path!r}"


@contextmanager
def _oauth2_and_pagination_fastapi_server():
    app = FastAPI()
    issued_token = "oauth2-tok+live/42=ok"
    all_items = [{"id": i, "title": f"item-{i}"} for i in range(1, 7)]

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/token")
    async def login_token(request: Request):
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" not in ctype:
            raise HTTPException(status_code=415, detail="OAuth2 /token requires form-urlencoded body")
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        grant_type = (form.get("grant_type") or ["password"])[0]
        if grant_type != "password" or username != "alice" or password != "alice-form-secret":
            raise HTTPException(status_code=401, detail="invalid credentials")
        return {
            "access_token": issued_token,
            "token_type": "bearer",
            "next_page": 2,
        }

    @app.get("/items")
    def list_items(
        request: Request,
        page: int = 1,
        page_size: int = 2,
        api_key: str = "",
        cursor: str = "",
    ):
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {issued_token}" or api_key != "query-key-777" or cursor != issued_token:
            raise HTTPException(status_code=403, detail="forbidden")
        start = (page - 1) * page_size
        return {
            "items": all_items[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": len(all_items),
        }

    port = free_port()
    server, thread = start_server(app, port)
    base = f"http://127.0.0.1:{port}"
    wait_ready(base)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_oauth2_form_login_and_query_params_pagination_with_full_redaction():
    check = {
        "id": "oauth2-form-and-params",
        "name": "OAuth2 表单登录与分页查询脱敏",
        "category": "new_requirement",
        "expectation": "通过 form 登录 /token 后携带 params 分页拉取第 2 页且全链路凭据脱敏",
        "steps": [
            {
                "name": "OAuth2 表单登录",
                "method": "POST",
                "path": "/token",
                "form": {
                    "grant_type": "password",
                    "username": "alice",
                    "password": "alice-form-secret",
                    "client_secret": "cs-live-999",
                },
                "extract": {
                    "token": {"source": "json", "path": "$.access_token", "sensitive": True},
                    "target_page": {"source": "json", "path": "$.next_page", "sensitive": False},
                },
                "assert": [
                    {"type": "status", "op": "equals", "value": 200},
                    {"type": "json", "path": "$.token_type", "op": "equals", "value": "bearer"},
                ],
            },
            {
                "name": "带 params 查询第 2 页",
                "method": "GET",
                "path": "/items",
                "params": {
                    "page": "{{target_page}}",
                    "page_size": 2,
                    "api_key": "query-key-777",
                    "cursor": "{{token}}",
                },
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [
                    {"type": "status", "op": "equals", "value": 200},
                    {"type": "json", "path": "$.page", "op": "equals", "value": 2},
                    {"type": "json", "path": "$.items", "op": "length_equals", "value": 2},
                    {"type": "json", "path": "$.items[0].id", "op": "equals", "value": 3},
                ],
            },
        ],
    }
    assert validate_checks([check]) == []

    with _oauth2_and_pagination_fastapi_server() as base_url:
        res = run(check, base_url)

    assert res["status"] == PASSED, res["reason"]
    step1, step2 = res["steps"]
    assert step1["request"]["form"] == {
        "grant_type": "password",
        "username": "alice",
        "password": "[REDACTED]",
        "client_secret": "[REDACTED]",
    }
    assert step1["request"]["json"] is None
    assert step2["request"]["params"] == {
        "page": 2,
        "page_size": 2,
        "api_key": "[REDACTED]",
        "cursor": "[REDACTED]",
    }
    assert "api_key=[REDACTED]" in step2["request"]["url"]
    assert "cursor=[REDACTED]" in step2["request"]["url"]

    cmp = compare_check(check, res, res, {check["id"]: (res, res)})
    payload = {"summary": build_summary([cmp], total_checks=1, run_status="completed"), "checks": [cmp]}
    task = {"id": 1, "name": "OAuth2 验收", "requirement": "", "baseline_url": "http://127.0.0.1:1", "candidate_url": "http://127.0.0.1:2"}
    ver = {"id": 1, "version": 1, "status": "confirmed", "note": "", "confirmed_at": "2026-01-01T00:00:00Z"}
    run_meta = {"id": 1, "status": "completed", "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z", "error": None}
    md = render_markdown(task, ver, run_meta, payload)
    json_rep = json.dumps(build_json_report(task, ver, run_meta, payload), ensure_ascii=False)

    assert "表单请求体" in md
    assert "查询参数" in md
    for secret in ("alice-form-secret", "cs-live-999", "query-key-777", "oauth2-tok+live/42=ok", "oauth2-tok"):
        assert secret not in md
        assert secret not in json_rep


def test_executor_rejects_simultaneous_json_and_form_even_without_validator(baseline_server):
    check = {
        "id": "conflict-body",
        "name": "同时传 json 与 form",
        "steps": [
            {
                "method": "POST",
                "path": "/api/login",
                "json": {"username": "alice"},
                "form": {"username": "alice"},
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            }
        ],
    }
    res = run(check, baseline_server)
    assert res["status"] == FAILED
    assert res["steps"][0]["error"]["kind"] == "config"
