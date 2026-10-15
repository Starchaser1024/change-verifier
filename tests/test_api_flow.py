"""验收器 API 流程测试：任务 → 规则版本（不可变）→ 确认 → 运行 → 报告。"""

from __future__ import annotations

from verifier.db import get_db, now_iso

CHECK_V1 = {
    "id": "ping",
    "name": "健康检查",
    "category": "existing_behavior",
    "steps": [{"method": "GET", "path": "/healthz", "assert": [{"type": "status", "op": "equals", "value": 200}]}],
}

CHECK_V2 = {
    "id": "ping",
    "name": "健康检查（调整期望）",
    "category": "existing_behavior",
    "steps": [{"method": "GET", "path": "/healthz", "assert": [{"type": "status", "op": "in", "value": [200, 204]}]}],
}


def create_task(client):
    resp = client.post(
        "/api/tasks",
        json={
            "name": "API 流程测试",
            "requirement": "验证规则版本管理与报告导出",
            "baseline_url": "http://127.0.0.1:8620",
            "candidate_url": "http://127.0.0.1:8621",
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_task_and_rule_version_flow(verifier_client):
    client = verifier_client
    task_id = create_task(client)

    # 无效规则 → 400 且带具体错误
    bad = client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [{"id": "x"}], "note": ""})
    assert bad.status_code == 400
    errors = bad.json()["detail"]["errors"]
    assert any("steps" in e for e in errors)

    # 保存 v1 草稿
    v1 = client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [CHECK_V1], "note": "初版", "confirm": False}).json()
    assert v1["version"] == 1 and v1["status"] == "draft"
    v1_id = v1["id"]

    # 保存 v2（已确认）——v1 保持不变（不可变版本）
    v2 = client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [CHECK_V2], "note": "调整", "confirm": True}).json()
    assert v2["version"] == 2 and v2["status"] == "confirmed"

    v1_full = client.get(f"/api/rule-versions/{v1_id}").json()
    assert v1_full["status"] == "draft"
    assert v1_full["checks"] == [CHECK_V1]

    # 确认 v1 不影响已确认的版本，也不会改内容
    assert client.post(f"/api/rule-versions/{v1_id}/confirm").status_code == 200
    v1_again = client.get(f"/api/rule-versions/{v1_id}").json()
    assert v1_again["status"] == "confirmed"
    assert v1_again["checks"] == [CHECK_V1]

    versions = client.get(f"/api/tasks/{task_id}/rule-versions").json()["rule_versions"]
    assert [v["version"] for v in versions] == [2, 1]

    # 任务详情包含版本与运行列表
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["task"]["id"] == task_id
    assert len(detail["rule_versions"]) == 2
    assert detail["runs"] == []


def test_report_unavailable_before_result(verifier_client):
    client = verifier_client
    task_id = create_task(client)
    v = client.post(f"/api/tasks/{task_id}/rule-versions", json={"checks": [CHECK_V1], "note": "", "confirm": True}).json()

    # 直接插入一条「运行中」记录（模拟尚未产生结果的运行）
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (task_id, rule_version_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (task_id, v["id"], now_iso()),
        )
        run_id = int(cur.lastrowid)

    resp = client.get(f"/api/runs/{run_id}/report.md")
    assert resp.status_code == 409


def test_run_requires_rule_version(verifier_client):
    client = verifier_client
    task_id = create_task(client)
    resp = client.post(f"/api/tasks/{task_id}/runs", json={})
    assert resp.status_code == 400
    assert "规则" in str(resp.json()["detail"])


def test_demo_task_endpoint_creates_confirmed_rules(verifier_client):
    client = verifier_client
    resp = client.post("/api/demo/create-task")
    assert resp.status_code == 200
    data = resp.json()
    assert data["checks"] == 10
    versions = client.get(f"/api/tasks/{data['task_id']}/rule-versions").json()["rule_versions"]
    assert versions[0]["status"] == "confirmed"
    assert versions[0]["check_count"] == 10


def test_suggest_without_key_returns_actionable_error(verifier_client, monkeypatch):
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "OPENCODE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = verifier_client
    task_id = create_task(client)
    resp = client.post(f"/api/tasks/{task_id}/suggest-checks", json={})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "llm_not_configured"
    assert "手动编写" in detail["message"]


def test_llm_status_endpoint(verifier_client, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    status = verifier_client.get("/api/llm/status").json()
    assert status["available"] is False
