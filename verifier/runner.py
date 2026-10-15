"""运行编排：在 baseline / candidate 两个环境执行同一套检查，并分类结果。"""

from __future__ import annotations

import json
import logging
import secrets
import threading

from .classification import build_summary, compare_check
from .db import get_db, now_iso
from .executor import UNVERIFIABLE, execute_check

log = logging.getLogger("verifier.runner")

UNEXECUTED_REASON = "检查未执行"
RUNNING_REASON = "检查尚未执行（运行进行中）"
CANCELLED_REASON = "检查未执行（运行已取消）"

_cancel_events: dict[int, threading.Event] = {}
_events_lock = threading.Lock()


def register_cancel_event(run_id: int) -> threading.Event:
    ev = threading.Event()
    with _events_lock:
        _cancel_events[run_id] = ev
    return ev


def get_cancel_event(run_id: int) -> threading.Event | None:
    with _events_lock:
        return _cancel_events.get(run_id)


def cancel_run(run_id: int) -> bool:
    ev = get_cancel_event(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def _unverifiable(env: str, reason: str) -> dict:
    return {"env": env, "status": UNVERIFIABLE, "reason": reason, "steps": []}


def execute_run(run_id: int) -> None:
    """在工作线程中执行一次完整验收。异常会被记录为 run.status='error'。"""
    cancel_event = get_cancel_event(run_id)
    try:
        with get_db() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                return
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
            version = conn.execute("SELECT * FROM rule_versions WHERE id = ?", (run["rule_version_id"],)).fetchone()

        checks = json.loads(version["checks_json"])
        run_nonce = secrets.token_hex(4)
        env_results: list[tuple[dict, dict, dict]] = []

        def save_partial(status: str) -> None:
            comparisons = _build_comparisons(
                checks, env_results, executed_count=len(env_results), unexecuted_reason=RUNNING_REASON
            )
            summary = build_summary(
                comparisons, total_checks=len(checks), run_status=status, executed_checks=len(env_results)
            )
            payload = {
                "run_nonce": run_nonce,
                "partial": status == "running",
                "summary": summary,
                "checks": comparisons,
            }
            with get_db() as conn:
                conn.execute(
                    "UPDATE runs SET result_json = ?, status = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), status, run_id),
                )

        for index, check in enumerate(checks):
            if cancel_event is not None and cancel_event.is_set():
                break
            baseline = execute_check(check, "baseline", task["baseline_url"], run_nonce, index, cancel_event=cancel_event)
            candidate = execute_check(check, "candidate", task["candidate_url"], run_nonce, index, cancel_event=cancel_event)
            env_results.append((check, baseline, candidate))
            save_partial("running")  # 逐项更新，网页可实时查看进度

        cancelled = cancel_event is not None and cancel_event.is_set()
        final_status = "cancelled" if cancelled else "completed"
        comparisons = _build_comparisons(
            checks,
            env_results,
            executed_count=len(env_results),
            unexecuted_reason=CANCELLED_REASON if cancelled else UNEXECUTED_REASON,
        )
        summary = build_summary(
            comparisons,
            total_checks=len(checks),
            run_status=final_status,
            cancelled=cancelled,
            executed_checks=len(env_results),
        )
        payload = {
            "run_nonce": run_nonce,
            "partial": False,
            "summary": summary,
            "checks": comparisons,
        }
        with get_db() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, result_json = ?, finished_at = ? WHERE id = ?",
                (final_status, json.dumps(payload, ensure_ascii=False), now_iso(), run_id),
            )
    except Exception as e:  # noqa: BLE001 - 任何异常都要落到 run 记录里，便于排查
        log.exception("run %s failed", run_id)
        try:
            with get_db() as conn:
                conn.execute(
                    "UPDATE runs SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                    (f"{type(e).__name__}: {e}", now_iso(), run_id),
                )
        except Exception:  # pragma: no cover
            log.exception("failed to persist run error")


def _build_comparisons(
    checks: list[dict],
    env_results: list[tuple[dict, dict, dict]],
    executed_count: int,
    unexecuted_reason: str = UNEXECUTED_REASON,
) -> list[dict]:
    """把已执行结果分类；未执行的检查标记为无法判断。"""
    by_id: dict[str, tuple[dict, dict]] = {}
    comparisons: list[dict] = []
    for check, baseline, candidate in env_results:
        by_id[check["id"]] = (baseline, candidate)
    # 先把未执行的写入 by_id（unverifiable），保证引用解析可以看到它们
    for check in checks[executed_count:]:
        by_id.setdefault(
            check["id"],
            (_unverifiable("baseline", unexecuted_reason), _unverifiable("candidate", unexecuted_reason)),
        )
    for check, baseline, candidate in env_results:
        comparisons.append(compare_check(check, baseline, candidate, by_id))
    for check in checks[executed_count:]:
        b, c = by_id[check["id"]]
        comparisons.append(compare_check(check, b, c, by_id))
    return comparisons
