"""FastAPI 路由层公共数据访问与异常转换辅助函数。"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException

from ..db import now_iso
from ..safety import SafetyError, validate_target_url
from ..schemas import TaskIn


def get_task_or_404(conn, task_id: int) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    task = dict(row)
    task["endpoints"] = json.loads(task.pop("endpoints_json") or "[]")
    return task


def get_version_or_404(conn, version_id: int) -> dict:
    row = conn.execute("SELECT * FROM rule_versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"规则版本 {version_id} 不存在")
    version = dict(row)
    version["checks"] = json.loads(version.pop("checks_json"))
    return version


def get_run_or_404(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    run = dict(row)
    run["result"] = json.loads(run.pop("result_json")) if run.get("result_json") else None
    return run


def latest_version(conn, task_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE task_id = ? ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return dict(row) if row else None


def next_version_number(conn, task_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM rule_versions WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return int(row["v"]) + 1


def validate_http_url(url: str, label: str) -> str:
    try:
        return validate_target_url(url, label)
    except SafetyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def create_task_row(conn, payload: TaskIn, endpoints: list[dict]) -> int:
    baseline_url = validate_http_url(payload.baseline_url, "修改前")
    candidate_url = validate_http_url(payload.candidate_url, "修改后")
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO tasks (name, requirement, baseline_url, candidate_url, allow_remote, allow_remote_writes,
                              endpoints_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload.name.strip(),
            payload.requirement,
            baseline_url,
            candidate_url,
            int(payload.allow_remote),
            int(payload.allow_remote_writes),
            json.dumps(endpoints, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    return int(cur.lastrowid)


def save_version_row(conn, task_id: int, checks: list[dict], note: str, confirm: bool) -> int:
    version_number = next_version_number(conn, task_id)
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO rule_versions (task_id, version, checks_json, status, note, created_at, confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            version_number,
            json.dumps(checks, ensure_ascii=False),
            "confirmed" if confirm else "draft",
            note,
            ts,
            ts if confirm else None,
        ),
    )
    return int(cur.lastrowid)
