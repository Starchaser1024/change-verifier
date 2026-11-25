"""变异评估：给示例服务注入「已知缺陷组合」，用演示规则验收，并逐条对照预期分类。

目的：量化「验收引擎 + 这套已确认规则」的机械性误报/漏报：
同一条规则，在缺陷开启时是否被判为「本次引入的回归」，关闭时是否被判为「通过」。

重要限制（不要过度解读）：缺陷是人为注入的，预期结果由程序推导，因此这里测的是
「引擎分类是否正确、这套演示规则是否真的有效」，不能替代针对具体业务接口的独立验收规则。

用法：
    python scripts/mutation_eval.py
    python scripts/mutation_eval.py --md docs/mutation_eval_results.md
"""

from __future__ import annotations

import argparse
import atexit
import datetime
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 独立的临时数据库，避免污染本地 verifier_data/
_TMP_DB_DIR = tempfile.mkdtemp(prefix="mutation_eval_")
atexit.register(shutil.rmtree, _TMP_DB_DIR, True)
os.environ["VERIFIER_DB"] = str(pathlib.Path(_TMP_DB_DIR) / "verifier.db")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from demo.services import DEFAULT_CANDIDATE_DEFECTS, create_app  # noqa: E402
from verifier import demo_data  # noqa: E402
from verifier.main import app as verifier_app  # noqa: E402

# 场景 = 一组被开启的演示缺陷
SCENARIOS: list[tuple[str, set[str]]] = [
    ("none（全部修好）", set()),
    ("A（跨用户读）", {"A"}),
    ("B（管理员权限）", {"B"}),
    ("C（幂等失效）", {"C"}),
    ("D（未登录可读）", {"D"}),
    ("E（分页未实现）", {"E"}),
    ("ABCD（旧缺陷全开，新需求已实现）", {"A", "B", "C", "D"}),
    ("ABCDE（当前演示默认）", set(DEFAULT_CANDIDATE_DEFECTS)),
    ("AE（混合）", {"A", "E"}),
]

# 规则与缺陷的对应关系（与 verifier/demo_data.py 中的检查一一对应）
DEFECT_CHECKS = {
    "read-other-order-denied": "A",
    "admin-endpoint-forbidden": "B",
    "create-order-idempotent": "C",
    "anonymous-list-denied": "D",
}
NEW_REQ_CHECKS = {"list-page-size": "E"}
FIXED_EXPECTATIONS = {
    "login-success": "pass",
    "read-own-order": "pass",
    "list-legacy-envelope": "expected_change",
    "missing-order-status": "pre_existing",
    "list-new-envelope": "satisfied",
}
COUNT_CODES = ["pass", "regression", "pre_existing", "expected_change", "satisfied", "not_met", "unverifiable"]


def expected_classifications(defects: set[str]) -> dict[str, str]:
    expected = dict(FIXED_EXPECTATIONS)
    for check_id, defect in DEFECT_CHECKS.items():
        expected[check_id] = "regression" if defect in defects else "pass"
    for check_id, defect in NEW_REQ_CHECKS.items():
        expected[check_id] = "not_met" if defect in defects else "satisfied"
    return expected


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_app(app, port: int, log_level: str = "warning"):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level, access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪: {url}")


def run_once(client: httpx.Client, verifier: str, baseline_url: str, candidate_url: str, label: str) -> dict:
    """在给定的两个环境中用演示规则跑一次验收，返回运行结果。"""
    task = client.post(
        f"{verifier}/api/tasks",
        json={
            "name": f"变异评估：{label}",
            "requirement": demo_data.DEMO_TASK["requirement"],
            "baseline_url": baseline_url,
            "candidate_url": candidate_url,
        },
    ).json()
    task_id = task["id"]
    version = client.post(
        f"{verifier}/api/tasks/{task_id}/rule-versions",
        json={"checks": demo_data.demo_checks(), "note": "变异评估（自动）", "confirm": True},
    ).json()
    run_id = client.post(f"{verifier}/api/tasks/{task_id}/runs", json={"rule_version_id": version["id"]}).json()["run_id"]
    while True:
        data = client.get(f"{verifier}/api/runs/{run_id}").json()
        if data["run"]["status"] != "running":
            break
        time.sleep(0.15)
    if data["run"]["status"] != "completed":
        raise RuntimeError(f"运行未正常完成：{data['run']['status']} {data['run']['error']}")
    return data["result"]


def evaluate(defects: set[str], result: dict) -> dict:
    expected = expected_classifications(defects)
    actual = {c["id"]: c["classification"]["code"] for c in result["checks"]}
    mismatches = []
    for check_id, want in expected.items():
        got = actual.get(check_id)
        if got != want:
            mismatches.append((check_id, want, got))
    fp = [m for m in mismatches if m[2] == "regression" and m[1] != "regression"]
    fn = [m for m in mismatches if m[1] == "regression" and m[2] != "regression"]
    return {
        "counts": result["summary"]["counts"],
        "mismatches": mismatches,
        "false_positives": fp,   # 回归误报：判成回归但缺陷其实没开
        "false_negatives": fn,   # 回归漏报：缺陷开着却没判成回归
    }


def fmt_counts(counts: dict) -> str:
    return "  ".join(str(counts.get(code, 0)).rjust(3) for code in COUNT_CODES)


