"""Board-owned plan identities, atomic across every materialization client.

The project is a logical namespace, not a profile-local projects.db lookup.
References survive archival: a new execution or replacement needs a new logical id.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterable

from hermes_cli.kanban_db_connect import write_txn


_MATERIALIZATIONS_SQL = """
CREATE TABLE IF NOT EXISTS task_materializations (
    project TEXT NOT NULL,
    plan_revision TEXT NOT NULL,
    proposed_task_id TEXT NOT NULL,
    intent_fingerprint TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (project, plan_revision, proposed_task_id)
)
"""


def migrate_materializations(conn: sqlite3.Connection) -> None:
    """Add storage without rewriting existing cards or their execution kinds."""
    conn.execute(_MATERIALIZATIONS_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_materializations_logical_task "
                 "ON task_materializations(project, proposed_task_id, intent_fingerprint)")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_materialization_retirements (
        project TEXT NOT NULL,
        proposed_task_id TEXT NOT NULL,
        task_id TEXT NOT NULL REFERENCES tasks(id),
        replacement_id TEXT NOT NULL,
        PRIMARY KEY (project, proposed_task_id)
    )""")
    # Earlier versions recorded renames only in events. Preserve their retirement
    # across upgrades too, without rewriting the cards or their historical rows.
    for event in conn.execute("SELECT task_id, payload FROM task_events "
                              "WHERE kind='materialization_mapped'"):
        payload = json.loads(event["payload"])
        mapping = _parse_mapping(payload["maps_from"])
        assert mapping is not None
        source_id = mapping[1]
        if source_id != payload["proposed_task_id"]:
            conn.execute("INSERT OR IGNORE INTO task_materialization_retirements "
                "(project, proposed_task_id, task_id, replacement_id) VALUES (?,?,?,?)",
                (payload["project"], source_id, event["task_id"], payload["proposed_task_id"]))


def require_deletable_card(conn: sqlite3.Connection, task_id: str) -> None:
    """Identity rows are durable duplicate guards, not disposable task relations."""
    if conn.execute("SELECT 1 FROM task_materializations WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
        raise ValueError("materialized_card_retained")


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _parse_mapping(maps_from: str | None) -> tuple[str, str] | None:
    if maps_from is None:
        return None
    if not isinstance(maps_from, str) or maps_from.count(":") != 1:
        raise ValueError("maps_from must be prior_revision:prior_proposed_id")
    revision, proposed_id = maps_from.split(":")
    _require_text("maps_from revision", revision)
    _require_text("maps_from proposed id", proposed_id)
    return revision, proposed_id


def _reference(conn: sqlite3.Connection, project: str, revision: str, proposed_id: str):
    return conn.execute(
        "SELECT intent_fingerprint, task_id FROM task_materializations "
        "WHERE project=? AND plan_revision=? AND proposed_task_id=?",
        (project, revision, proposed_id),
    ).fetchone()


def _mapping_source(conn: sqlite3.Connection, project: str, plan_revision: str,
                    mapping: tuple[str, str] | None):
    if mapping is None:
        return None
    revision, proposed_id = mapping
    if revision == plan_revision:
        raise ValueError("maps_from must name a prior revision")
    source = _reference(conn, project, revision, proposed_id)
    if source is None:
        raise ValueError("mapping_not_found")
    return source


def _conflict(task_id: str, reason: str) -> dict:
    return {"state": "conflict", "task_id": task_id, "reason": reason}


def _parent_tasks(conn: sqlite3.Connection, project: str, plan_revision: str,
                  parent_proposed: Iterable[str], pending_parents=None) -> list[str]:
    parents = []
    for proposed_id in dict.fromkeys(parent_proposed):
        _require_text("parent proposed id", proposed_id)
        row = _reference(conn, project, plan_revision, proposed_id)
        if row is not None:
            parents.append(row["task_id"])
        elif pending_parents is not None and proposed_id in pending_parents:
            # Check-only projection from earlier checked plan rows. No task or
            # materialization reference is ever inserted for a pending parent.
            task_id = pending_parents[proposed_id]
            if task_id is not None and conn.execute(
                    "SELECT 1 FROM task_materializations WHERE project=? AND task_id=? LIMIT 1",
                    (project, task_id)).fetchone() is None:
                raise ValueError("pending_parent_not_found")
            parents.append(task_id if task_id is not None else "pending:" + proposed_id)
        else:
            raise ValueError(f"unknown parent proposed id in this revision: {proposed_id}")
    return parents


def _retired(conn, project, proposed_id):
    return conn.execute("SELECT task_id FROM task_materialization_retirements "
                        "WHERE project=? AND proposed_task_id=?", (project, proposed_id)).fetchone()


def _decision(conn, *, project, plan_revision, proposed_task_id, intent_fingerprint,
              title, body=None, parent_proposed=(), maps_from=None,
              execution_kind="external_session", pending_parents=None):
    """Pure row validation shared by the read-only check and the locked writer."""
    from hermes_cli import kanban_db as kb

    for name, value in (("project", project), ("plan_revision", plan_revision),
                        ("proposed_task_id", proposed_task_id),
                        ("intent_fingerprint", intent_fingerprint), ("title", title)):
        _require_text(name, value)
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be text or null")
    if execution_kind != "external_session":
        raise ValueError("materialization requires execution_kind external_session")
    mapping = _parse_mapping(maps_from)
    retired = _retired(conn, project, proposed_task_id)
    if retired is not None:
        return _conflict(retired["task_id"], "logical_id_retired"), []
    current = _reference(conn, project, plan_revision, proposed_task_id)
    if current is not None:
        if current["intent_fingerprint"] != intent_fingerprint:
            return _conflict(current["task_id"], "materialization_conflict"), []
        return {"state": "existing", "task_id": current["task_id"]}, []

    source = _mapping_source(conn, project, plan_revision, mapping)
    prior = conn.execute(
        "SELECT intent_fingerprint, task_id FROM task_materializations "
        "WHERE project=? AND proposed_task_id=? ORDER BY created_at, plan_revision",
        (project, proposed_task_id),
    ).fetchall()
    matching = next((r for r in prior if r["intent_fingerprint"] == intent_fingerprint), None)
    if matching is not None:
        return {"state": "existing", "task_id": matching["task_id"]}, []
    if source is None:
        if prior:
            return _conflict(prior[0]["task_id"], "revision_requires_mapping"), []
        return {"state": "created", "task_id": None}, _parent_tasks(
            conn, project, plan_revision, parent_proposed, pending_parents)
    assert mapping is not None
    tid = source["task_id"]
    if any(r["task_id"] != tid for r in prior):
        return _conflict(prior[0]["task_id"], "materialization_conflict"), []
    if mapping[1] != proposed_task_id:
        # The keep-first order must refuse too. Neither revision ordering nor
        # creation timestamps may silently pick one alias as the winner.
        if (_retired(conn, project, mapping[1]) is not None
                or _reference(conn, project, plan_revision, mapping[1]) is not None):
            return _conflict(tid, "logical_id_retired"), []
    parents = _parent_tasks(conn, project, plan_revision, parent_proposed, pending_parents)
    task = kb.get_task(conn, tid)
    assert task is not None
    changed = (task.title != title or task.body != body or set(kb.parent_ids(conn, tid)) != set(parents))
    started = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? UNION ALL "
                           "SELECT 1 FROM task_external_bindings WHERE task_id=? LIMIT 1",
                           (tid, tid)).fetchone() is not None
    if changed and started:
        return _conflict(tid, "mapping_card_started"), []
    for parent in parents:
        if kb._would_cycle(conn, parent, tid):
            raise ValueError("mapping_dependency_cycle")
    return {"state": "mapped", "task_id": tid}, parents


