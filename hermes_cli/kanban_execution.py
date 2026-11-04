"""Execution eligibility shared by the kernel and dispatcher.

An unreadable or omitted kind is not permission to execute a worker.
"""
from __future__ import annotations

import sqlite3

VALID_EXECUTION_KINDS = frozenset({"worker", "external_session"})


def execution_kind_of(conn: sqlite3.Connection, task_id: str):
    try:
        row = conn.execute("SELECT execution_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row[0] if row is not None else None


def has_external_attempt(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_external_bindings WHERE task_id = ? AND state = 'active' LIMIT 1",
        (task_id,),
    ).fetchone() is not None
