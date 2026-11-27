from __future__ import annotations

import pytest

from verifier.models import validate_checks
from verifier.redaction import REDACTED, mask_text, redact_headers, redact_obj, redact_url


def test_redact_headers():
    headers = {
        "Authorization": "Bearer super-secret-token",
        "Cookie": "session=abc",
        "Content-Type": "application/json",
        "X-Request-Id": "123",
    }
    out = redact_headers(headers)
    assert out["Authorization"] == REDACTED
    assert out["Cookie"] == REDACTED
    assert out["Content-Type"] == "application/json"
    assert out["X-Request-Id"] == "123"


def test_redact_obj_by_key_name():
    data = {
        "username": "alice",
        "password": "alice-password",
        "access_token": "tok_123",
        "nested": {"api_key": "k-1", "items": [{"secret": "s"}, {"value": 1}]},
        "null_token": None,
    }
    out = redact_obj(data)
    assert out["username"] == "alice"
    assert out["password"] == REDACTED
    assert out["access_token"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["items"][0]["secret"] == REDACTED
    assert out["nested"]["items"][1]["value"] == 1
    assert out["null_token"] == REDACTED  # 键名敏感，值整体替换


def test_mask_text_replaces_extracted_secret_values():
    text = '{"msg": "hello", "token": "eyJhbGciOi.abc"}'
    masked = mask_text(text, ["eyJhbGciOi.abc"])
    assert "eyJhbGciOi.abc" not in masked
    assert REDACTED in masked


def test_redact_obj_masks_secret_values_in_strings():
    out = redact_obj({"note": "prefix tok-42 suffix"}, ["tok-42"])
    assert out["note"] == f"prefix {REDACTED} suffix"


def valid_check():
    return {
        "id": "sample-check",
        "name": "示例检查",
        "category": "existing_behavior",
        "steps": [
            {
                "method": "GET",
                "path": "/api/orders",
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            }
        ],
    }


def test_validate_ok():
    assert validate_checks([valid_check()]) == []


def test_validate_catches_common_mistakes():
    bad = valid_check()
    bad["id"] = "Bad ID"
    bad["category"] = "whatever"
    bad["steps"][0]["method"] = "FETCH"
    bad["steps"][0]["assert"] = [{"type": "status", "op": "equals", "value": "200"}]
    errors = validate_checks([bad])
    text = "\n".join(errors)
    assert "id 必须" in text
    assert "category" in text
    assert "method" in text
    assert "整数状态码" in text


def test_validate_rejects_duplicate_ids_and_bad_ref():
    c1 = valid_check()
    c2 = valid_check()
    c3 = valid_check()
    c3["id"] = "ref-check"
    c3["category"] = "existing_behavior"
    c3["expected_change_ref"] = "sample-check"  # 指向的检查不是 new_requirement
    errors = validate_checks([c1, c2, c3])
    text = "\n".join(errors)
    assert "重复" in text
    assert "只能指向「新需求验收」类检查" in text


def test_validate_rejects_empty_asserts_and_bad_timeout():
    c = valid_check()
    c["steps"][0]["assert"] = []
    c["steps"][0]["timeout"] = 999
    errors = validate_checks([c])
    text = "\n".join(errors)
    assert "非空数组" in text
    assert "timeout" in text


def test_validate_accepts_length_comparison_ops():
    """「最多 N 条」这类需求要能表达出来。"""
    c = valid_check()
    c["steps"][0]["assert"] = [
        {"type": "json", "path": "$.items", "op": "length_less_than", "value": 3},
        {"type": "json", "path": "$.items", "op": "length_greater_than", "value": 0},
    ]
    assert validate_checks([c]) == []


@pytest.mark.parametrize("bad_value", ["2", 2.5, None, True])
def test_validate_rejects_non_integer_length_value(bad_value):
    c = valid_check()
    c["steps"][0]["assert"] = [
        {"type": "json", "path": "$.items", "op": "length_less_than", "value": bad_value}
    ]
    text = "\n".join(validate_checks([c]))
    assert "必须是整数" in text


def test_redact_url_masks_credentials_sensitive_query_keys_and_encoded_secrets():
    raw = (
        "http://alice:super-pass@127.0.0.1:8620/api/orders"
        "?page=2&api_key=sk-query-123&cursor=tok%2Babc%2F1%3D2"
    )
    masked = redact_url(raw, ["tok+abc/1=2"])
    assert "alice" not in masked
    assert "super-pass" not in masked
    assert "sk-query-123" not in masked
    assert "tok" not in masked
    assert "page=2" in masked
    assert f"api_key={REDACTED}" in masked
    assert f"cursor={REDACTED}" in masked


def test_validate_accepts_params_and_oauth2_form_steps():
    c = {
        "id": "oauth2-and-params",
        "name": "OAuth2 表单登录与分页参数",
        "category": "existing_behavior",
        "steps": [
            {
                "name": "OAuth2 登录",
                "method": "POST",
                "path": "/token",
                "form": {
                    "grant_type": "password",
                    "username": "alice",
                    "password": "alice-password",
                    "scope": ["orders:read", "orders:write"],
                },
                "extract": {"token": {"source": "json", "path": "$.access_token", "sensitive": True}},
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            },
            {
                "name": "分页查询",
                "method": "GET",
                "path": "/api/orders",
                "params": {"page": 1, "page_size": 5, "active": True, "tag": ["vip", "paid"]},
                "headers": {"Authorization": "Bearer {{token}}"},
                "assert": [{"type": "status", "op": "equals", "value": 200}],
            },
        ],
    }
    assert validate_checks([c]) == []


def test_validate_rejects_json_plus_form_and_invalid_params_or_form_types():
    both = valid_check()
    both["steps"][0]["method"] = "POST"
    both["steps"][0]["json"] = {"username": "alice"}
    both["steps"][0]["form"] = {"username": "alice"}
    err_both = "\n".join(validate_checks([both]))
    assert "json 与 form 不能同时" in err_both

    bad_params = valid_check()
    bad_params["steps"][0]["params"] = {"filter": {"nested": 1}}
    assert any("params" in e for e in validate_checks([bad_params]))

    bad_form = valid_check()
    bad_form["steps"][0]["method"] = "POST"
    bad_form["steps"][0]["form"] = {"": "empty-key"}
    assert any("form" in e for e in validate_checks([bad_form]))
