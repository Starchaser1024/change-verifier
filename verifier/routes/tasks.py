"""任务、演示任务与接口清单（含 OpenAPI 导入）路由。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from .. import demo_data
from ..db import get_db, now_iso, rows_to_dicts
from ..openapi_import import extract_endpoints, fetch_spec
from ..safety import HTTP_METHODS, SafetyError
from ..schemas import EndpointIn, OpenApiImportIn, TaskIn, TaskPatch
from .deps import create_task_row, get_task_or_404, save_version_row, validate_http_url

router = APIRouter()


@router.get("/api/demo/config")
def api_demo_config():
    return {
        "task": demo_data.DEMO_TASK,
        "endpoints": demo_data.DEMO_ENDPOINTS,
        "checks": demo_data.demo_checks(),
        "dead_candidate_url": "http://127.0.0.1:8623",
        "notes": [
            "先启动两个示例服务：python demo/run_baseline.py（默认 8620）与 python demo/run_candidate.py（默认 8621）",
            "candidate 服务包含 5 个标注缺陷（GET /api/meta/defects 可查看）",
        ],
    }


@router.post("/api/demo/create-task")
def api_demo_create_task(dead_candidate: bool = False):
    """一键创建演示任务：任务 + 接口清单 + 已确认的演示规则 v1。"""
    with get_db() as conn:
        task_id = create_task_row(
            conn,
            TaskIn(
                name=demo_data.DEMO_TASK["name"] + ("（不可达环境演示）" if dead_candidate else ""),
                requirement=demo_data.DEMO_TASK["requirement"],
                baseline_url=demo_data.DEMO_TASK["baseline_url"],
                candidate_url=("http://127.0.0.1:8623" if dead_candidate else demo_data.DEMO_TASK["candidate_url"]),
            ),
            demo_data.DEMO_ENDPOINTS,
        )
        checks = demo_data.demo_checks()
        version_id = save_version_row(conn, task_id, checks, demo_data.demo_rule_note(), confirm=True)
    return {"task_id": task_id, "rule_version_id": version_id, "checks": len(checks)}


@router.post("/api/tasks")
def api_create_task(payload: TaskIn):
    with get_db() as conn:
        task_id = create_task_row(conn, payload, [])
    return {"id": task_id}


@router.get("/api/tasks")
def api_list_tasks():
    with get_db() as conn:
        tasks = rows_to_dicts(conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall())
        for t in tasks:
            t.pop("endpoints_json", None)
            v = conn.execute(
                "SELECT COUNT(*) AS n, MAX(version) AS latest FROM rule_versions WHERE task_id = ?",
                (t["id"],),
            ).fetchone()
            t["rule_versions"] = v["n"]
            t["latest_version"] = v["latest"]
    return {"tasks": tasks}


@router.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
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
        runs = rows_to_dicts(
            conn.execute(
                "SELECT id, rule_version_id, status, error, started_at, finished_at "
                "FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        )
    return {"task": task, "rule_versions": versions, "runs": runs}


@router.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: TaskPatch):
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
        name = payload.name.strip() if payload.name is not None else task["name"]
        requirement = payload.requirement if payload.requirement is not None else task["requirement"]
        baseline_url = (
            validate_http_url(payload.baseline_url, "修改前")
            if payload.baseline_url is not None
            else task["baseline_url"]
        )
        candidate_url = (
            validate_http_url(payload.candidate_url, "修改后")
            if payload.candidate_url is not None
            else task["candidate_url"]
        )
        allow_remote = int(payload.allow_remote) if payload.allow_remote is not None else int(task["allow_remote"])
        allow_remote_writes = (
            int(payload.allow_remote_writes)
            if payload.allow_remote_writes is not None
            else int(task["allow_remote_writes"])
        )
        conn.execute(
            """UPDATE tasks SET name=?, requirement=?, baseline_url=?, candidate_url=?,
               allow_remote=?, allow_remote_writes=?, updated_at=? WHERE id=?""",
            (name, requirement, baseline_url, candidate_url, allow_remote, allow_remote_writes, now_iso(), task_id),
        )
    return {"ok": True}


@router.post("/api/tasks/{task_id}/endpoints")
def api_add_endpoint(task_id: int, payload: EndpointIn):
    method = payload.method.upper().strip()
    if method not in HTTP_METHODS:
        raise HTTPException(status_code=400, detail=f"不支持的 HTTP 方法: {payload.method}")
    if not payload.path.startswith("/") or payload.path.startswith("//"):
        raise HTTPException(status_code=400, detail="路径必须以单个 / 开头")
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
        endpoints = task["endpoints"]
        endpoints.append({"method": method, "path": payload.path, "summary": payload.summary})
        conn.execute(
            "UPDATE tasks SET endpoints_json=?, updated_at=? WHERE id=?",
            (json.dumps(endpoints, ensure_ascii=False), now_iso(), task_id),
        )
    return {"ok": True, "endpoints": endpoints}


@router.post("/api/tasks/{task_id}/endpoints/import")
def api_import_openapi(task_id: int, payload: OpenApiImportIn):
    with get_db() as conn:
        task = get_task_or_404(conn, task_id)
    if payload.spec is None and not payload.spec_url:
        raise HTTPException(status_code=400, detail="请提供 spec_url 或直接粘贴 OpenAPI JSON（spec 字段）")
    allow_remote = bool(task.get("allow_remote"))
    try:
        spec = payload.spec if payload.spec is not None else fetch_spec(payload.spec_url, allow_remote=allow_remote)
        endpoints = extract_endpoints(spec)
    except (SafetyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"导入 OpenAPI 失败：{type(e).__name__}: {e}") from e
    if not endpoints:
        raise HTTPException(status_code=400, detail="未从 OpenAPI 文件中解析到任何接口")
    with get_db() as conn:
        get_task_or_404(conn, task_id)
        conn.execute(
            "UPDATE tasks SET endpoints_json=?, updated_at=? WHERE id=?",
            (json.dumps(endpoints, ensure_ascii=False), now_iso(), task_id),
        )
    return {"ok": True, "endpoints": endpoints}
