"""Additive external-session storage and sticky execution-kind constraints."""
from __future__ import annotations

import sqlite3


_BINDINGS_SQL = """
CREATE TABLE IF NOT EXISTS task_external_bindings (
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    owner_host TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    pid TEXT NOT NULL,
    conversation TEXT NOT NULL,
    worktree TEXT NOT NULL,
    reservation_id TEXT NOT NULL UNIQUE,
    reservation_token_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'closed', 'handed_over')),
    bound_at INTEGER NOT NULL,
    closed_at INTEGER,
    close_reason TEXT,
    PRIMARY KEY (task_id, run_id)
)
"""


def migrate_external_sessions(conn: sqlite3.Connection) -> None:
    """Called after tasks' additive columns exist, including on populated boards."""
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn, allow_nested=True):
        conn.execute(_BINDINGS_SQL)
        columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(task_external_bindings)")}
        if columns["pid"] == "INTEGER":
            # Recover the owner's literal value from JSON, not CAST(pid), which
            # has already lost leading zeroes. Table and indexes change atomically.
            conn.execute(_BINDINGS_SQL.replace("task_external_bindings (", "task_external_bindings_literal_pid ("))
            names = ",".join(columns)
            values = ",".join("json_extract(incarnation, '$.pid')" if name == "pid" else name for name in columns)
            conn.execute(f"INSERT INTO task_external_bindings_literal_pid ({names}) "
                         f"SELECT {values} FROM task_external_bindings")
            conn.execute("DROP TABLE task_external_bindings")
            conn.execute("ALTER TABLE task_external_bindings_literal_pid RENAME TO task_external_bindings")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_external_active_task "
                     "ON task_external_bindings(task_id) WHERE state = 'active'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_external_active_process "
                     "ON task_external_bindings(owner_host, pid, conversation) WHERE state = 'active'")
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_tasks_execution_kind_insert
            BEFORE INSERT ON tasks
            WHEN NEW.execution_kind IS NULL OR NEW.execution_kind NOT IN ('worker', 'external_session')
            BEGIN
                SELECT RAISE(ABORT, 'invalid execution_kind');
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_tasks_execution_kind_sticky
            BEFORE UPDATE OF execution_kind ON tasks
            WHEN NEW.execution_kind IS NOT OLD.execution_kind
            BEGIN
                SELECT RAISE(ABORT, 'execution_kind is immutable');
            END
        """)
