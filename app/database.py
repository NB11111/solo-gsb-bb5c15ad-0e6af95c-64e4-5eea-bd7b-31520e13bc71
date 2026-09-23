"""SQLite 连接与建表。"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator

DB_PATH = os.environ.get("ALIGN_DB_PATH", "/data/alignment.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT,
    reference_beats     TEXT    NOT NULL,   -- JSON 数组（秒）
    performance_onsets  TEXT    NOT NULL,   -- JSON 数组（秒）
    anchors             TEXT    NOT NULL    DEFAULT '[]',  -- JSON 锚点数组
    params              TEXT    NOT NULL,   -- JSON 参数
    status              TEXT    NOT NULL,   -- pending / done / error
    error               TEXT,
    result              TEXT,               -- JSON 对齐结果
    created_at          TEXT    NOT NULL,
    aligned_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """为旧版数据库补齐 anchors 列（幂等）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "anchors" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN anchors TEXT NOT NULL DEFAULT '[]'")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
