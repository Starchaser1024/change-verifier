"""未执行的检查（如运行被取消）必须归类为「无法判断」，不能当作通过或失败。"""

from verifier.classification import CLASSIFICATION_LABELS
from verifier.runner import _build_comparisons

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
