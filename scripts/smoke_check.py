"""冒烟验证：在三个服务（baseline 8620 / candidate 8621 / 验收器 8622）已启动后，
通过验收器 API 跑一次完整验收并打印结论摘要。

用法：
    python scripts/smoke_check.py
端口与 run_demo.py 保持一致，可用环境变量覆盖。
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

VERIFIER = f"http://127.0.0.1:{os.environ.get('VERIFIER_PORT', '8622')}"
BASELINE = f"http://127.0.0.1:{os.environ.get('DEMO_BASELINE_PORT', '8620')}"
CANDIDATE = f"http://127.0.0.1:{os.environ.get('DEMO_CANDIDATE_PORT', '8621')}"


def main() -> int:
    with httpx.Client(timeout=30) as c:
        for name, url in (("baseline", BASELINE + "/healthz"), ("candidate", CANDIDATE + "/healthz")):
            try:
                resp = c.get(url)
                print(f"[ok] {name} 服务可达：{url} -> {resp.status_code}")
            except httpx.HTTPError as e:
                print(f"[fail] {name} 服务不可达 {url}: {e}")
                print("请先运行 python run_demo.py，或分别启动三个服务。")
                return 1
        c.post(BASELINE + "/api/debug/reset")
        c.post(CANDIDATE + "/api/debug/reset")

        task_id = c.post(f"{VERIFIER}/api/demo/create-task").json()["task_id"]
        print(f"[ok] 已创建演示任务 #{task_id}")
        run_id = c.post(f"{VERIFIER}/api/tasks/{task_id}/runs", json={}).json()["run_id"]
        print(f"[..] 运行 #{run_id} 已启动，等待结束…")
        while True:
            data = c.get(f"{VERIFIER}/api/runs/{run_id}").json()
            if data["run"]["status"] != "running":
                break
            time.sleep(0.3)

        result = data["result"]
        status = data["run"]["status"]
        print(f"[ok] 运行状态：{status}")
        print(f"[ok] 结论：{result['summary']['headline']}")
        for code, n in sorted(result["summary"]["counts"].items()):
            print(f"     {result['classification_labels'][code]}: {n}")
        print()
        print("逐项结果：")
        for cmp in result["checks"]:
            print(f"  [{cmp['classification']['label']}] {cmp['name']}")

        md = c.get(f"{VERIFIER}/api/runs/{run_id}/report.md").text
        for secret in ("alice-password", "bob-password", "admin-password"):
            if secret in md:
                print(f"[fail] 报告泄露了凭据：{secret}")
                return 1
        print()
        print("[ok] Markdown 报告不含演示密码（已脱敏）")
        print(f"[ok] 报告地址：{VERIFIER}/api/runs/{run_id}/report.md")

        expected = {"pass": 2, "regression": 4, "pre_existing": 1, "expected_change": 1, "satisfied": 1, "not_met": 1}
        if status == "completed" and result["summary"]["counts"] == expected:
            print("[ok] 演示缺陷全部按预期被发现（4 个回归 + 1 个新需求未满足 + 1 个原有问题）")
            return 0
        print("[fail] 结果与预期不一致，请检查上方明细")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
