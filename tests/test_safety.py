"""安全限制：默认只允许操作本地环境；远程目标需要显式开启，远程写入需要二次确认。"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading

import httpx
import pytest

from verifier.openapi_import import fetch_spec
from verifier.safety import SafetyError, is_loopback

VALID_CHECK_GET = {
    "id": "ping",
    "name": "健康检查",
    "category": "existing_behavior",
    "steps": [{"method": "GET", "path": "/healthz", "assert": [{"type": "status", "op": "equals", "value": 200}]}],
}

VALID_CHECK_POST = {
    "id": "write",
    "name": "写入检查",
    "category": "new_requirement",
    "steps": [{"method": "POST", "path": "/api/orders", "json": {"item": "x"}, "assert": [{"type": "status", "op": "in", "value": [200, 201]}]}],
}


def make_task(client, **overrides):
    body = {
        "name": "安全测试任务",
        "requirement": "测试安全限制",
        "baseline_url": "http://127.0.0.1:1",
        "candidate_url": "http://example.com",
        **overrides,
    }
    resp = client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@contextmanager
def local_openapi_redirect_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            redirects = {
                "/redirect-external": "https://example.com/openapi.json",
                "/redirect-metadata": "http://169.254.169.254/latest/meta-data/",
                "/redirect-proto-rel": "//example.com/openapi.json",
                "/redirect-file": "file:///etc/passwd",
                "/redirect-local": "/v1/openapi.json",
                "/redirect-loop": "/redirect-loop",
            }
            if self.path in redirects:
                self.send_response(302)
                self.send_header("Location", redirects[self.path])
                self.end_headers()
                return
            if self.path == "/v1/openapi.json":
                raw = json.dumps(
                    {
                        "openapi": "3.1.0",
                        "paths": {"/api/orders": {"get": {"summary": "订单列表"}}},
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/not-json":
                raw = b"openapi: 3.0.0\npaths: {}\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_remote_target_requires_explicit_opt_in(verifier_client):
    task_id = make_task(verifier_client)
    verifier_client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [VALID_CHECK_GET], "note": "", "confirm": True})
    resp = verifier_client.post(f"/api/tasks/{task_id}/runs", json={})
    assert resp.status_code == 400
    assert "安全限制" in str(resp.json()["detail"])


def test_remote_writes_blocked_even_with_maybe_allow_remote(verifier_client):
    task_id = make_task(verifier_client, allow_remote=True)
    verifier_client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [VALID_CHECK_POST], "note": "", "confirm": True})
    resp = verifier_client.post(f"/api/tasks/{task_id}/runs", json={})
    assert resp.status_code == 400
    assert "写入" in str(resp.json()["detail"])


def test_invalid_url_rejected(verifier_client):
    resp = verifier_client.post(
        "/api/tasks",
        json={"name": "bad", "baseline_url": "not-a-url", "candidate_url": "http://127.0.0.1:1"},
    )
    assert resp.status_code == 400


def test_is_loopback_rejects_spoofed_127_subdomains():
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("127.0.0.2") is True
    assert is_loopback("localhost") is True
    assert is_loopback("::1") is True
    assert is_loopback("[::1]") is True
    assert is_loopback("127.evil.com") is False
    assert is_loopback("127.0.0.1.attacker.tld") is False
    assert is_loopback("169.254.169.254") is False


def test_openapi_import_checks_task_exists_before_fetching(verifier_client, monkeypatch):
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("fetch_spec should not be called when task does not exist")

    monkeypatch.setattr("verifier.routes.tasks.fetch_spec", fail_if_called)
    resp = verifier_client.post(
        "/api/tasks/999999/endpoints/import",
        json={"spec_url": "http://127.0.0.1:1/openapi.json"},
    )
    assert resp.status_code == 404
    assert called is False


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://example.com/openapi.json",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.evil.com/openapi.json",
        "file:///etc/passwd",
    ],
)
def test_openapi_import_blocks_non_loopback_and_non_http_urls_by_default(verifier_client, bad_url):
    task_id = make_task(verifier_client, candidate_url="http://127.0.0.1:2", allow_remote=False)
    with pytest.raises(SafetyError):
        fetch_spec(bad_url, allow_remote=False)
    resp = verifier_client.post(f"/api/tasks/{task_id}/endpoints/import", json={"spec_url": bad_url})
    assert resp.status_code == 400


def test_openapi_import_blocks_302_redirect_to_remote_and_allows_loopback_redirect(verifier_client):
    task_id = make_task(verifier_client, candidate_url="http://127.0.0.1:2", allow_remote=False)
    with local_openapi_redirect_server() as base:
        for evil_path in ("/redirect-external", "/redirect-metadata", "/redirect-proto-rel", "/redirect-file"):
            with pytest.raises(SafetyError):
                fetch_spec(base + evil_path, allow_remote=False)
            resp = verifier_client.post(
                f"/api/tasks/{task_id}/endpoints/import",
                json={"spec_url": base + evil_path},
            )
            assert resp.status_code == 400, evil_path

        # 本机内部相对重定向应正常放行并成功解析接口
        ok_resp = verifier_client.post(
            f"/api/tasks/{task_id}/endpoints/import",
            json={"spec_url": base + "/redirect-local"},
        )
        assert ok_resp.status_code == 200, ok_resp.text
        assert ok_resp.json()["endpoints"] == [{"method": "GET", "path": "/api/orders", "summary": "订单列表"}]

        # 重定向死循环应受上限保护并返回 400
        loop_resp = verifier_client.post(
            f"/api/tasks/{task_id}/endpoints/import",
            json={"spec_url": base + "/redirect-loop"},
        )
        assert loop_resp.status_code == 400
        assert "重定向次数过多" in loop_resp.json()["detail"]

        # 非 JSON 内容应给出准确的 OpenAPI JSON 错误说明
        yaml_resp = verifier_client.post(
            f"/api/tasks/{task_id}/endpoints/import",
            json={"spec_url": base + "/not-json"},
        )
        assert yaml_resp.status_code == 400
        detail = yaml_resp.json()["detail"]
        assert "OpenAPI JSON" in detail
        assert "yaml" not in detail.lower()


def test_openapi_import_respects_task_allow_remote_flag(verifier_client, monkeypatch):
    task_id = make_task(verifier_client, candidate_url="https://staging.example.com", allow_remote=True)

    def fake_get(self, url, *args, **kwargs):
        return httpx.Response(
            200,
            json={"openapi": "3.0.3", "paths": {"/healthz": {"get": {"summary": "健康检查"}}}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    resp = verifier_client.post(
        f"/api/tasks/{task_id}/endpoints/import",
        json={"spec_url": "https://staging.example.com/openapi.json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["endpoints"] == [{"method": "GET", "path": "/healthz", "summary": "健康检查"}]
