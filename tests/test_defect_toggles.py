"""缺陷开关（DEMO_DEFECTS / create_app(defects=...)）的行为测试。

关闭某个缺陷后，对应行为必须与「正确实现」一致；开启时只影响它自己对应的行为。
这是变异评估（scripts/mutation_eval.py）的底层机制，先在这里把语义钉死。
"""

from __future__ import annotations

import httpx
import pytest


def test_parse_defects_env():
    from demo.services import parse_defects

    assert parse_defects(None) is None
    assert parse_defects("") is None
    assert parse_defects("   ") is None
    assert parse_defects("none") == set()
    assert parse_defects("NONE") == set()
    assert parse_defects("a, c") == {"A", "C"}
    assert parse_defects("A，C") == {"A", "C"}  # 中文逗号也接受
    with pytest.raises(ValueError):
        parse_defects("X")  # 拼错必须报错，不能静默变成「无缺陷」


def _login(c: httpx.Client) -> dict:
    r = c.post("/api/login", json={"username": "alice", "password": "alice-password"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_candidate_without_defects_behaves_correctly(start_demo_service):
    base = start_demo_service("candidate", defects=set())
    with httpx.Client(base_url=base, timeout=10) as c:
        assert c.get("/api/meta/defects").json()["defects"] == []
        h = _login(c)
        assert c.get("/api/orders/102", headers=h).status_code == 403          # A 关闭：跨用户读被拒
        assert c.get("/api/orders", headers={}).status_code == 401             # D 关闭：未登录被拒
        assert c.get("/api/admin/users", headers=h).status_code == 403         # B 关闭：普通用户被拒
        lst = c.get("/api/orders?page_size=1", headers=h).json()               # E 关闭：分页生效
        assert len(lst["items"]) == 1 and lst["total"] > 0
        key = "toggle-test-idem"
        first = c.post("/api/orders", headers={**h, "Idempotency-Key": key}, json={"item": "x", "quantity": 1}).json()
        second = c.post("/api/orders", headers={**h, "Idempotency-Key": key}, json={"item": "x", "quantity": 1}).json()
        assert second["id"] == first["id"]                                     # C 关闭：幂等生效


def test_single_defect_only_affects_its_own_behavior(start_demo_service):
    base = start_demo_service("candidate", defects={"A"})
    with httpx.Client(base_url=base, timeout=10) as c:
        assert [d["id"] for d in c.get("/api/meta/defects").json()["defects"]] == ["A"]
        h = _login(c)
        assert c.get("/api/orders/102", headers=h).status_code == 200          # A 开启：跨用户可读
        assert c.get("/api/orders", headers={}).status_code == 401             # D 关闭
        assert c.get("/api/admin/users", headers=h).status_code == 403         # B 关闭
        lst = c.get("/api/orders?page_size=1", headers=h).json()               # E 关闭
        assert len(lst["items"]) == 1


def test_baseline_never_has_defects(start_demo_service):
    base = start_demo_service("baseline")
    with httpx.Client(base_url=base, timeout=10) as c:
        assert c.get("/api/meta/defects").json()["defects"] == []
        h = _login(c)
        assert c.get("/api/orders/102", headers=h).status_code == 403
