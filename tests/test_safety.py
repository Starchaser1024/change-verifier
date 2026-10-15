"""安全限制：默认只允许操作本地环境；远程目标需要显式开启，远程写入需要二次确认。"""

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
