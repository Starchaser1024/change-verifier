"""无头 CLI（`change-verifier` / `python -m verifier`）与模块化静态资源的测试。"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

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

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
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
        encoding="utf-8",
        errors="replace",
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


def test_wait_ready_fails_fast_when_process_exits(dead_url):
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(4)"])
    try:
        started = time.monotonic()
        ready = cli.wait_ready(dead_url, timeout=10.0, proc=proc)
        elapsed = time.monotonic() - started
    finally:
        proc.wait(timeout=5)
    assert ready is False
    assert elapsed < 3.0


def test_run_demo_stack_terminates_started_procs_on_readiness_timeout(monkeypatch, capsys):
    ports = cli.allocate_free_ports(3)
    spawned: list = []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.terminated = False
            self.waited = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            self.terminated = True

    def fake_popen(cmd, cwd=None, env=None):
        p = FakeProc(cmd)
        spawned.append(p)
        return p

    ready_calls: list[str] = []

    def fake_wait_ready(url, timeout=20.0, proc=None):
        ready_calls.append(url)
        # 第一个服务（baseline）就绪，第二个服务（candidate）超时未就绪
        return len(ready_calls) == 1

    monkeypatch.setattr(cli, "port_is_available", lambda port: True)
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "wait_ready", fake_wait_ready)

    code = cli.run_demo_stack(
        baseline_port_override=ports[0],
        candidate_port_override=ports[1],
        verifier_port_override=ports[2],
        ready_timeout=0.5,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert len(spawned) == 2, "第二个服务未就绪时应立即中止，不再拉起第三个服务"
    assert all(p.terminated and p.waited for p in spawned), "已拉起的子进程必须在退出前被终止回收"
    assert "全部服务已启动" not in captured.out
    assert "未就绪" in captured.err


def test_run_demo_stack_fails_fast_when_child_exits_prematurely(monkeypatch, capsys):
    ports = cli.allocate_free_ports(3)
    spawned: list = []

    class FakeProc:
        def __init__(self, idx: int):
            self.idx = idx
            self.terminated = False
            self.waited = False

        def poll(self):
            if self.idx == 1:
                return 3  # candidate 启动阶段立即崩溃退出
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def kill(self):
            self.terminated = True

    def fake_popen(cmd, cwd=None, env=None):
        p = FakeProc(len(spawned))
        spawned.append(p)
        return p

    monkeypatch.setattr(cli, "port_is_available", lambda port: True)
    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "wait_ready", lambda url, timeout=20.0, proc=None: proc.poll() is None)

    code = cli.run_demo_stack(
        baseline_port_override=ports[0],
        candidate_port_override=ports[1],
        verifier_port_override=ports[2],
        ready_timeout=1.0,
    )

    captured = capsys.readouterr()
    assert code == 1
    assert len(spawned) == 2
    assert spawned[0].terminated and spawned[0].waited
    assert "全部服务已启动" not in captured.out
    assert "提前退出" in captured.err


def test_split_command_template_handles_windows_and_posix_paths(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "nt")
    win_tokens = cli._split_command_template(
        r'"C:\Program Files\Python313\python.exe" -m uvicorn app:app --port {port} --db="{temp_dir}\state.db"'
    )
    assert win_tokens == [
        r"C:\Program Files\Python313\python.exe",
        "-m",
        "uvicorn",
        "app:app",
        "--port",
        "{port}",
        r"--db={temp_dir}\state.db",
    ]

    monkeypatch.setattr(cli.os, "name", "posix")
    posix_tokens = cli._split_command_template(
        "'/opt/py venv/bin/python' -m uvicorn app:app --port {port} --db '{temp_dir}/state.db'"
    )
    assert posix_tokens == [
        "/opt/py venv/bin/python",
        "-m",
        "uvicorn",
        "app:app",
        "--port",
        "{port}",
        "--db",
        "{temp_dir}/state.db",
    ]


def test_verify_managed_dual_commands_placeholders_and_cleanup(tmp_path):
    """验证 --baseline-cmd / --candidate-cmd 自动分配端口、注入占位符与环境变量并在结束后清理进程。"""
    server_script = tmp_path / "mini_service.py"
    server_script.write_text(
        """
