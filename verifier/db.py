"""SQLite 存储：任务、规则版本（不可变）、运行记录。

- 规则版本只增不改：每次保存都会创建新版本；运行记录引用当时的规则版本。
- 运行不会以任何方式修改规则，避免「为了通过测试而改规则」。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path("verifier_data") / "verifier.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    requirement TEXT NOT NULL DEFAULT '',
    baseline_url TEXT NOT NULL,
    candidate_url TEXT NOT NULL,
    allow_remote INTEGER NOT NULL DEFAULT 0,
    allow_remote_writes INTEGER NOT NULL DEFAULT 0,
    endpoints_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    checks_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE (task_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    status TEXT NOT NULL,
    error TEXT,
    result_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_path() -> Path:
    env = os.environ.get("VERIFIER_DB")
    return Path(env) if env else DEFAULT_DB_PATH


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]
