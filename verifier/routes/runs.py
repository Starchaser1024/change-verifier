"""双环境验收运行编排、取消与脱敏报告导出路由。"""

from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException, Response

from ..classification import CLASSIFICATION_LABELS, CLASSIFICATION_ORDER
from ..db import get_db, now_iso, rows_to_dicts
from ..report import build_json_report, render_markdown
from ..runner import cancel_run, execute_run, register_cancel_event
from ..safety import SafetyError, check_run_safety
from ..schemas import RunIn
from .deps import get_run_or_404, get_task_or_404, get_version_or_404, latest_version

router = APIRouter()


@router.post("/api/tasks/{task_id}/runs")
def api_start_run(task_id: int, payload: RunIn):
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
        if payload.rule_version_id is not None:
            version = get_version_or_404(conn, payload.rule_version_id)
            if version["task_id"] != task_id:
                raise HTTPException(status_code=400, detail="规则版本不属于该任务")
        else:
            row = latest_version(conn, task_id)
            if row is None:
                raise HTTPException(status_code=400, detail="该任务还没有规则版本，请先保存规则")
            version = get_version_or_404(conn, row["id"])

        try:
            check_run_safety(
                task["baseline_url"],
                task["candidate_url"],
                version["checks"],
                allow_remote=bool(task.get("allow_remote")),
                allow_remote_writes=bool(task.get("allow_remote_writes")),
            )
        except SafetyError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        cur = conn.execute(
            "INSERT INTO runs (task_id, rule_version_id, status, started_at) VALUES (?, ?, 'running', ?)",
            (task_id, version["id"], now_iso()),
        )
        run_id = int(cur.lastrowid)

    register_cancel_event(run_id)
    threading.Thread(target=execute_run, args=(run_id,), name=f"run-{run_id}", daemon=True).start()
    warning = None
    if version["status"] != "confirmed":
        warning = f"规则版本 v{version['version']} 尚未确认（草稿），本次结果仅供参考；请编辑后确认版本。"
    return {"run_id": run_id, "rule_version_id": version["id"], "version": version["version"], "warning": warning}


@router.get("/api/tasks/{task_id}/runs")
def api_list_runs(task_id: int):
    with get_db() as conn:
        get_task_or_404(conn, task_id)
        runs = rows_to_dicts(
            conn.execute(
                "SELECT id, rule_version_id, status, error, started_at, finished_at "
                "FROM runs WHERE task_id = ? ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        )
    return {"runs": runs}


@router.get("/api/runs/{run_id}")
def api_get_run(run_id: int):
    with get_db() as conn:
        run = get_run_or_404(conn, run_id)
        task = get_task_or_404(conn, run["task_id"])
        version = get_version_or_404(conn, run["rule_version_id"])
    result = run["result"]
    if result is not None:
        result["classification_order"] = CLASSIFICATION_ORDER
        result["classification_labels"] = CLASSIFICATION_LABELS
    return {
        "run": {
            "id": run["id"],
            "task_id": run["task_id"],
            "rule_version_id": run["rule_version_id"],
            "status": run["status"],
            "error": run["error"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
        },
        "task": {k: task[k] for k in ("id", "name", "baseline_url", "candidate_url")},
        "rule_version": {
            "id": version["id"],
            "version": version["version"],
            "status": version["status"],
            "note": version["note"],
        },
        "result": result,
    }


@router.post("/api/runs/{run_id}/cancel")
def api_cancel_run(run_id: int):
    with get_db() as conn:
        run = get_run_or_404(conn, run_id)
        if run["status"] != "running":
            raise HTTPException(status_code=400, detail=f"运行已结束（{run['status']}），无法取消")
    cancelled = cancel_run(run_id)
    return {"ok": cancelled}


def _report_parts(run_id: int):
    with get_db() as conn:
        run = get_run_or_404(conn, run_id)
        task = get_task_or_404(conn, run["task_id"])
        version = get_version_or_404(conn, run["rule_version_id"])
    if run["result"] is None:
        raise HTTPException(status_code=409, detail="运行尚未产生结果，无法生成报告")
    return run, task, version


@router.get("/api/runs/{run_id}/report.md")
def api_report_markdown(run_id: int):
    run, task, version = _report_parts(run_id)
    md = render_markdown(task, version, run, run["result"])
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="acceptance-run-{run_id}.md"'},
    )


@router.get("/api/runs/{run_id}/report.json")
def api_report_json(run_id: int):
    run, task, version = _report_parts(run_id)
    report = build_json_report(task, version, run, run["result"])
    return Response(
        content=json.dumps(report, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="acceptance-run-{run_id}.json"'},
    )