def main() -> int:
    parser = argparse.ArgumentParser(description="对示例服务做缺陷变异评估")
    parser.add_argument("--md", default=None, help="把结果写入 Markdown 文件（例如 docs/mutation_eval_results.md）")
    args = parser.parse_args()

    baseline_port = free_port()
    verifier_port = free_port()
    baseline_url = f"http://127.0.0.1:{baseline_port}"
    verifier = f"http://127.0.0.1:{verifier_port}"

    b_server, b_thread = start_app(create_app("baseline"), baseline_port, log_level="critical")
    v_server, v_thread = start_app(verifier_app, verifier_port, log_level="critical")
    wait_ready(baseline_url + "/healthz")
    wait_ready(verifier + "/api/llm/status")

    header = "场景".ljust(30) + "".join(code.rjust(5) for code in COUNT_CODES) + "   偏差"
    print(header)
    print("-" * len(header))

    rows = []
    total_fp = total_fn = 0
    try:
        with httpx.Client(timeout=60) as client:
            for label, defects in SCENARIOS:
                candidate_port = free_port()
                candidate_url = f"http://127.0.0.1:{candidate_port}"
                c_server, c_thread = start_app(create_app("candidate", defects=defects), candidate_port, log_level="critical")
                wait_ready(candidate_url + "/healthz")
                try:
                    client.post(baseline_url + "/api/debug/reset")
                    client.post(candidate_url + "/api/debug/reset")
                    result = run_once(client, verifier, baseline_url, candidate_url, label)
                finally:
                    c_server.should_exit = True
                    c_thread.join(5)

                outcome = evaluate(defects, result)
                total_fp += len(outcome["false_positives"])
                total_fn += len(outcome["false_negatives"])
                rows.append({"label": label, "defects": sorted(defects), "outcome": outcome})
                print(
                    label.ljust(30)
                    + fmt_counts(outcome["counts"]).ljust(len(COUNT_CODES) * 5)
                    + f"   {len(outcome['mismatches'])}"
                )
                for check_id, want, got in outcome["mismatches"]:
                    print(f"    ✗ {check_id}: 预期 {want}，实际 {got}")
    finally:
        v_server.should_exit = True
        b_server.should_exit = True
        b_thread.join(5)
        v_thread.join(5)

    total_mismatch = sum(len(r["outcome"]["mismatches"]) for r in rows)
    print()
    print(f"结论：{len(rows)} 个场景，共 {total_mismatch} 处分类偏差；回归误报 {total_fp}，回归漏报 {total_fn}")
    print("（误报 = 缺陷没开却判成回归；漏报 = 缺陷开着却未判成回归）")

    if args.md:
        write_markdown(pathlib.Path(args.md), rows, total_fp, total_fn)
        print(f"[ok] 结果已写入 {args.md}")

    return 0 if total_mismatch == 0 else 1


def write_markdown(path: pathlib.Path, rows: list[dict], total_fp: int, total_fn: int) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 变异评估结果（示例服务 · 已知缺陷组合）",
        "",
        f"- 运行时间：{now}",
        "- 运行命令：`python scripts/mutation_eval.py --md docs/mutation_eval_results.md`",
        "- 环境：本地进程内启动 baseline / candidate / 验收器（独立临时 SQLite），规则为演示规则（10 条检查，已确认）",
        "",
        "## 这是什么 / 不是什么",
        "",
        "把「AI 改动后可能引入的缺陷」逐个开关，检查验收器是否恰好把它们判为"
        "「本次引入的回归」，而缺陷关闭时判为「通过」。",
        "",
        "**边界说明**：这里的缺陷是人为注入的、预期结果由程序推导。它证明的是"
        "「引擎 8 类判定状态机确定性正确 + 演示规则集对这 5 类缺陷无误报/漏报」，不能替代业务服务自身的验收规则集。",
        "",
        "## 场景 × 结果",
        "",
        "| 场景 | 开启的缺陷 | " + " | ".join(f"`{c}`" for c in COUNT_CODES) + " | 分类偏差 |",
        "| --- | --- | " + " | ".join("---" for _ in COUNT_CODES) + " | --- |",
    ]
    for r in rows:
        counts = r["outcome"]["counts"]
        cells = " | ".join(str(counts.get(code, 0)) for code in COUNT_CODES)
        lines.append(
            f"| {r['label']} | {', '.join(r['defects']) or '（无）'} | {cells} | {len(r['outcome']['mismatches'])} |"
        )
    total = sum(len(r["outcome"]["mismatches"]) for r in rows)
    lines += [
        "",
        f"**合计：{len(rows)} 个场景，{total} 处分类偏差；回归误报 {total_fp}，回归漏报 {total_fn}。**",
        "",
        "指标含义：",
        "",
        "- **回归误报**：缺陷未开启（该行为其实没坏），却被判为「本次引入的回归」——最危险的错误。",
        "- **回归漏报**：缺陷已开启（该行为确实坏了），却没有被判为「本次引入的回归」。",
        "- `unverifiable`（无法判断）出现即说明环境抖动，本评估中应为 0。",
        "",
        "## 偏差明细",
        "",
    ]
    details = [
        f"- {r['label']}：{check_id} 预期 {want}，实际 {got}"
        for r in rows
        for check_id, want, got in r["outcome"]["mismatches"]
    ]
    lines += details or ["（无偏差）"]
    lines += [
        "",
        "## 已知局限",
        "",
        "- 缺陷仅包含上述 5 种并逐一对应到具体检查；未编写对应检查项的业务逻辑变化不会被自动覆盖。",
        "- 全部在本地示例服务上运行，不涉及网络抖动、鉴权复杂度、真实数据依赖。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
