"""Read-only SQLite helpers shared by schema and SQL tools."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'
