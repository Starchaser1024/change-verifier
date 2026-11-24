"""不可变规则版本管理与可选 LLM 规则建议路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from ..db import get_db, now_iso, rows_to_dicts
from ..llm import LLMError, LLMNotConfigured, llm_status, suggest_checks
from ..models import validate_checks
from ..schemas import RuleVersionIn, SuggestIn
from .deps import get_task_or_404, get_version_or_404, latest_version, save_version_row

router = APIRouter()


@router.get("/api/llm/status")
def api_llm_status():
    return llm_status()


@router.get("/api/tasks/{task_id}/rule-versions")
def api_list_versions(task_id: int):
    with get_db() as conn:
        get_task_or_404(conn, task_id)
        versions = rows_to_dicts(
            conn.execute(
                "SELECT id, version, status, note, created_at, confirmed_at "
                "FROM rule_versions WHERE task_id = ? ORDER BY version DESC",
                (task_id,),
            ).fetchall()
        )
        for v in versions:
            row = conn.execute("SELECT checks_json FROM rule_versions WHERE id = ?", (v["id"],)).fetchone()
            v["check_count"] = len(json.loads(row["checks_json"]))
    return {"rule_versions": versions}


@router.post("/api/tasks/{task_id}/rule-versions")
def api_create_version(task_id: int, payload: RuleVersionIn):
    errors = validate_checks(payload.checks)
    if errors:
        raise HTTPException(status_code=400, detail={"message": "规则校验失败", "errors": errors})
    with get_db() as conn:
        get_task_or_404(conn, task_id)
        version_id = save_version_row(conn, task_id, payload.checks, payload.note, confirm=payload.confirm)
        version = get_version_or_404(conn, version_id)
    return {"id": version_id, "version": version["version"], "status": version["status"]}


@router.get("/api/rule-versions/{version_id}")
def api_get_version(version_id: int):
    with get_db() as conn:
        return get_version_or_404(conn, version_id)


@router.post("/api/rule-versions/{version_id}/confirm")
def api_confirm_version(version_id: int):
    with get_db() as conn:
        version = get_version_or_404(conn, version_id)
        if version["status"] == "confirmed":
            return {"ok": True, "already_confirmed": True}
        conn.execute(
            "UPDATE rule_versions SET status='confirmed', confirmed_at=? WHERE id=?",
            (now_iso(), version_id),
        )
    return {"ok": True}


@router.post("/api/tasks/{task_id}/suggest-checks")
def api_suggest_checks(task_id: int, payload: SuggestIn):
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
        latest = latest_version(conn, task_id)
        existing = json.loads(latest["checks_json"]) if latest else []
    try:
        result = suggest_checks(
            requirement=task["requirement"],
            endpoints=task["endpoints"],
            existing_checks=existing,
        )
    except LLMNotConfigured as e:
        raise HTTPException(status_code=400, detail={"message": str(e), "code": "llm_not_configured"}) from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail={"message": str(e), "code": "llm_error"}) from e
    return {**result, "hint": "模型建议仅供参考，请检查、编辑并确认后再保存为规则版本"}
