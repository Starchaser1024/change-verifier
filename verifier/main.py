"""改动验收器 FastAPI 应用：验收任务的创建、规则版本管理、运行与报告导出。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, demo_data
from .classification import CLASSIFICATION_ORDER, CLASSIFICATION_LABELS
from .db import get_db, init_db, now_iso, rows_to_dicts
from .llm import LLMError, LLMNotConfigured, llm_status, suggest_checks
from .models import (
    EndpointIn,
    OpenApiImportIn,
    RuleVersionIn,
    RunIn,
    SuggestIn,
    TaskIn,
    TaskPatch,
    UNSAFE_METHODS,
    validate_checks,
)
from .openapi_import import extract_endpoints, fetch_spec
from .report import build_json_report, render_markdown
from .runner import cancel_run, execute_run, register_cancel_event

log = logging.getLogger("verifier.main")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="改动验收器", version=__version__)
init_db()


# ------------------------------------------------------------------ 工具函数


def _get_task(conn, task_id: int) -> dict:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    task = dict(row)
    task["endpoints"] = json.loads(task.pop("endpoints_json") or "[]")
    return task


def _get_version(conn, version_id: int) -> dict:
    row = conn.execute("SELECT * FROM rule_versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"规则版本 {version_id} 不存在")
    version = dict(row)
    version["checks"] = json.loads(version.pop("checks_json"))
    return version


def _get_run(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"运行 {run_id} 不存在")
    run = dict(row)
    run["result"] = json.loads(run.pop("result_json")) if run.get("result_json") else None
    return run


def _latest_version(conn, task_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE task_id = ? ORDER BY version DESC LIMIT 1", (task_id,)
    ).fetchone()
    return dict(row) if row else None


def _next_version_number(conn, task_id: int) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM rule_versions WHERE task_id = ?", (task_id,)).fetchone()
    return int(row["v"]) + 1


def _validate_task_url(url: str, label: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail=f"{label}地址不能为空")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail=f"{label}地址必须是合法的 http(s) URL：{url!r}")
    return url.strip().rstrip("/")


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    host = host.lower().strip("[]")
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("127.")


def _check_run_safety(task: dict, checks: list[dict]) -> None:
    """安全限制：默认只允许操作本地环境；远程写入需要显式开启。"""
    targets = [("修改前", task["baseline_url"]), ("修改后", task["candidate_url"])]
    remote_hosts = [(label, url) for label, url in targets if not _is_loopback(urlparse(url).hostname)]
    if remote_hosts and not task.get("allow_remote"):
        detail = "、".join(f"{label}({urlparse(url).hostname})" for label, url in remote_hosts)
        raise HTTPException(
            status_code=400,
            detail=(
                f"安全限制：{detail} 不是本地地址。默认只允许操作本地（127.0.0.1/localhost）环境，"
                "避免向真实业务系统写入测试数据。如确需远程环境，请在任务设置中显式开启「允许远程环境」。"
            ),
        )
    if remote_hosts and task.get("allow_remote"):
        has_unsafe = any(str(s.get("method", "")).upper() in UNSAFE_METHODS for c in checks for s in c.get("steps", []))
        if has_unsafe and not task.get("allow_remote_writes"):
            detail = "、".join(f"{label}({urlparse(url).hostname})" for label, url in remote_hosts)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"安全限制：规则包含写入类请求（{', '.join(UNSAFE_METHODS)}），目标是远程环境 {detail}。"
                    "请显式开启「允许远程写入」后再运行，避免误写真实数据。"
                ),
            )


# ------------------------------------------------------------------ LLM 状态


@app.get("/api/llm/status")
def api_llm_status():
    return llm_status()


# ------------------------------------------------------------------ 演示


@app.get("/api/demo/config")
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


@app.post("/api/demo/create-task")
def api_demo_create_task(dead_candidate: bool = False):
    """一键创建演示任务：任务 + 接口清单 + 已确认的演示规则 v1。"""
    with get_db() as conn:
        task_id = _create_task_row(
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
        version_id = _save_version(conn, task_id, checks, demo_data.demo_rule_note(), confirm=True)
    return {"task_id": task_id, "rule_version_id": version_id, "checks": len(checks)}


# ------------------------------------------------------------------ 任务


def _create_task_row(conn, payload: TaskIn, endpoints: list[dict]) -> int:
    baseline_url = _validate_task_url(payload.baseline_url, "修改前")
    candidate_url = _validate_task_url(payload.candidate_url, "修改后")
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


@app.post("/api/tasks")
def api_create_task(payload: TaskIn):
    with get_db() as conn:
        task_id = _create_task_row(conn, payload, [])
    return {"id": task_id}


@app.get("/api/tasks")
def api_list_tasks():
    with get_db() as conn:
        tasks = rows_to_dicts(conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall())
        for t in tasks:
            t.pop("endpoints_json", None)
            v = conn.execute(
                "SELECT COUNT(*) AS n, MAX(version) AS latest FROM rule_versions WHERE task_id = ?", (t["id"],)
            ).fetchone()
            t["rule_versions"] = v["n"]
            t["latest_version"] = v["latest"]
    return {"tasks": tasks}


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    with get_db() as conn:
        task = _get_task(conn, task_id)
        versions = rows_to_dicts(
            conn.execute(
                "SELECT id, version, status, note, created_at, confirmed_at FROM rule_versions WHERE task_id = ? ORDER BY version DESC",
                (task_id,),
            ).fetchall()
        )
        for v in versions:
            row = conn.execute("SELECT checks_json FROM rule_versions WHERE id = ?", (v["id"],)).fetchone()
            v["check_count"] = len(json.loads(row["checks_json"]))
        runs = rows_to_dicts(
            conn.execute(
                "SELECT id, rule_version_id, status, error, started_at, finished_at FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        )
    return {"task": task, "rule_versions": versions, "runs": runs}


@app.patch("/api/tasks/{task_id}")
def api_update_task(task_id: int, payload: TaskPatch):
    with get_db() as conn:
        task = _get_task(conn, task_id)
        name = payload.name.strip() if payload.name is not None else task["name"]
        requirement = payload.requirement if payload.requirement is not None else task["requirement"]
        baseline_url = (
            _validate_task_url(payload.baseline_url, "修改前") if payload.baseline_url is not None else task["baseline_url"]
        )
        candidate_url = (
            _validate_task_url(payload.candidate_url, "修改后") if payload.candidate_url is not None else task["candidate_url"]
        )
        allow_remote = int(payload.allow_remote) if payload.allow_remote is not None else int(task["allow_remote"])
        allow_remote_writes = (
            int(payload.allow_remote_writes) if payload.allow_remote_writes is not None else int(task["allow_remote_writes"])
        )
        conn.execute(
            """UPDATE tasks SET name=?, requirement=?, baseline_url=?, candidate_url=?,
               allow_remote=?, allow_remote_writes=?, updated_at=? WHERE id=?""",
            (name, requirement, baseline_url, candidate_url, allow_remote, allow_remote_writes, now_iso(), task_id),
        )
    return {"ok": True}


# ------------------------------------------------------------------ 接口清单


@app.post("/api/tasks/{task_id}/endpoints")
def api_add_endpoint(task_id: int, payload: EndpointIn):
    method = payload.method.upper().strip()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        raise HTTPException(status_code=400, detail=f"不支持的 HTTP 方法: {payload.method}")
    if not payload.path.startswith("/"):
        raise HTTPException(status_code=400, detail="路径必须以 / 开头")
    with get_db() as conn:
        task = _get_task(conn, task_id)
        endpoints = task["endpoints"]
        endpoints.append({"method": method, "path": payload.path, "summary": payload.summary})
        conn.execute("UPDATE tasks SET endpoints_json=?, updated_at=? WHERE id=?", (json.dumps(endpoints, ensure_ascii=False), now_iso(), task_id))
    return {"ok": True, "endpoints": endpoints}


@app.post("/api/tasks/{task_id}/endpoints/import")
def api_import_openapi(task_id: int, payload: OpenApiImportIn):
    if payload.spec is None and not payload.spec_url:
        raise HTTPException(status_code=400, detail="请提供 spec_url 或直接粘贴 OpenAPI JSON（spec 字段）")
    try:
        spec = payload.spec if payload.spec is not None else fetch_spec(payload.spec_url)
        endpoints = extract_endpoints(spec)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"导入 OpenAPI 失败：{type(e).__name__}: {e}") from e
    if not endpoints:
        raise HTTPException(status_code=400, detail="未从 OpenAPI 文件中解析到任何接口")
    with get_db() as conn:
        _get_task(conn, task_id)
        conn.execute(
            "UPDATE tasks SET endpoints_json=?, updated_at=? WHERE id=?",
            (json.dumps(endpoints, ensure_ascii=False), now_iso(), task_id),
        )
    return {"ok": True, "endpoints": endpoints}


# ------------------------------------------------------------------ 规则版本


def _save_version(conn, task_id: int, checks: list[dict], note: str, confirm: bool) -> int:
    version_number = _next_version_number(conn, task_id)
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


@app.get("/api/tasks/{task_id}/rule-versions")
def api_list_versions(task_id: int):
    with get_db() as conn:
        _get_task(conn, task_id)
        versions = rows_to_dicts(
            conn.execute(
                "SELECT id, version, status, note, created_at, confirmed_at FROM rule_versions WHERE task_id = ? ORDER BY version DESC",
                (task_id,),
            ).fetchall()
        )
        for v in versions:
            row = conn.execute("SELECT checks_json FROM rule_versions WHERE id = ?", (v["id"],)).fetchone()
            v["check_count"] = len(json.loads(row["checks_json"]))
    return {"rule_versions": versions}


@app.post("/api/tasks/{task_id}/rule-versions")
def api_create_version(task_id: int, payload: RuleVersionIn):
    errors = validate_checks(payload.checks)
    if errors:
        raise HTTPException(status_code=400, detail={"message": "规则校验失败", "errors": errors})
    with get_db() as conn:
        _get_task(conn, task_id)
        version_id = _save_version(conn, task_id, payload.checks, payload.note, confirm=payload.confirm)
        version = _get_version(conn, version_id)
    return {"id": version_id, "version": version["version"], "status": version["status"]}


@app.get("/api/rule-versions/{version_id}")
def api_get_version(version_id: int):
    with get_db() as conn:
        version = _get_version(conn, version_id)
    return version


@app.post("/api/rule-versions/{version_id}/confirm")
def api_confirm_version(version_id: int):
    with get_db() as conn:
        version = _get_version(conn, version_id)
        if version["status"] == "confirmed":
            return {"ok": True, "already_confirmed": True}
        conn.execute(
            "UPDATE rule_versions SET status='confirmed', confirmed_at=? WHERE id=?",
            (now_iso(), version_id),
        )
    return {"ok": True}


# ------------------------------------------------------------------ LLM 建议


@app.post("/api/tasks/{task_id}/suggest-checks")
def api_suggest_checks(task_id: int, payload: SuggestIn):
    with get_db() as conn:
        task = _get_task(conn, task_id)
        latest = _latest_version(conn, task_id)
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


# ------------------------------------------------------------------ 运行


@app.post("/api/tasks/{task_id}/runs")
def api_start_run(task_id: int, payload: RunIn):
    with get_db() as conn:
        task = _get_task(conn, task_id)
        if payload.rule_version_id is not None:
            version = _get_version(conn, payload.rule_version_id)
            if version["task_id"] != task_id:
                raise HTTPException(status_code=400, detail="规则版本不属于该任务")
        else:
            row = _latest_version(conn, task_id)
            if row is None:
                raise HTTPException(status_code=400, detail="该任务还没有规则版本，请先保存规则")
            version = _get_version(conn, row["id"])
        checks = version["checks"]
        _check_run_safety(task, checks)
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


@app.get("/api/tasks/{task_id}/runs")
def api_list_runs(task_id: int):
    with get_db() as conn:
        _get_task(conn, task_id)
        runs = rows_to_dicts(
            conn.execute(
                "SELECT id, rule_version_id, status, error, started_at, finished_at FROM runs WHERE task_id = ? ORDER BY id DESC",
                (task_id,),
            ).fetchall()
        )
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: int):
    with get_db() as conn:
        run = _get_run(conn, run_id)
        task = _get_task(conn, run["task_id"])
        version = _get_version(conn, run["rule_version_id"])
    result = run["result"]
    if result is not None:
        # 补充未执行的分类计数展示需要
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


@app.post("/api/runs/{run_id}/cancel")
def api_cancel_run(run_id: int):
    with get_db() as conn:
        run = _get_run(conn, run_id)
        if run["status"] != "running":
            raise HTTPException(status_code=400, detail=f"运行已结束（{run['status']}），无法取消")
    cancelled = cancel_run(run_id)
    return {"ok": cancelled}


def _report_parts(run_id: int):
    with get_db() as conn:
        run = _get_run(conn, run_id)
        task = _get_task(conn, run["task_id"])
        version = _get_version(conn, run["rule_version_id"])
    if run["result"] is None:
        raise HTTPException(status_code=409, detail="运行尚未产生结果，无法生成报告")
    return run, task, version


@app.get("/api/runs/{run_id}/report.md")
def api_report_markdown(run_id: int):
    run, task, version = _report_parts(run_id)
    md = render_markdown(task, version, run, run["result"])
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="acceptance-run-{run_id}.md"'},
    )


@app.get("/api/runs/{run_id}/report.json")
def api_report_json(run_id: int):
    run, task, version = _report_parts(run_id)
    report = build_json_report(task, version, run, run["result"])
    return Response(
        content=json.dumps(report, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="acceptance-run-{run_id}.json"'},
    )


# ------------------------------------------------------------------ 静态页面


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