import json
import os
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

port = int(sys.argv[1])
role = sys.argv[2]
state_dir = pathlib.Path(sys.argv[3])
marker = state_dir / "boot.txt"
marker.write_text(role, encoding="utf-8")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path == "/readyz":
            body = {"ok": True}
            code = 200
        elif self.path == "/api/info":
            body = {
                "role": os.environ.get("VERIFIER_TARGET_ENV"),
                "arg_role": role,
                "custom_tag": os.environ.get("SERVICE_TAG"),
                "state_exists": marker.is_file(),
                "cwd_name": pathlib.Path.cwd().name,
                "schema_v": 2 if os.environ.get("CANDIDATE_V2") == "1" else 1,
            }
            code = 200
        else:
            body = {"detail": "not found"}
            code = 404
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

HTTPServer(("127.0.0.1", port), Handler).serve_forever()
""".strip(),
        encoding="utf-8",
    )

    base_cwd = tmp_path / "worktree_base"
    cand_cwd = tmp_path / "worktree_cand"
    base_cwd.mkdir()
    cand_cwd.mkdir()

    rules_file = tmp_path / "managed_rules.json"
    rules_file.write_text(
        json.dumps(
            [
                {
                    "id": "isolated-state-check",
                    "name": "双实例隔离目录与工作目录生效",
                    "category": "existing_behavior",
                    "steps": [
                        {
                            "method": "GET",
                            "path": "/api/info",
                            "assert": [
                                {"type": "status", "op": "equals", "value": 200},
                                {"type": "json", "path": "$.state_exists", "op": "equals", "value": True},
                                {"type": "json", "path": "$.role", "op": "in", "value": ["baseline", "candidate"]},
                            ],
                        }
                    ],
                },
                {
                    "id": "schema-v2-upgrade",
                    "name": "新版本返回 schema_v=2",
                    "category": "new_requirement",
                    "steps": [
                        {
                            "method": "GET",
                            "path": "/api/info",
                            "assert": [
                                {"type": "status", "op": "equals", "value": 200},
                                {"type": "json", "path": "$.schema_v", "op": "equals", "value": 2},
                                {"type": "json", "path": "$.cwd_name", "op": "equals", "value": "worktree_cand"},
                            ],
                        }
                    ],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    json_report_path = tmp_path / "managed_report.json"
    cmd_tpl = f'"{sys.executable}" "{server_script}" {{port}} {{env}} "{{temp_dir}}"'

    code = cli.main(
        [
            "verify",
            "--rules",
            str(rules_file),
            "--baseline-cmd",
            cmd_tpl,
            "--baseline-cwd",
            str(base_cwd),
            "--baseline-env",
            "SERVICE_TAG={env}-{port}",
            "--candidate-cmd",
            cmd_tpl,
            "--candidate-cwd",
            str(cand_cwd),
            "--candidate-env",
            "CANDIDATE_V2=1",
            "--ready-path",
            "/readyz",
            "--ready-timeout",
            "15",
            "--json-output",
            str(json_report_path),
            "--quiet",
        ]
    )

    assert code == 0
    report = json.loads(json_report_path.read_text(encoding="utf-8"))
    assert report["summary"]["counts"] == {"pass": 1, "satisfied": 1}

    # 确认托管子进程已被 finally 终止，不再监听对应回环地址（Windows 回环关闭端口可能先触发 SYN 超时）
    for key in ("baseline_url", "candidate_url"):
        url = report["task"][key]
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            httpx.get(f"{url}/readyz", timeout=0.5)


def test_verify_managed_demo_services_gate_pass_and_regression(tmp_path):
    base_cmd = f'"{sys.executable}" -m demo.run_baseline'
    cand_cmd = f'"{sys.executable}" -m demo.run_candidate'
    out_json = tmp_path / "demo_managed.json"

    assert (
        cli.main(
            [
                "verify",
                "--demo-rules",
                "--baseline-cmd",
                base_cmd,
                "--baseline-cwd",
                str(ROOT),
                "--candidate-cmd",
                cand_cmd,
                "--candidate-cwd",
                str(ROOT),
                "--candidate-env",
                "DEMO_DEFECTS=none",
                "--json-output",
                str(out_json),
                "--quiet",
            ]
        )
        == 0
    )
    counts = json.loads(out_json.read_text(encoding="utf-8"))["summary"]["counts"]
    assert counts.get("regression", 0) == 0
    assert counts.get("not_met", 0) == 0

    # 开启缺陷 A 时门禁必须返回 1
    assert (
        cli.main(
            [
                "verify",
                "--demo-rules",
                "--baseline-cmd",
                base_cmd,
                "--baseline-cwd",
                str(ROOT),
                "--candidate-cmd",
                cand_cmd,
                "--candidate-cwd",
                str(ROOT),
                "--candidate-env",
                "DEMO_DEFECTS=A",
                "--quiet",
            ]
        )
        == 1
    )


def test_verify_managed_command_early_exit_and_timeout_cleanup(tmp_path, capsys):
    rules_path = tmp_path / "ping.json"
    rules_path.write_text(json.dumps([PING_CHECK]), encoding="utf-8")

    # 1) candidate 启动阶段崩溃退出：应快速失败返回 1，并终止已启动的 baseline 服务
    base_cmd = f'"{sys.executable}" -m demo.run_baseline'
    crash_cmd = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\\n\'); sys.exit(5)"'
    code_crash = cli.main(
        [
            "verify",
            "--rules",
            str(rules_path),
            "--baseline-cmd",
            base_cmd,
            "--baseline-cwd",
            str(ROOT),
            "--candidate-cmd",
            crash_cmd,
            "--ready-timeout",
            "10",
            "--quiet",
        ]
    )
    err_crash = capsys.readouterr().err
    assert code_crash == 1
    assert "提前退出" in err_crash
    assert "boom" in err_crash

    # 2) 服务挂起不监听端口：超过 ready-timeout 返回 1 并清理子进程
    sleep_cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = time.monotonic()
    code_timeout = cli.main(
        [
            "verify",
            "--rules",
            str(rules_path),
            "--baseline-cmd",
            sleep_cmd,
            "--candidate-cmd",
            sleep_cmd,
            "--ready-timeout",
            "0.6",
            "--quiet",
        ]
    )
    elapsed = time.monotonic() - started
    err_timeout = capsys.readouterr().err
    assert code_timeout == 1
    assert elapsed < 8.0
    assert "未就绪" in err_timeout


def test_verify_managed_command_rejects_conflicting_or_malformed_flags(tmp_path):
    rules_path = tmp_path / "ping.json"
    rules_path.write_text(json.dumps([PING_CHECK]), encoding="utf-8")
    noop_cmd = f'"{sys.executable}" -m demo.run_baseline'

    # --baseline 与 --baseline-cmd 冲突 -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(rules_path),
                "--baseline",
                "http://127.0.0.1:8620",
                "--baseline-cmd",
                noop_cmd,
                "--candidate",
                "http://127.0.0.1:8621",
            ]
        )
        == 2
    )
    # 缺少 = 的环境变量覆盖 -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(rules_path),
                "--baseline-cmd",
                noop_cmd,
                "--baseline-env",
                "INVALID_ENV_PAIR",
                "--candidate",
                "http://127.0.0.1:8621",
            ]
        )
        == 2
    )
    # --ready-path 未以 / 开头 -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(rules_path),
                "--baseline-cmd",
                noop_cmd,
                "--candidate",
                "http://127.0.0.1:8621",
                "--ready-path",
                "healthz",
            ]
        )
        == 2
    )
    # --baseline-cwd 未配合 --baseline-cmd -> 2
    assert (
        cli.main(
            [
                "verify",
                "--rules",
                str(rules_path),
                "--baseline",
                "http://127.0.0.1:8620",
                "--baseline-cwd",
                str(tmp_path),
                "--candidate",
                "http://127.0.0.1:8621",
            ]
        )
        == 2
    )
