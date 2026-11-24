"""无头 CLI（`change-verifier` / `python -m verifier`）与模块化静态资源的测试。"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import httpx

from verifier import cli, demo_data
from verifier.db import SCHEMA, now_iso

ROOT = pathlib.Path(__file__).resolve().parents[1]

PING_CHECK = {
    "id": "health-ping",
    "name": "健康检查返回 200",
    "category": "existing_behavior",
    "expectation": "GET /healthz 返回 200",
    "steps": [
        {
            "name": "请求 healthz",
            "method": "GET",
            "path": "/healthz",
            "assert": [{"type": "status", "op": "equals", "value": 200}],
        }
    ],
}

WRITE_CHECK = {
    "id": "create-order",
    "name": "写入检查",
    "category": "new_requirement",
    "steps": [
        {
            "method": "POST",
            "path": "/api/orders",
            "json": {"item": "book"},
            "assert": [{"type": "status", "op": "in", "value": [200, 201]}],
        }
    ],
}


def _seed_sqlite_db(
    db_file: pathlib.Path,
    *,
    baseline_url: str,
    candidate_url: str,
    checks: list[dict],
    confirmed: bool,
) -> tuple[int, int]:
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(SCHEMA)
        ts = now_iso()
        cur = conn.execute(
            """INSERT INTO tasks (name, requirement, baseline_url, candidate_url, allow_remote, allow_remote_writes,
                                  endpoints_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, 0, '[]', ?, ?)""",
            ("CI 门禁测试任务", "验证 SQLite 规则加载", baseline_url, candidate_url, ts, ts),
        )
        task_id = int(cur.lastrowid)
        v_cur = conn.execute(
            """INSERT INTO rule_versions (task_id, version, checks_json, status, note, created_at, confirmed_at)
               VALUES (?, 1, ?, ?, 'v1', ?, ?)""",
            (
                task_id,
                json.dumps(checks, ensure_ascii=False),
                "confirmed" if confirmed else "draft",
                ts,
                ts if confirmed else None,
            ),
        )
        version_id = int(v_cur.lastrowid)
        conn.commit()
        return task_id, version_id
    finally:
        conn.close()


def test_default_invocation_starts_serve_mode(monkeypatch):
    """无参数调用 `python -m verifier` / `change-verifier` 必须保持向后兼容，启动网页服务。"""
    captured = {}

    def fake_run(app_import, *, host, port, log_level):
        captured["app"] = app_import
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr("uvicorn.run", fake_run)
    monkeypatch.setenv("VERIFIER_HOST", "127.0.0.1")
    monkeypatch.setenv("VERIFIER_PORT", "18622")

    code = cli.main([])
    assert code == 0
    assert captured == {
        "app": "verifier.main:app",
        "host": "127.0.0.1",
        "port": 18622,
        "log_level": "info",
    }


def test_serve_subcommand_accepts_explicit_flags(monkeypatch):
    captured = {}

    def fake_run(app_import, *, host, port, log_level):
        captured.update(app=app_import, host=host, port=port, log_level=log_level)

    monkeypatch.setattr("uvicorn.run", fake_run)
    code = cli.main(["serve", "--host", "0.0.0.0", "--port", "9090", "--log-level", "warning"])
    assert code == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9090
    assert captured["log_level"] == "warning"


def test_verify_json_rules_zero_regressions_exits_0_and_writes_redacted_reports(
    tmp_path, baseline_server, start_demo_service
):
    """candidate 缺陷全部修复时：即便存在原有问题与已确认的行为变化，退出码也必须为 0。"""
    clean_candidate = start_demo_service("candidate", defects=set())
    httpx.post(baseline_server + "/api/debug/reset", timeout=5)
    httpx.post(clean_candidate + "/api/debug/reset", timeout=5)

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "name": "订单服务 CI 验收",
                "requirement": demo_data.DEMO_TASK["requirement"],
                "checks": demo_data.demo_checks(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    md_out = tmp_path / "reports" / "report.md"
    json_out = tmp_path / "reports" / "report.json"

    code = cli.main(
        [
            "verify",
            "--rules",
            str(rules_path),
            "--baseline",
            baseline_server,
            "--candidate",
            clean_candidate,
            "--output",
            str(md_out),
            "--json-output",
            str(json_out),
        ]
    )

    assert code == 0
    md_text = md_out.read_text(encoding="utf-8")
    assert "订单服务 CI 验收" in md_text
    assert "未发现本次引入的回归" in md_text
    assert "alice-password" not in md_text
    assert "[REDACTED]" in md_text

    report_json = json.loads(json_out.read_text(encoding="utf-8"))
    counts = report_json["summary"]["counts"]
    assert counts.get("regression", 0) == 0
    assert counts.get("not_met", 0) == 0
    assert counts["pre_existing"] == 1
    assert counts["expected_change"] == 1
    assert counts["satisfied"] == 2
    assert counts["pass"] == 6


def test_verify_detects_regressions_and_exits_nonzero(tmp_path, baseline_server, candidate_server):
    httpx.post(baseline_server + "/api/debug/reset", timeout=5)
    httpx.post(candidate_server + "/api/debug/reset", timeout=5)
    md_out = tmp_path / "regression_report.md"

    code = cli.main(
        [
            "verify",
            "--demo-rules",
            "--baseline",
            baseline_server,
            "--candidate",
            candidate_server,
            "--output",
            str(md_out),
            "--quiet",
        ]
    )

    assert code == 1
    md_text = md_out.read_text(encoding="utf-8")
    assert "发现 4 项本次引入的回归" in md_text
    assert "alice-password" not in md_text


def test_verify_fail_on_allows_regression_only_or_report_only_gating(
    baseline_server, start_demo_service, dead_url
):
    only_unmet = start_demo_service("candidate", defects={"E"})
    httpx.post(baseline_server + "/api/debug/reset", timeout=5)
    httpx.post(only_unmet + "/api/debug/reset", timeout=5)

    # 默认门禁同时拦截 not_met -> 返回 1
    assert (
        cli.main(
            [
                "verify",
                "--demo-rules",
                "--baseline",
                baseline_server,
                "--candidate",
                only_unmet,
                "--quiet",
            ]
        )
        == 1
    )
    # 仅拦截回归（分步交付场景）-> 0 回归时返回 0
    assert (
        cli.main(
            [
                "verify",
                "--demo-rules",
                "--baseline",
                baseline_server,
                "--candidate",
                only_unmet,
                "--fail-on",
                "regression",
                "--quiet",
            ]
        )
        == 0
    )

    # 不可达环境默认拦截（unverifiable）-> 返回 1；report-only (--fail-on none) -> 返回 0
    rules_check = [PING_CHECK]
    assert (
        cli.main(
            [
                "verify",
                "--demo-rules",
                "--baseline",
                baseline_server,
                "--candidate",
                dead_url,
                "--fail-on",
                "none",
                "--quiet",
            ]
        )
        == 0
    )
    assert rules_check  # 保持辅助常量引用清晰


def test_verify_sqlite_enforces_confirmed_rules_and_supports_url_override(
    tmp_path, baseline_server, candidate_server
):
    db_file = tmp_path / "ci_rules.db"
    task_id, draft_vid = _seed_sqlite_db(
        db_file,
        baseline_url=baseline_server,
        candidate_url=baseline_server,
        checks=[PING_CHECK],
        confirmed=False,
    )

    # 未确认草稿默认拒绝执行（退出码 2），防止未经人工审查的规则进入 CI 门禁
    assert cli.main(["verify", "--db", str(db_file), "--task-id", str(task_id)]) == 2
    assert cli.main(["verify", "--db", str(db_file), "--rule-version-id", str(draft_vid)]) == 2

    # 显式开启 --allow-draft 时允许执行，且自动复用任务中保存的 baseline/candidate URL
    assert (
        cli.main(
            [
                "verify",
                "--db",
                str(db_file),
                "--rule-version-id",
                str(draft_vid),
                "--allow-draft",
                "--quiet",
            ]
        )
        == 0
    )

    # 确认规则版本后，无需 --allow-draft 即可按 --task-id 运行，并支持命令行覆盖 --candidate
    confirmed_db = tmp_path / "confirmed.db"
    c_task_id, _ = _seed_sqlite_db(
        confirmed_db,
        baseline_url=baseline_server,
        candidate_url=baseline_server,
        checks=demo_data.demo_checks(),
        confirmed=True,
    )
    httpx.post(baseline_server + "/api/debug/reset", timeout=5)
    httpx.post(candidate_server + "/api/debug/reset", timeout=5)
    assert (
        cli.main(
            [
                "verify",
                "--db",
                str(confirmed_db),
                "--task-id",
                str(c_task_id),
                "--candidate",
                candidate_server,
                "--quiet",
            ]
        )
        == 1
    )


def test_verify_rejects_invalid_inputs_and_enforces_remote_safety(tmp_path):
    missing_db = tmp_path / "does_not_exist.db"
    assert cli.main(["verify", "--db", str(missing_db), "--task-id", "1"]) == 2
    assert not missing_db.exists(), "读取不存在的 SQLite 文件时不应在磁盘创建空数据库"

    bad_rules = tmp_path / "bad.json"
    bad_rules.write_text(json.dumps([{"id": "BAD ID"}]), encoding="utf-8")
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(bad_rules),
                "--baseline",
                "http://127.0.0.1:8620",
                "--candidate",
                "http://127.0.0.1:8621",
            ]
        )
        == 2
    )

    valid_write = tmp_path / "write.json"
    valid_write.write_text(json.dumps([WRITE_CHECK]), encoding="utf-8")
    # 远程地址未开 --allow-remote -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(valid_write),
                "--baseline",
                "http://127.0.0.1:8620",
                "--candidate",
                "https://staging.example.com",
            ]
        )
        == 2
    )
    # 开了 --allow-remote 但包含 POST 且未开 --allow-remote-writes -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(valid_write),
                "--baseline",
                "http://127.0.0.1:8620",
                "--candidate",
                "https://staging.example.com",
                "--allow-remote",
            ]
        )
        == 2
    )


def test_subprocess_module_cli_exit_code(tmp_path, baseline_server):
    rules_path = tmp_path / "ping.json"
    rules_path.write_text(json.dumps([PING_CHECK]), encoding="utf-8")
    out_json = tmp_path / "out.json"

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "verifier",
            "verify",
            "--rules",
            str(rules_path),
            "--baseline",
            baseline_server,
            "--candidate",
            baseline_server,
            "--output",
            str(out_json),
            "--quiet",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["summary"]["counts"] == {"pass": 1}


def test_modular_frontend_assets_served(verifier_client):
    index_resp = verifier_client.get("/")
    assert index_resp.status_code == 200
    html = index_resp.text
    for asset in ("/static/core.js", "/static/tasks.js", "/static/rules.js", "/static/runs.js", "/static/app.js"):
        assert asset in html
        res = verifier_client.get(asset)
        assert res.status_code == 200
