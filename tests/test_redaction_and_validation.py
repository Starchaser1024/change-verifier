from verifier.models import validate_checks
from verifier.redaction import REDACTED, mask_text, redact_headers, redact_obj


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
