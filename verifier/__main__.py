"""`python -m verifier` 入口：默认启动网页工作台，同时支持 `serve` / `demo` / `verify` 子命令。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
