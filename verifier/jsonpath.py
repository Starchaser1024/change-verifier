"""极简 JSON 路径取值器，支持 $.a.b[0].c 形式。

不引入第三方依赖；返回值用 MISSING 哨兵区分「字段不存在」与「值为 null」。
"""

from __future__ import annotations

import re
from typing import Any


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"


MISSING = _Missing()

_TOKEN_RE = re.compile(r"[^.\[\]]+|\[(\d+)\]")


def parse_path(path: str) -> list[Any]:
    """把 '$.a.b[0]' / 'a.b[0]' / '$' 解析为 ['a', 'b', 0] 或 []。"""
    if not isinstance(path, str) or path.strip() == "":
        raise ValueError("JSON 路径不能为空")
    p = path.strip()
    if p.startswith("$"):
        p = p[1:]
    if p.startswith("."):
        p = p[1:]
    if p == "" or p == "$":
        return []
    tokens: list[Any] = []
    pos = 0
    for m in _TOKEN_RE.finditer(p):
        gap = p[pos:m.start()]
        if gap.strip(".") != "":
            raise ValueError(f"JSON 路径格式错误: {path}")
        if m.group(1) is not None:
            tokens.append(int(m.group(1)))
        else:
            tokens.append(m.group(0))
        pos = m.end()
    if pos != len(p) or p[pos:] != "":
        raise ValueError(f"JSON 路径格式错误: {path}")
    return tokens


def get_path(data: Any, path: str) -> Any:
    """按路径取值，不存在时返回 MISSING。"""
    for token in parse_path(path):
        if isinstance(token, int):
            if isinstance(data, (list, tuple)) and 0 <= token < len(data):
                data = data[token]
            else:
                return MISSING
        else:
            if isinstance(data, dict) and token in data:
                data = data[token]
            else:
                return MISSING
    return data
