"""结果分类：把「同一检查在 baseline / candidate 两环境的结果」映射为结论。

分类含义：
- pass              通过       修改前后都通过
- regression        本次引入的回归   修改前通过、修改后失败
- pre_existing      原有问题   修改前后都失败
- fixed             相比修改前已改善   修改前失败、修改后通过
- satisfied         新需求满足   new_requirement 检查在修改后通过
- not_met           新需求未满足  new_requirement 检查在修改后仍失败
- expected_change   符合已确认需求的行为变化   旧行为改变，但被一条已确认的新需求检查解释
- unverifiable      无法判断   任一环境服务不可达 / 超时 / 检查未完成
"""

from __future__ import annotations

from .executor import FAILED, PASSED, UNVERIFIABLE

CLASSIFICATION_LABELS = {
    "pass": "通过",
    "regression": "本次引入的回归",
    "pre_existing": "原有问题",
    "fixed": "相比修改前已改善",
    "satisfied": "新需求满足",
    "not_met": "新需求未满足",
    "expected_change": "符合已确认需求的行为变化",
    "unverifiable": "无法判断",
}

# 报告中的展示顺序
CLASSIFICATION_ORDER = [
    "regression",
    "not_met",
    "unverifiable",
    "pre_existing",
    "pass",
    "satisfied",
    "expected_change",
    "fixed",
]


def classify_check(check: dict, baseline: dict, candidate: dict, env_results_by_id: dict[str, tuple[dict, dict]]) -> dict:
    """返回 {code, label, reason}。

    env_results_by_id: {check_id: (baseline_result, candidate_result)}，
    用于解析 expected_change_ref（引用的新需求检查在修改后是否通过）。
    """
    category = check.get("category", "existing_behavior")
    b_status, c_status = baseline["status"], candidate["status"]

    def decide(code: str, reason: str) -> dict:
        return {"code": code, "label": CLASSIFICATION_LABELS[code], "reason": reason}

    if category == "new_requirement":
        if c_status == UNVERIFIABLE:
            return decide("unverifiable", f"新需求无法判断：修改后环境{candidate['reason']}")
        if c_status == PASSED:
            note = "（修改前不满足，符合预期）" if b_status == FAILED else "（修改前也已满足）"
            return decide("satisfied", f"修改后满足该新需求{note}")
        baseline_note = ""
        if b_status == PASSED:
            baseline_note = "；注意：修改前该检查是通过的，说明行为可能被改坏"
        return decide("not_met", f"修改后仍未满足该新需求：{candidate['reason']}{baseline_note}")

    # ---------------- existing_behavior ----------------
    if b_status == UNVERIFIABLE or c_status == UNVERIFIABLE:
        which = "修改前" if b_status == UNVERIFIABLE else "修改后"
        detail = baseline["reason"] if b_status == UNVERIFIABLE else candidate["reason"]
        return decide("unverifiable", f"{which}环境无法判断：{detail}")
    if b_status == PASSED and c_status == PASSED:
        return decide("pass", "修改前后均通过")
    if b_status == PASSED and c_status == FAILED:
        ref = check.get("expected_change_ref")
        if ref:
            ref_result = env_results_by_id.get(ref)
            if ref_result is not None and ref_result[1]["status"] == PASSED:
                return decide(
                    "expected_change",
                    f"修改前行为在修改后被改变，但已被已确认的新需求检查「{ref}」覆盖，不算回归",
                )
        return decide("regression", f"修改前通过，修改后失败：{candidate['reason']}")
    if b_status == FAILED and c_status == FAILED:
        return decide("pre_existing", "修改前后均失败（原有问题，非本次引入）")
    if b_status == FAILED and c_status == PASSED:
        return decide("fixed", "修改前失败，修改后通过")
    return decide("unverifiable", "结果组合无法识别")  # pragma: no cover


def compare_check(check: dict, baseline: dict, candidate: dict, env_results_by_id: dict[str, tuple[dict, dict]]) -> dict:
    """比较单个检查在两环境的结果，返回完整比较记录。"""
    classification = classify_check(check, baseline, candidate, env_results_by_id)

    def env_summary(r: dict) -> str:
        label = {"passed": "通过", "failed": "失败", "unverifiable": "无法判断"}[r["status"]]
        return label

    diff = f"修改前：{env_summary(baseline)}；修改后：{env_summary(candidate)}"
    if baseline["status"] != candidate["status"]:
        diff += f"（变化：{env_summary(baseline)} -> {env_summary(candidate)}）"

    return {
        "id": check.get("id"),
        "name": check.get("name"),
        "category": check.get("category", "existing_behavior"),
        "expectation": check.get("expectation", ""),
        "expected_change_ref": check.get("expected_change_ref"),
        "classification": classification,
        "baseline": baseline,
        "candidate": candidate,
        "diff_summary": diff,
    }


def build_summary(
    comparisons: list[dict],
    total_checks: int,
    run_status: str,
    cancelled: bool = False,
    executed_checks: int | None = None,
) -> dict:
    """统计分类数量并生成一句整体结论。

    executed_checks 为实际执行过的检查数（未执行的检查也会出现在 comparisons 里，
    归类为「无法判断」）。不传时回退为 len(comparisons)，便于单独构造 comparisons 的调用方。
    """
    counts: dict[str, int] = {}
    for cmp in comparisons:
        code = cmp["classification"]["code"]
        counts[code] = counts.get(code, 0) + 1

    executed = len(comparisons) if executed_checks is None else executed_checks
    notes: list[str] = []
    if counts.get("regression"):
        headline = f"发现 {counts['regression']} 项本次引入的回归，建议修复后再上线"
    elif counts.get("not_met"):
        headline = f"新需求未完全满足：{counts['not_met']} 项检查未通过"
    elif counts.get("unverifiable"):
        headline = f"存在 {counts['unverifiable']} 项无法判断的检查（服务不可达或执行未完成），结论不完整"
    else:
        headline = "在已列出的检查范围内，未发现本次引入的回归"

    if counts.get("not_met") and counts.get("regression"):
        notes.append(f"另有 {counts['not_met']} 项新需求未满足")
    if counts.get("unverifiable"):
        notes.append(f"{counts['unverifiable']} 项无法判断（不算通过，也不算失败）")
    if counts.get("pre_existing"):
        notes.append(f"{counts['pre_existing']} 项为原有问题（修改前后都失败）")
    if counts.get("expected_change"):
        notes.append(f"{counts['expected_change']} 项行为变化已由确认的新需求解释")
    if counts.get("fixed"):
        notes.append(f"{counts['fixed']} 项相比修改前有改善")
    if cancelled or run_status == "cancelled":
        notes.append("运行被用户取消，部分检查未执行")

    limitations = [
        "本报告只覆盖上方列出的检查项，不代表整个项目正确，也不代表不存在安全漏洞。",
        "超时、连接失败等环境问题归类为「无法判断」，不视为业务失败。",
        "证据为脱敏后的真实请求与响应；敏感字段（Authorization/Cookie/密码/token）已被替换。",
    ]

    return {
        "counts": counts,
        "labels": CLASSIFICATION_LABELS,
        "executed_checks": executed,
        "total_checks": total_checks,
        "headline": headline,
        "notes": notes,
        "limitations": limitations,
    }


def classification_order() -> list[str]:
    return list(CLASSIFICATION_ORDER)
