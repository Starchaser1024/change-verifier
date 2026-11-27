"""报告导出：Markdown 与 JSON。

报告基于已存储（且已脱敏）的运行结果生成，不会引入新的敏感信息。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .classification import CLASSIFICATION_ORDER, CLASSIFICATION_LABELS

ENV_LABELS = {"baseline": "修改前（baseline）", "candidate": "修改后（candidate）"}
STATUS_LABELS = {"passed": "通过", "failed": "失败", "unverifiable": "无法判断"}


def _fmt(value: Any, limit: int = 1500) -> str:
    if value is None:
        return "(空)"
    if isinstance(value, str):
        s = value
    else:
        s = json.dumps(value, ensure_ascii=False, indent=2)
    if len(s) > limit:
        s = s[:limit] + "\n…[已截断]"
    return s


def _render_step_md(step: dict) -> str:
    lines: list[str] = []
    req = step.get("request") or {}
    method = req.get("method", "?")
    path = req.get("path", "?")
    lines.append(f"  - 请求：`{method} {path}`")
    if req.get("params"):
        lines.append(f"    - 查询参数：`{json.dumps(req['params'], ensure_ascii=False)}`")
    if req.get("headers"):
        lines.append(f"    - 请求头：`{json.dumps(req['headers'], ensure_ascii=False)}`")
    if req.get("json") is not None:
        lines.append("    - 请求体：")
        lines.append("      ```json")
        for ln in _fmt(req["json"]).splitlines():
            lines.append(f"      {ln}")
        lines.append("      ```")
    if req.get("form") is not None:
        lines.append("    - 表单请求体（`application/x-www-form-urlencoded`）：")
        lines.append("      ```json")
        for ln in _fmt(req["form"]).splitlines():
            lines.append(f"      {ln}")
        lines.append("      ```")
    extracted = step.get("extracted") or {}
    if extracted:
        lines.append(f"    - 提取变量：`{json.dumps(extracted, ensure_ascii=False)}`")

    if step.get("error"):
        err = step["error"]
        lines.append(f"    - ⚠️ {err.get('label', err.get('kind'))}：{err.get('message')}")
    resp = step.get("response")
    if resp is not None:
        status = resp.get("status")
        elapsed = resp.get("elapsed_ms")
        lines.append(f"    - 响应：`{status}`（{elapsed} ms）")
        body = resp.get("json") if resp.get("is_json") else resp.get("text")
        if body is not None:
            lines.append("      ```json" if resp.get("is_json") else "      ```text")
            for ln in _fmt(body).splitlines():
                lines.append(f"      {ln}")
            lines.append("      ```")
    for a in step.get("assertions") or []:
        mark = "✓" if a["ok"] else "✗"
        lines.append(f"    - {mark} 断言：{a['desc']}（期望 {a['expected']}，实际 {a['actual']}）")
    return "\n".join(lines)


def render_markdown(task: dict, version: dict, run: dict, result: dict) -> str:
    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    lines: list[str] = []
    lines.append(f"# 验收报告：{task['name']}")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"- 任务 ID：{task['id']}；运行 ID：{run['id']}；规则版本：v{version['version']}"
                 f"（{'已确认' if version['status'] == 'confirmed' else '草稿，未确认'}）")
    lines.append(f"- 规则说明：{version.get('note') or '（无）'}")
    lines.append(f"- 运行状态：{run['status']}；开始：{run.get('started_at')}；结束：{run.get('finished_at') or '（未结束）'}")
    lines.append(f"- 修改前环境：`{task['baseline_url']}`")
    lines.append(f"- 修改后环境：`{task['candidate_url']}`")
    lines.append("")
    lines.append("## 需求")
    lines.append("")
    lines.append(task.get("requirement") or "（未填写）")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"**{summary.get('headline', '（无结论）')}**")
    for note in summary.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## 结果汇总")
    lines.append("")
    lines.append("| 分类 | 数量 |")
    lines.append("| --- | --- |")
    for code in CLASSIFICATION_ORDER:
        if counts.get(code):
            lines.append(f"| {CLASSIFICATION_LABELS[code]} | {counts[code]} |")
    lines.append(f"| **合计** | **{summary.get('total_checks', 0)} 项检查（已执行 {summary.get('executed_checks', 0)} 项）** |")
    lines.append("")
    lines.append("## 检查明细")
    lines.append("")
    checks = result.get("checks", [])
    for i, cmp in enumerate(checks, 1):
        cl = cmp["classification"]
        lines.append(f"### {i}. [{cl['label']}] {cmp['name']}")
        lines.append("")
        lines.append(f"- 检查 id：`{cmp['id']}`；类型：{'保持原有行为' if cmp['category'] == 'existing_behavior' else '新需求验收'}")
        if cmp.get("expectation"):
            lines.append(f"- 期望：{cmp['expectation']}")
        if cmp.get("expected_change_ref"):
            lines.append(f"- 行为变化引用：`{cmp['expected_change_ref']}`（修改未回归的依据）")
        lines.append(f"- 判定：**{cl['label']}** —— {cl['reason']}")
        lines.append(f"- 前后对比：{cmp['diff_summary']}")
        lines.append("")
        for env_key in ("baseline", "candidate"):
            env = cmp[env_key]
            lines.append(f"**{ENV_LABELS[env_key]}**：{STATUS_LABELS[env['status']]} —— {env['reason']}")
            for step in env.get("steps", []):
                lines.append(f"- 步骤「{step.get('name')}」")
                lines.append(_render_step_md(step))
            if not env.get("steps"):
                lines.append("- （无步骤记录）")
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append("## 范围与限制")
    lines.append("")
    for lim in summary.get("limitations", []):
        lines.append(f"- {lim}")
    lines.append("- 报告中的结论只对上面列出的检查项负责；未列出的功能与风险未被验证。")
    lines.append("")
    return "\n".join(lines)


def build_json_report(task: dict, version: dict, run: dict, result: dict) -> dict:
    return {
        "report_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "本报告仅覆盖列出的检查项；所有证据均已脱敏（Authorization/Cookie/密码/token 替换为 [REDACTED]）。",
        "task": {
            "id": task["id"],
            "name": task["name"],
            "requirement": task.get("requirement", ""),
            "baseline_url": task["baseline_url"],
            "candidate_url": task["candidate_url"],
        },
        "rule_version": {
            "id": version["id"],
            "version": version["version"],
            "status": version["status"],
            "note": version.get("note", ""),
            "confirmed_at": version.get("confirmed_at"),
        },
        "run": {
            "id": run["id"],
            "status": run["status"],
            "started_at": run["started_at"],
            "finished_at": run.get("finished_at"),
            "error": run.get("error"),
        },
        "summary": result.get("summary", {}),
        "checks": result.get("checks", []),
    }
