"""多场景 FastAPI 接口改动与契约演进端到端测试。

覆盖后端 PR 门禁中常见的五类接口演进与回归模式：
1. 列表分页与最大页长上限（`limit` 上限保护与 `v1 -> v2` 规则精度对比）
2. 多仓库租户隔离与跨仓越权防护（`repo_id` 归属校验 + 动态凭据脱敏）
3. 基于 `{{nonce}}` 与动态变量提取的内容幂等去重（保留原始类型对比）
4. 受控契约演进（`expected_change_ref` 放行合法结构升级、拦截未达标破坏性改动）
5. `ActionDraft` 人工确认门禁、SQLite 规则草稿确认门禁与历史遗留缺陷隔离（`pre_existing`）
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from typing import Any, Optional

import pytest
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from tests.conftest import free_port, start_server, wait_ready
from verifier import cli
from verifier.db import SCHEMA, now_iso
from verifier.models import validate_checks
from verifier.runner import run_checks

USERS = {
    "alice": {
        "id": "u-alice",
        "password": "alice-dev-secret-pw",
        "role": "maintainer",
        "repos": {"repo-alpha"},
    },
    "viewer": {
        "id": "u-viewer",
        "password": "viewer-dev-secret-pw",
        "role": "viewer",
        "repos": {"repo-alpha"},
    },
    "bob": {
        "id": "u-bob",
        "password": "bob-dev-secret-pw",
        "role": "maintainer",
        "repos": {"repo-beta"},
    },
}


class LoginRequest(BaseModel):
    username: str
    password: str


class DocumentIngestRequest(BaseModel):
    title: str
    content: str
    content_sha256: str


class DraftCreateRequest(BaseModel):
    repo_id: str
    draft_type: str = "pr_comment"
    title: str
    content: str


def create_repo_platform_app(mode: str, *, regressions: Optional[set[str]] = None) -> FastAPI:
    """构建用于契约对比的仓库协作服务（baseline vs candidate）。"""
    assert mode in ("baseline", "candidate")
    active_bugs = set(regressions or set())
    app = FastAPI(title=f"repo-platform-{mode}")
    lock = threading.Lock()

    seed_prs = [
        {
            "id": f"pr-{i:03d}",
            "repo_id": "repo-alpha",
            "number": i,
            "title": f"Refactor module {i}",
            "state": "open",
        }
        for i in range(1, 61)
    ]
    seed_feedback = [
        {
            "id": "fb-alpha-1",
            "repo_id": "repo-alpha",
            "rating": "helpful",
            "comment": "Accurate citation",
        },
        {
            "id": "fb-beta-1",
            "repo_id": "repo-beta",
            "rating": "unhelpful",
            "comment": "Internal beta roadmap note",
        },
    ]

    state: dict[str, Any] = {
        "tokens": {},
        "docs_by_key": {},
        "docs": {},
        "next_doc_id": 500,
        "drafts": {
            "draft-cancelled-1": {
                "id": "draft-cancelled-1",
                "repo_id": "repo-alpha",
                "draft_type": "close_issue",
                "title": "Close stale issue",
                "status": "cancelled",
            }
        },
        "next_draft_id": 900,
    }

    def require_user(authorization: Optional[str]) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        username = state["tokens"].get(authorization[len("Bearer ") :])
        if not username or username not in USERS:
            raise HTTPException(status_code=401, detail="Invalid token")
        return USERS[username]

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "mode": mode}

    @app.post("/api/auth/login")
    def login(payload: LoginRequest):
        user = USERS.get(payload.username)
        if user is None or user["password"] != payload.password:
            raise HTTPException(status_code=401, detail="Bad credentials")
        token = f"tok-{payload.username}-{secrets.token_hex(8)}"
        with lock:
            state["tokens"][token] = payload.username
        return {
            "access_token": token,
            "user": {"id": user["id"], "username": payload.username, "role": user["role"]},
        }

    @app.get("/api/repos/{repo_id}/pull-requests")
    def list_pull_requests(
        repo_id: str,
        limit: int = Query(default=20),
        offset: int = Query(default=0),
        authorization: Optional[str] = Header(default=None),
    ):
        user = require_user(authorization)
        if repo_id not in user["repos"]:
            raise HTTPException(status_code=403, detail="Repo access denied")
        effective_offset = max(offset, 0)
        if "pagination_cap_missing" in active_bugs:
            effective_limit = max(limit, 1)
        else:
            effective_limit = min(max(limit, 1), 50)
        matched = [pr for pr in seed_prs if pr["repo_id"] == repo_id]
        items = matched[effective_offset : effective_offset + effective_limit]
        return {"items": items, "total": len(matched), "limit": effective_limit, "offset": effective_offset}

    @app.get("/api/feedback")
    def list_feedback(
        repo_id: str = Query(...),
        authorization: Optional[str] = Header(default=None),
    ):
        user = require_user(authorization)
        if "cross_repo_leak" not in active_bugs and repo_id not in user["repos"]:
            raise HTTPException(status_code=403, detail="Cross-repo access denied")
        items = [fb for fb in seed_feedback if fb["repo_id"] == repo_id]
        return {"items": items, "count": len(items)}

    @app.post("/api/repos/{repo_id}/documents", status_code=201)
    def ingest_document(
        repo_id: str,
        payload: DocumentIngestRequest,
        authorization: Optional[str] = Header(default=None),
    ):
        user = require_user(authorization)
        if repo_id not in user["repos"]:
            raise HTTPException(status_code=403, detail="Repo access denied")
        dedupe_key = (repo_id, payload.content_sha256)
        with lock:
            if "idempotency_broken" not in active_bugs and dedupe_key in state["docs_by_key"]:
                existing = dict(state["docs"][state["docs_by_key"][dedupe_key]])
                existing["deduplicated"] = True
                return existing
            doc_id = state["next_doc_id"]
            state["next_doc_id"] += 1
            record = {
                "id": doc_id,
                "repo_id": repo_id,
                "title": payload.title,
                "content_sha256": payload.content_sha256,
                "deduplicated": False,
            }
            state["docs"][doc_id] = record
            state["docs_by_key"][dedupe_key] = doc_id
            return record

    @app.get("/api/pull-requests/{pr_id}/analysis")
    def get_pr_analysis(pr_id: str, authorization: Optional[str] = Header(default=None)):
        require_user(authorization)
        if mode == "baseline":
            return {
                "pr_id": pr_id,
                "verdict": "approve",
                "confidence_score": 0.92,
            }
        if "incomplete_schema_migration" in active_bugs:
            return {
                "pr_id": pr_id,
                "verdict": "approve",
                "evidence_sufficiency": {"status": "sufficient"},
            }
        return {
            "pr_id": pr_id,
            "verdict": "approve",
            "evidence_sufficiency": {
                "status": "sufficient",
                "citation_count": 4,
            },
        }

    @app.post("/api/action-drafts", status_code=201)
    def create_draft(payload: DraftCreateRequest, authorization: Optional[str] = Header(default=None)):
        user = require_user(authorization)
        if payload.repo_id not in user["repos"]:
            raise HTTPException(status_code=403, detail="Repo access denied")
        with lock:
            did = f"draft-{state['next_draft_id']}"
            state["next_draft_id"] += 1
            draft = {
                "id": did,
                "repo_id": payload.repo_id,
                "draft_type": payload.draft_type,
                "title": payload.title,
                "status": "pending_confirmation",
            }
            state["drafts"][did] = draft
            return draft

    @app.post("/api/action-drafts/{draft_id}/confirm")
    def confirm_draft(draft_id: str, authorization: Optional[str] = Header(default=None)):
        user = require_user(authorization)
        with lock:
            draft = state["drafts"].get(draft_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="Draft not found")
            if "draft_gate_bypass" not in active_bugs:
                if user["role"] != "maintainer":
                    raise HTTPException(status_code=403, detail="Requires maintainer approval")
                if draft["status"] != "pending_confirmation":
                    raise HTTPException(status_code=400, detail="Only pending drafts can be confirmed")
            draft["status"] = "executed"
            return {"draft": dict(draft), "executed_by": user["id"]}

    @app.get("/api/action-drafts/legacy-broken/{draft_id}")
    def get_legacy_broken_draft(draft_id: str, authorization: Optional[str] = Header(default=None)):
        require_user(authorization)
        # 两个环境共有的历史遗留缺陷（未捕获 KeyError -> 500），用于验证 pre_existing 归类不误报为回归
        return state["drafts"][draft_id]

    return app


def _login_step(username: str = "alice") -> dict:
    return {
        "name": f"登录 {username}",
        "method": "POST",
        "path": "/api/auth/login",
        "json": {"username": username, "password": USERS[username]["password"]},
        "extract": {"token": {"source": "json", "path": "$.access_token", "sensitive": True}},
        "assert": [{"type": "status", "op": "equals", "value": 200}],
    }


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer {{token}}"}


def build_platform_checks(*, enforce_max_limit_cap: bool = True) -> list[dict]:
    """构造覆盖 5 类场景的已确认验收规则集。"""
    pagination_steps = [
        _login_step("alice"),
        {
            "name": "常规分页 limit=5&offset=0",
            "method": "GET",
            "path": "/api/repos/repo-alpha/pull-requests?limit=5&offset=0",
            "headers": _bearer(),
            "assert": [
                {"type": "status", "op": "equals", "value": 200},
                {"type": "json", "path": "$.items", "op": "length_equals", "value": 5},
                {"type": "json", "path": "$.total", "op": "equals", "value": 60},
            ],
        },
    ]
    if enforce_max_limit_cap:
        pagination_steps.append(
            {
                "name": "超大 limit=200 请求被截断在上限 50 条以内",
                "method": "GET",
                "path": "/api/repos/repo-alpha/pull-requests?limit=200&offset=0",
                "headers": _bearer(),
                "assert": [
                    {"type": "status", "op": "equals", "value": 200},
                    {"type": "json", "path": "$.items", "op": "length_less_than", "value": 51},
                ],
            }
        )

    return [
        {
            "id": "pr-list-pagination-cap",
            "name": "PR 列表分页切片与单页最大条数上限保护",
            "category": "existing_behavior",
            "expectation": "limit=5 返回 5 条；limit=200 时单页最多返回 50 条",
            "steps": pagination_steps,
        },
        {
            "id": "feedback-cross-repo-isolated",
            "name": "跨仓库查询反馈列表必须拒绝访问",
            "category": "existing_behavior",
            "expectation": "仅拥有 repo-alpha 权限的 alice 访问 repo-beta 反馈列表必须返回 403",
            "steps": [
                _login_step("alice"),
                {
                    "name": "读取本仓库反馈正常返回",
                    "method": "GET",
                    "path": "/api/feedback?repo_id=repo-alpha",
                    "headers": _bearer(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.count", "op": "equals", "value": 1},
                    ],
                },
                {
                    "name": "越权读取其他仓库反馈被拦截",
                    "method": "GET",
                    "path": "/api/feedback?repo_id=repo-beta",
                    "headers": _bearer(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 403},
                        {"type": "json", "path": "$.items", "op": "not_exists"},
                    ],
                },
            ],
        },
        {
            "id": "document-sha256-idempotent",
            "name": "相同 content_sha256 重复提交文档保持幂等",
            "category": "existing_behavior",
            "expectation": "同一 {{nonce}} 摘要连续入库两次返回相同整数文档 ID",
            "steps": [
                _login_step("alice"),
                {
                    "name": "首次入库文档",
                    "method": "POST",
                    "path": "/api/repos/repo-alpha/documents",
                    "headers": _bearer(),
                    "json": {
                        "title": "Architecture Spec",
                        "content": "Immutable rule semantics",
                        "content_sha256": "sha256-{{nonce}}",
                    },
                    "extract": {"first_doc_id": {"source": "json", "path": "$.id", "sensitive": False}},
                    "assert": [
                        {"type": "status", "op": "equals", "value": 201},
                        {"type": "json", "path": "$.id", "op": "type", "value": "integer"},
                    ],
                },
                {
                    "name": "重复入库同一摘要文档",
                    "method": "POST",
                    "path": "/api/repos/repo-alpha/documents",
                    "headers": _bearer(),
                    "json": {
                        "title": "Architecture Spec",
                        "content": "Immutable rule semantics",
                        "content_sha256": "sha256-{{nonce}}",
                    },
                    "assert": [
                        {"type": "status", "op": "in", "value": [200, 201]},
                        {"type": "json", "path": "$.id", "op": "equals", "value": "{{first_doc_id}}"},
                        {"type": "json", "path": "$.deduplicated", "op": "equals", "value": True},
                    ],
                },
            ],
        },
        {
            "id": "pr-analysis-legacy-confidence",
            "name": "PR 评审旧版返回 confidence_score 数值字段",
            "category": "existing_behavior",
            "expected_change_ref": "pr-analysis-evidence-sufficiency",
            "expectation": "旧版包含 confidence_score，升级后由结构化 evidence_sufficiency 替代",
            "steps": [
                _login_step("alice"),
                {
                    "name": "读取 PR 分析结果（旧字段断言）",
                    "method": "GET",
                    "path": "/api/pull-requests/pr-001/analysis",
                    "headers": _bearer(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.confidence_score", "op": "type", "value": "number"},
                    ],
                },
            ],
        },
        {
            "id": "pr-analysis-evidence-sufficiency",
            "name": "PR 评审新版返回结构化证据充分度字段",
            "category": "new_requirement",
            "expectation": "返回 evidence_sufficiency.status 与大于 0 的 citation_count",
            "steps": [
                _login_step("alice"),
                {
                    "name": "读取 PR 分析结果（新结构断言）",
                    "method": "GET",
                    "path": "/api/pull-requests/pr-001/analysis",
                    "headers": _bearer(),
                    "assert": [
                        {"type": "status", "op": "equals", "value": 200},
                        {"type": "json", "path": "$.evidence_sufficiency.status", "op": "equals", "value": "sufficient"},
                        {"type": "json", "path": "$.evidence_sufficiency.citation_count", "op": "greater_than", "value": 0},
                    ],
                },
            ],
        },
        {
            "id": "action-draft-confirmation-gate",
            "name": "ActionDraft 必须由审批角色在待确认状态下执行",
            "category": "existing_behavior",
            "expectation": "viewer 角色执行确认返回 403，且已取消草稿再次确认返回 400",
            "steps": [
                _login_step("alice"),
                {
                    "name": "maintainer 创建待确认草稿",
                    "method": "POST",
                    "path": "/api/action-drafts",
                    "headers": _bearer(),
                    "json": {
                        "repo_id": "repo-alpha",
                        "draft_type": "pr_comment",
                        "title": "Post review summary {{nonce}}",
                        "content": "LGTM with notes",
                    },
                    "extract": {"pending_draft_id": {"source": "json", "path": "$.id", "sensitive": False}},
                    "assert": [
                        {"type": "status", "op": "equals", "value": 201},
                        {"type": "json", "path": "$.status", "op": "equals", "value": "pending_confirmation"},
                    ],
                },
                {
                    "name": "已取消草稿尝试执行确认必须返回 400",
                    "method": "POST",
                    "path": "/api/action-drafts/draft-cancelled-1/confirm",
                    "headers": _bearer(),
                    "assert": [{"type": "status", "op": "equals", "value": 400}],
                },
                _login_step("viewer"),
                {
                    "name": "无审批权限的 viewer 尝试确认草稿必须返回 403",
                    "method": "POST",
                    "path": "/api/action-drafts/{{pending_draft_id}}/confirm",
                    "headers": _bearer(),
                    "assert": [{"type": "status", "op": "equals", "value": 403}],
                },
            ],
        },
        {
            "id": "legacy-draft-missing-404",
            "name": "查询不存在的旧版草稿应返回 404（已知历史缺陷）",
            "category": "existing_behavior",
            "expectation": "访问不存在的草稿理想应返回 404，但两个版本均因未捕获 KeyError 返回 500",
            "steps": [
                _login_step("alice"),
                {
                    "name": "访问不存在的旧版草稿路径",
                    "method": "GET",
                    "path": "/api/action-drafts/legacy-broken/non-existent-id",
                    "headers": _bearer(),
                    "assert": [{"type": "status", "op": "equals", "value": 404}],
                },
            ],
        },
    ]


class _RunningService:
    def __init__(self, mode: str, regressions: Optional[set[str]] = None):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server, self._thread = start_server(create_repo_platform_app(mode, regressions=regressions), self.port)
        wait_ready(self.url)

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(5)

    def __enter__(self) -> str:
        return self.url

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


@pytest.fixture(scope="module")
def baseline_platform_url():
    svc = _RunningService("baseline")
    yield svc.url
    svc.close()


def test_clean_candidate_passes_gate_with_zero_false_positives_and_valid_v1_report(tmp_path, baseline_platform_url):
    """无回归候选版本：涵盖 expected_change 与 pre_existing 时，CLI 门禁返回 0 且 JSON 报告满足 v1 契约。"""
    checks = build_platform_checks(enforce_max_limit_cap=True)
    assert validate_checks(checks) == []

    rules_file = tmp_path / "platform_rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "name": "Repo Platform API 改动验收",
                "version": 2,
                "note": "覆盖分页上限、跨仓隔离、SHA-256 幂等、评审结构演进与草稿确认门禁",
                "checks": checks,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    md_out = tmp_path / "acceptance.md"
    json_out = tmp_path / "acceptance.json"

    with _RunningService("candidate", regressions=set()) as cand_url:
        exit_code = cli.main(
            [
                "verify",
                "--rules",
                str(rules_file),
                "--baseline",
                baseline_platform_url,
                "--candidate",
                cand_url,
                "--output",
                str(md_out),
                "--json-output",
                str(json_out),
                "--quiet",
            ]
        )

    assert exit_code == 0
    raw_json_text = json_out.read_text(encoding="utf-8")
    raw_md_text = md_out.read_text(encoding="utf-8")

    for secret_pw in (USERS["alice"]["password"], USERS["viewer"]["password"]):
        assert secret_pw not in raw_json_text
        assert secret_pw not in raw_md_text
    assert "tok-alice-" not in raw_json_text
    assert "tok-viewer-" not in raw_json_text
    assert "[REDACTED]" in raw_json_text

    report = json.loads(raw_json_text)
    assert report["report_version"] == 1
    assert report["rule_version"]["version"] == 2
    assert report["rule_version"]["status"] == "confirmed"
    assert report["run"]["status"] == "completed"

    by_id = {c["id"]: c["classification"]["code"] for c in report["checks"]}
    assert by_id == {
        "pr-list-pagination-cap": "pass",
        "feedback-cross-repo-isolated": "pass",
        "document-sha256-idempotent": "pass",
        "pr-analysis-legacy-confidence": "expected_change",
        "pr-analysis-evidence-sufficiency": "satisfied",
        "action-draft-confirmation-gate": "pass",
        "legacy-draft-missing-404": "pre_existing",
    }
    assert report["summary"]["counts"].get("regression", 0) == 0
    assert report["summary"]["counts"].get("not_met", 0) == 0


def test_rule_evolution_v1_misses_limit_cap_while_v2_catches_regression(baseline_platform_url):
    """仅断言常规 limit=5 的初版规则会漏检上限失效，补充超大 limit 边界断言后准确捕获回归。"""
    v1_checks = build_platform_checks(enforce_max_limit_cap=False)
    v2_checks = build_platform_checks(enforce_max_limit_cap=True)

    with _RunningService("candidate", regressions={"pagination_cap_missing"}) as cand_url:
        v1_res = run_checks(v1_checks, baseline_platform_url, cand_url)["payload"]
        v2_res = run_checks(v2_checks, baseline_platform_url, cand_url)["payload"]

    v1_by_id = {c["id"]: c["classification"]["code"] for c in v1_res["checks"]}
    v2_by_id = {c["id"]: c["classification"]["code"] for c in v2_res["checks"]}
    assert v1_by_id["pr-list-pagination-cap"] == "pass"
    assert v2_by_id["pr-list-pagination-cap"] == "regression"
    assert v2_res["summary"]["counts"]["regression"] == 1


@pytest.mark.parametrize(
    ("bug_name", "expected_regressions", "expected_not_met"),
    [
        ("pagination_cap_missing", {"pr-list-pagination-cap"}, set()),
        ("cross_repo_leak", {"feedback-cross-repo-isolated"}, set()),
        ("idempotency_broken", {"document-sha256-idempotent"}, set()),
        ("draft_gate_bypass", {"action-draft-confirmation-gate"}, set()),
        (
            "incomplete_schema_migration",
            {"pr-analysis-legacy-confidence"},
            {"pr-analysis-evidence-sufficiency"},
        ),
    ],
)
def test_isolated_regression_pinpointed_without_collateral_false_positives(
    baseline_platform_url, bug_name: str, expected_regressions: set[str], expected_not_met: set[str]
):
    checks = build_platform_checks(enforce_max_limit_cap=True)
    with _RunningService("candidate", regressions={bug_name}) as cand_url:
        payload = run_checks(checks, baseline_platform_url, cand_url)["payload"]

    by_id = {c["id"]: c["classification"]["code"] for c in payload["checks"]}
    actual_regressions = {cid for cid, code in by_id.items() if code == "regression"}
    actual_not_met = {cid for cid, code in by_id.items() if code == "not_met"}

    assert actual_regressions == expected_regressions
    assert actual_not_met == expected_not_met
    assert by_id["legacy-draft-missing-404"] == "pre_existing"


def test_sqlite_rule_confirmation_gate_blocks_draft_until_confirmed(tmp_path, baseline_platform_url):
    """验证 SQLite 规则草稿未确认时无头门禁返回退出码 2，追加确认版本后正常放行无回归候选实例。"""
    db_file = tmp_path / "rules_gate.db"
    checks = build_platform_checks(enforce_max_limit_cap=True)

    with _RunningService("candidate", regressions=set()) as cand_url:
        conn = sqlite3.connect(str(db_file))
        try:
            conn.executescript(SCHEMA)
            ts = now_iso()
            cur = conn.execute(
                """INSERT INTO tasks (name, requirement, baseline_url, candidate_url, allow_remote, allow_remote_writes,
                                      endpoints_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, 0, '[]', ?, ?)""",
                ("SQLite 确认门禁验收", "未确认草稿不得直接放行", baseline_platform_url, cand_url, ts, ts),
            )
            task_id = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO rule_versions (task_id, version, checks_json, status, note, created_at, confirmed_at)
                   VALUES (?, 1, ?, 'draft', 'v1 draft', ?, NULL)""",
                (task_id, json.dumps(checks, ensure_ascii=False), ts),
            )
            conn.commit()
        finally:
            conn.close()

        # 仅有 draft 版本时拒绝运行（退出码 2）
        assert cli.main(["verify", "--db", str(db_file), "--task-id", str(task_id), "--quiet"]) == 2

        # 人工审查确认追加不可变 v2 后，门禁正常执行并通过（退出码 0）
        conn = sqlite3.connect(str(db_file))
        try:
            ts2 = now_iso()
            conn.execute(
                """INSERT INTO rule_versions (task_id, version, checks_json, status, note, created_at, confirmed_at)
                   VALUES (?, 2, ?, 'confirmed', 'v2 confirmed', ?, ?)""",
                (task_id, json.dumps(checks, ensure_ascii=False), ts2, ts2),
            )
            conn.commit()
        finally:
            conn.close()

        assert cli.main(["verify", "--db", str(db_file), "--task-id", str(task_id), "--quiet"]) == 0
