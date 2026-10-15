"""端到端验收演示：真实的 baseline / candidate 服务 + 验收器 API。

验证：
1. 演示规则在 candinate 上稳定发现 4 个预设回归、1 个新需求未满足、1 个原有问题、
   1 个已确认的行为变化、1 个新需求满足、2 个通过。
2. 报告包含真实请求/响应证据，且不泄露密码与 token。
3. 修改后环境不可达时，所有检查归类为「无法判断」。
"""

from __future__ import annotations

import re
import time

import httpx

EXPECTED_CODES = {
    "login-success": "pass",
    "read-own-order": "pass",
    "read-other-order-denied": "regression",
    "anonymous-list-denied": "regression",
    "admin-endpoint-forbidden": "regression",
    "create-order-idempotent": "regression",
    "list-legacy-envelope": "expected_change",
    "missing-order-status": "pre_existing",
    "list-new-envelope": "satisfied",
    "list-page-size": "not_met",
}

EXPECTED_COUNTS = {
    "pass": 2,
    "regression": 4,
    "pre_existing": 1,
    "expected_change": 1,
    "satisfied": 1,
    "not_met": 1,
}


def wait_run(client, run_id: int, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/runs/{run_id}").json()
        if data["run"]["status"] != "running":
            return data
        time.sleep(0.25)
    raise AssertionError("运行未在超时时间内结束")


def create_demo_task(client, baseline_url: str, candidate_url: str) -> int:
    task_id = client.post("/api/demo/create-task").json()["task_id"]
    resp = client.patch(
        f"/api/tasks/{task_id}",
        json={"baseline_url": baseline_url, "candidate_url": candidate_url},
    )
    assert resp.status_code == 200, resp.text
    return task_id


def walk(obj):
    """递归遍历 JSON，产出所有 dict。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def test_demo_regression_matrix(verifier_client, baseline_server, candidate_server):
    for base in (baseline_server, candidate_server):
        httpx.post(base + "/api/debug/reset", timeout=5)

    task_id = create_demo_task(verifier_client, baseline_server, candidate_server)
    run_id = verifier_client.post(f"/api/tasks/{task_id}/runs", json={}).json()["run_id"]
    data = wait_run(verifier_client, run_id)

    assert data["run"]["status"] == "completed", data["run"].get("error")
    result = data["result"]
    codes = {c["id"]: c["classification"]["code"] for c in result["checks"]}
    assert codes == EXPECTED_CODES, codes
    assert result["summary"]["counts"] == EXPECTED_COUNTS
    assert "4 项本次引入的回归" in result["summary"]["headline"]

    # 跨用户读取的回归：修改前 403，修改后 200 且返回了 bob 的订单（证据）
    cross = next(c for c in result["checks"] if c["id"] == "read-other-order-denied")
    assert cross["baseline"]["steps"][-1]["response"]["status"] == 403
    assert cross["candidate"]["steps"][-1]["response"]["status"] == 200
    assert cross["candidate"]["steps"][-1]["response"]["json"]["owner_id"] == 2

    # 幂等回归：两次创建的 id 不同
    idem = next(c for c in result["checks"] if c["id"] == "create-order-idempotent")
    assert idem["baseline"]["status"] == "passed"
    assert idem["candidate"]["status"] == "failed"
    second_response = idem["candidate"]["steps"][-1]
    id_assertion = next(a for a in second_response["assertions"] if "$.id" in a["desc"])
    assert id_assertion["ok"] is False
    # 证据里能看到两次创建返回了不同的订单 id
    first_id = idem["candidate"]["steps"][-2]["response"]["json"]["id"]
    second_id = second_response["response"]["json"]["id"]
    assert first_id != second_id

    # ---------------- 报告：JSON ----------------
    report = verifier_client.get(f"/api/runs/{run_id}/report.json").json()
    assert report["task"]["id"] == task_id
    assert report["rule_version"]["status"] == "confirmed"
    assert report["summary"]["counts"]["regression"] == 4

    # 所有 Authorization 头必须已脱敏
    for d in walk(report["checks"]):
        for key, value in (d.get("headers") or {}).items():
            if key.lower() == "authorization":
                assert value == "[REDACTED]", value
        if "password" in d:
            assert d["password"] == "[REDACTED]", d["password"]
    # 登录响应中的 access_token 必须已脱敏
    login = next(c for c in report["checks"] if c["id"] == "login-success")
    assert login["candidate"]["steps"][0]["response"]["json"]["access_token"] == "[REDACTED]"

    # ---------------- 报告：Markdown ----------------
    md_resp = verifier_client.get(f"/api/runs/{run_id}/report.md")
    assert md_resp.status_code == 200
    md = md_resp.text
    assert "本次引入的回归" in md
    assert "无法判断" in md  # 限制声明里提到
    assert "alice-password" not in md
    assert not re.search(r"Bearer [A-Za-z0-9_\-]{16,}", md), "报告泄露了 Bearer token"
    assert "read-other-order-denied" in md
    assert "[REDACTED]" in md


def test_demo_unreachable_candidate_is_unverifiable(verifier_client, baseline_server, dead_url):
    httpx.post(baseline_server + "/api/debug/reset", timeout=5)
    task_id = create_demo_task(verifier_client, baseline_server, dead_url)
    run_id = verifier_client.post(f"/api/tasks/{task_id}/runs", json={}).json()["run_id"]
    data = wait_run(verifier_client, run_id)

    assert data["run"]["status"] == "completed"
    result = data["result"]
    assert all(c["classification"]["code"] == "unverifiable" for c in result["checks"])
    assert result["summary"]["counts"] == {"unverifiable": 10}
    assert "无法判断" in result["summary"]["headline"]
    # 超时/连接失败不能算作业务失败
    assert result["summary"]["counts"].get("regression") is None
