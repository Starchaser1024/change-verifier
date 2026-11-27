"""未执行的检查（如运行被取消）必须归类为「无法判断」，且双环境并发编排保持确定性顺序与共享 nonce。"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit

from verifier.classification import CLASSIFICATION_LABELS
from verifier.runner import _build_comparisons, run_checks

CHECKS = [
    {"id": "a", "name": "A", "category": "existing_behavior", "steps": [{"method": "GET", "path": "/", "assert": [{"type": "status", "op": "equals", "value": 200}]}]},
    {"id": "b", "name": "B", "category": "existing_behavior", "steps": [{"method": "GET", "path": "/", "assert": [{"type": "status", "op": "equals", "value": 200}]}]},
    {"id": "n", "name": "N", "category": "new_requirement", "steps": [{"method": "GET", "path": "/", "assert": [{"type": "status", "op": "equals", "value": 200}]}]},
]


def passed(env):
    return {"env": env, "status": "passed", "reason": "ok", "steps": []}


def test_unexecuted_checks_are_unverifiable():
    comparisons = _build_comparisons(CHECKS, [(CHECKS[0], passed("baseline"), passed("candidate"))], executed_count=1)
    assert [c["id"] for c in comparisons] == ["a", "b", "n"]
    codes = {c["id"]: c["classification"]["code"] for c in comparisons}
    assert codes["a"] == "pass"
    assert codes["b"] == "unverifiable"
    assert codes["n"] == "unverifiable"
    reason = comparisons[1]["classification"]["reason"]
    assert "未执行" in reason or "无法判断" in reason
    assert CLASSIFICATION_LABELS["unverifiable"] == "无法判断"


def test_summary_executed_checks_excludes_unexecuted():
    """取消运行后，摘要里的「已执行」只计真正执行过的检查（未执行的算「无法判断」）。"""
    from verifier.classification import build_summary

    comparisons = _build_comparisons(CHECKS, [(CHECKS[0], passed("baseline"), passed("candidate"))], executed_count=1)
    summary = build_summary(
        comparisons, total_checks=3, run_status="cancelled", cancelled=True, executed_checks=1
    )
    assert summary["total_checks"] == 3
    assert summary["executed_checks"] == 1, "未执行的检查不能计入「已执行」"
    assert summary["counts"]["unverifiable"] == 2
    assert any("取消" in n for n in summary["notes"])
    # 不传 executed_checks 时保持兼容：回退为 len(comparisons)
    fallback = build_summary(comparisons, total_checks=3, run_status="cancelled", cancelled=True)
    assert fallback["executed_checks"] == len(comparisons)


def test_expected_change_ref_to_unexecuted_is_not_confirmed():
    """引用的新需求检查没执行（无法判断）时，旧行为失败仍算回归，不能自动洗白。"""
    checks = [
        {
            "id": "old",
            "name": "旧行为",
            "category": "existing_behavior",
            "expected_change_ref": "n",
            "steps": [{"method": "GET", "path": "/", "assert": [{"type": "status", "op": "equals", "value": 200}]}],
        },
        CHECKS[2],
    ]
    comparisons = _build_comparisons(checks, [(checks[0], passed("baseline"), {**passed("candidate"), "status": "failed"})], executed_count=1)
    codes = {c["id"]: c["classification"]["code"] for c in comparisons}
    assert codes["old"] == "regression"
    assert codes["n"] == "unverifiable"


@contextmanager
def _barrier_pair_servers(barrier: threading.Barrier, seen: dict[str, list[str]]):
    lock = threading.Lock()

    def make_handler(role: str):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                qs = parse_qs(urlsplit(self.path).query)
                nonce = (qs.get("nonce") or [""])[0]
                try:
                    barrier.wait(timeout=3.0)
                    code = 200
                except threading.BrokenBarrierError:
                    code = 504
                with lock:
                    seen[role].append(nonce)
                raw = json.dumps({"role": role, "nonce": nonce}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler

    s_base = HTTPServer(("127.0.0.1", 0), make_handler("baseline"))
    s_cand = HTTPServer(("127.0.0.1", 0), make_handler("candidate"))
    t_base = threading.Thread(target=s_base.serve_forever, daemon=True)
    t_cand = threading.Thread(target=s_cand.serve_forever, daemon=True)
    t_base.start()
    t_cand.start()
    try:
        yield f"http://127.0.0.1:{s_base.server_address[1]}", f"http://127.0.0.1:{s_cand.server_address[1]}"
    finally:
        s_base.shutdown()
        s_cand.shutdown()
        s_base.server_close()
        s_cand.server_close()
        t_base.join(timeout=5)
        t_cand.join(timeout=5)


def test_run_checks_executes_baseline_and_candidate_concurrently_with_identical_nonce():
    barrier = threading.Barrier(2)
    seen: dict[str, list[str]] = {"baseline": [], "candidate": []}
    progress_counts: list[int] = []
    checks = [
        {
            "id": "c-0",
            "name": "并发检查 0",
            "category": "existing_behavior",
            "steps": [
                {
                    "method": "GET",
                    "path": "/sync",
                    "params": {"nonce": "{{nonce}}"},
                    "assert": [{"type": "status", "op": "equals", "value": 200}],
                }
            ],
        },
        {
            "id": "c-1",
            "name": "并发检查 1",
            "category": "new_requirement",
            "steps": [
                {
                    "method": "GET",
                    "path": "/sync",
                    "params": {"nonce": "{{nonce}}"},
                    "assert": [{"type": "status", "op": "equals", "value": 200}],
                }
            ],
        },
    ]

    with _barrier_pair_servers(barrier, seen) as (base_url, cand_url):
        outcome = run_checks(
            checks,
            base_url,
            cand_url,
            on_progress=lambda env_results, _nonce: progress_counts.append(len(env_results)),
        )

    assert outcome["status"] == "completed"
    payload = outcome["payload"]
    run_nonce = payload["run_nonce"]
    assert [c["id"] for c in payload["checks"]] == ["c-0", "c-1"]
    assert payload["summary"]["counts"] == {"pass": 1, "satisfied": 1}
    assert progress_counts == [1, 2]
    assert seen["baseline"] == [f"{run_nonce}-0", f"{run_nonce}-1"]
    assert seen["candidate"] == [f"{run_nonce}-0", f"{run_nonce}-1"]


def test_run_checks_stops_on_cancel_event_after_in_flight_check():
    barrier = threading.Barrier(2)
    seen: dict[str, list[str]] = {"baseline": [], "candidate": []}
    cancel_ev = threading.Event()
    checks = [
        {
            "id": "first",
            "name": "第一项",
            "category": "existing_behavior",
            "steps": [{"method": "GET", "path": "/sync", "assert": [{"type": "status", "op": "equals", "value": 200}]}],
        },
        {
            "id": "second",
            "name": "第二项（应被取消跳过）",
            "category": "existing_behavior",
            "steps": [{"method": "GET", "path": "/sync", "assert": [{"type": "status", "op": "equals", "value": 200}]}],
        },
    ]

    with _barrier_pair_servers(barrier, seen) as (base_url, cand_url):
        outcome = run_checks(
            checks,
            base_url,
            cand_url,
            cancel_event=cancel_ev,
            on_progress=lambda _res, _nonce: cancel_ev.set(),
        )

    assert outcome["status"] == "cancelled"
    summary = outcome["payload"]["summary"]
    assert summary["executed_checks"] == 1
    assert summary["counts"] == {"pass": 1, "unverifiable": 1}
