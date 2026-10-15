from verifier.jsonpath import MISSING, get_path, parse_path

import pytest


def test_parse_basic():
    assert parse_path("$.a.b") == ["a", "b"]
    assert parse_path("a.b") == ["a", "b"]
    assert parse_path("$.a[0].c") == ["a", 0, "c"]
    assert parse_path("$") == []


def test_get_nested_and_array():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}, "n": None}
    assert get_path(data, "$.a.b[1].c") == 2
    assert get_path(data, "$.a.b[0]") == {"c": 1}
    assert get_path(data, "$.n") is None
    assert get_path(data, "$") is data


def test_missing_is_sentinel_not_none():
    data = {"a": 1}
    assert get_path(data, "$.x") is MISSING
    assert get_path(data, "$.a[0]") is MISSING  # int 不是数组
    assert get_path(None, "$.a") is MISSING


def test_parse_errors():
    with pytest.raises(ValueError):
        parse_path("")
    with pytest.raises(ValueError):
        parse_path("$.a..b[")
