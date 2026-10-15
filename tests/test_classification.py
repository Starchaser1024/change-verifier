from verifier.classification import build_summary, classify_check, compare_check
from verifier.executor import FAILED, PASSED, UNVERIFIABLE


def env(status, reason="reason"):
    return {"env": "e", "status": status, "reason": reason, "steps": []}


def existing_check(**extra):
    return {"id": "c1", "name": "检查", "category": "existing_behavior", **extra}


def nr_check():
    return {"id": "n1", "name": "新需求", "category": "new_requirement"}


def test_pass():
    cl = classify_check(existing_check(), env(PASSED), env(PASSED), {})
    assert cl["code"] == "pass"


def test_regression():
    cl = classify_check(existing_check(), env(PASSED), env(FAILED), {})
    assert cl["code"] == "regression"


def test_pre_existing():
    cl = classify_check(existing_check(), env(FAILED), env(FAILED), {})
    assert cl["code"] == "pre_existing"


def test_fixed():
    cl = classify_check(existing_check(), env(FAILED), env(PASSED), {})
    assert cl["code"] == "fixed"


def test_unverifiable_any_side():
    assert classify_check(existing_check(), env(UNVERIFIABLE), env(PASSED), {})["code"] == "unverifiable"
    assert classify_check(existing_check(), env(PASSED), env(UNVERIFIABLE), {})["code"] == "unverifiable"
    assert classify_check(nr_check(), env(PASSED), env(UNVERIFIABLE), {})["code"] == "unverifiable"


def test_new_requirement_satisfied_and_not_met():
    assert classify_check(nr_check(), env(FAILED), env(PASSED), {})["code"] == "satisfied"
    assert classify_check(nr_check(), env(FAILED), env(FAILED), {})["code"] == "not_met"
    assert classify_check(nr_check(), env(PASSED), env(FAILED), {})["code"] == "not_met"


def test_expected_change_resolved_by_confirmed_new_requirement():
    check = existing_check(expected_change_ref="n1")
    by_id = {"n1": (env(FAILED), env(PASSED))}
    cl = classify_check(check, env(PASSED), env(FAILED), by_id)
    assert cl["code"] == "expected_change"

    # 引用的新需求检查本身没通过 → 仍然是回归
    by_id_fail = {"n1": (env(FAILED), env(FAILED))}
    cl2 = classify_check(check, env(PASSED), env(FAILED), by_id_fail)
    assert cl2["code"] == "regression"

    # 引用的新需求无法判断 → 不能宣布不是回归
    by_id_unknown = {"n1": (env(FAILED), env(UNVERIFIABLE))}
    cl3 = classify_check(check, env(PASSED), env(FAILED), by_id_unknown)
    assert cl3["code"] == "regression"


def test_compare_and_summary_headline_priority():
    checks = [
        existing_check(id="a"),
        existing_check(id="b"),
        nr_check(),
    ]
    cmp_a = compare_check(checks[0], env(PASSED), env(FAILED), {})
    cmp_b = compare_check(checks[1], env(FAILED), env(FAILED), {})
    cmp_n = compare_check(checks[2], env(FAILED), env(FAILED), {})
    summary = build_summary([cmp_a, cmp_b, cmp_n], total_checks=3, run_status="completed")
    assert summary["counts"]["regression"] == 1
    assert summary["counts"]["pre_existing"] == 1
    assert summary["counts"]["not_met"] == 1
    assert "回归" in summary["headline"]
    assert any("新需求未满足" in n for n in summary["notes"])
    assert any("原有问题" in n for n in summary["notes"])


def test_summary_headline_without_regression():
    cmp_u = compare_check(existing_check(), env(UNVERIFIABLE), env(PASSED), {})
    summary = build_summary([cmp_u], total_checks=1, run_status="completed")
    assert "无法判断" in summary["headline"]


def test_summary_headline_clean():
    cmp_p = compare_check(existing_check(), env(PASSED), env(PASSED), {})
    summary = build_summary([cmp_p], total_checks=1, run_status="completed")
    assert "未发现本次引入的回归" in summary["headline"]
    assert summary["counts"]["pass"] == 1
