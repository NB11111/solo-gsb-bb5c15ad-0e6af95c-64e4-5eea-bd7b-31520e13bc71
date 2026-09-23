"""jobs 表读写。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "reference_beats": json.loads(row["reference_beats"]),
        "performance_onsets": json.loads(row["performance_onsets"]),
        # 兼容迁移前的旧数据库（旧行没有 anchors 列值）
        "anchors": json.loads(row["anchors"]) if row["anchors"] else [],
        "params": json.loads(row["params"]),
        "status": row["status"],
        "error": row["error"],
        "created_at": row["created_at"],
        "aligned_at": row["aligned_at"],
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def create_job(
    conn: sqlite3.Connection,
    name: str | None,
    reference_beats: list[float],
    performance_onsets: list[float],
    params: dict[str, Any],
    anchors: list[Any] | None = None,
) -> dict[str, Any]:
    cur = conn.execute(
        """
        INSERT INTO jobs (name, reference_beats, performance_onsets, anchors,
                          params, status, error, result, created_at, aligned_at)
        VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, NULL)
        """,
        (
            name,
            json.dumps(reference_beats),
            json.dumps(performance_onsets),
            json.dumps(anchors or []),
            json.dumps(params),
            _now(),
        ),
    )
    conn.commit()
    return get_job(conn, cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_job(row) for row in rows]


def save_result(conn: sqlite3.Connection, job_id: int, result: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', result=?, error=NULL, aligned_at=? WHERE id=?",
        (json.dumps(result), _now(), job_id),
    )
    conn.commit()


def save_error(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    conn.execute(
        "UPDATE jobs SET status='error', error=?, aligned_at=? WHERE id=?",
        (message, _now(), job_id),
    )
    conn.commit()