@contextmanager
def _read_snapshot(conn):
    if conn.in_transaction:
        yield
    else:
        conn.execute("BEGIN")
        try:
            yield
        finally:
            conn.execute("ROLLBACK")


def check_materialization(conn: sqlite3.Connection, **row) -> dict:
    """No writes, no id allocation. Eligible new rows return task_id=null.

    A check is advisory: the writer revalidates under its own IMMEDIATE lock.
    """
    with _read_snapshot(conn):
        result, _ = _decision(conn, **row)
        return result


def _apply_mapping(conn, task_id, title, body, parents):
    from hermes_cli import kanban_db as kb

    conn.execute("UPDATE tasks SET title=?,body=? WHERE id=?", (title, body, task_id))
    conn.execute("DELETE FROM task_links WHERE child_id=?", (task_id,))
    for parent in parents:
        kb._link(conn, parent, task_id)
    kb._inherit_notify_subs(conn, task_id, parents)
    # Keep intentional archived/blocked/etc states, and never reopen an attempt.
    status = "ready" if kb._parents_satisfied(conn, task_id) else "todo"
    conn.execute("UPDATE tasks SET status=? WHERE id=? AND status IN ('todo','ready')", (status, task_id))


def materialize_task(
    conn: sqlite3.Connection, *, project: str, plan_revision: str, proposed_task_id: str,
    intent_fingerprint: str, title: str, body: str | None = None,
    parent_proposed: Iterable[str] = (), maps_from: str | None = None,
    execution_kind: str = "external_session",
) -> dict:
    """Atomically validate, apply unstarted mapped intent, and retain identity."""
    from hermes_cli import kanban_db as kb

    kb._assert_not_delegated_child_mutation()
    with write_txn(conn):
        result, parents = _decision(conn, project=project, plan_revision=plan_revision,
            proposed_task_id=proposed_task_id, intent_fingerprint=intent_fingerprint,
            title=title, body=body, parent_proposed=parent_proposed, maps_from=maps_from,
            execution_kind=execution_kind)
        state, task_id = result["state"], result["task_id"]
        if state == "conflict" or _reference(conn, project, plan_revision, proposed_task_id) is not None:
            return result
        if state == "created":
            task_id = kb.create_task(conn, title=title, body=body, parents=parents,
                execution_kind=execution_kind, workspace_kind="scratch", project_id="")
        if state == "mapped":
            _apply_mapping(conn, task_id, title, body, parents)
            mapping = _parse_mapping(maps_from)
            assert mapping is not None
            if mapping[1] != proposed_task_id:
                conn.execute("INSERT INTO task_materialization_retirements "
                    "(project,proposed_task_id,task_id,replacement_id) VALUES (?,?,?,?)",
                    (project, mapping[1], task_id, proposed_task_id))
        conn.execute(
            "INSERT INTO task_materializations "
            "(project,plan_revision,proposed_task_id,intent_fingerprint,task_id,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (project, plan_revision, proposed_task_id, intent_fingerprint, task_id, int(time.time())),
        )
        if state == "mapped":
            kb._append_event(conn, task_id, "materialization_mapped", dict(project=project,
                plan_revision=plan_revision, proposed_task_id=proposed_task_id,
                intent_fingerprint=intent_fingerprint, maps_from=maps_from))
        return {"state": state, "task_id": task_id}
