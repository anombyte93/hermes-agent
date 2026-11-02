"""Board-owned plan identities, atomic across every materialization client.

The project is a logical namespace, not a profile-local projects.db lookup.
References survive archival: a new execution or replacement needs a new logical id.
"""
from __future__ import annotations

import sqlite3
import time
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
                  parent_proposed: Iterable[str]) -> list[str]:
    parents = []
    for proposed_id in dict.fromkeys(parent_proposed):
        _require_text("parent proposed id", proposed_id)
        row = _reference(conn, project, plan_revision, proposed_id)
        if row is None:
            raise ValueError(f"unknown parent proposed id in this revision: {proposed_id}")
        parents.append(row["task_id"])
    return parents


def materialize_task(
    conn: sqlite3.Connection, *, project: str, plan_revision: str, proposed_task_id: str,
    intent_fingerprint: str, title: str, body: str | None = None,
    parent_proposed: Iterable[str] = (), maps_from: str | None = None,
    execution_kind: str = "external_session",
) -> dict:
    """Return created/existing/mapped or a conflict, under one IMMEDIATE txn.

    Same-revision intent is immutable. Across revisions an unchanged intent
    reuses its card; a changed intent needs an explicit, real prior reference.
    A rename may map to a prior id, but cannot redirect an established id to
    another card. Reuse never rewrites the card, dependencies or execution state.
    Parents for a new card must have been materialized in this exact revision.
    """
    from hermes_cli import kanban_db as kb

    for name, value in (("project", project), ("plan_revision", plan_revision),
                        ("proposed_task_id", proposed_task_id),
                        ("intent_fingerprint", intent_fingerprint), ("title", title)):
        _require_text(name, value)
    if execution_kind != "external_session":
        raise ValueError("materialization requires execution_kind external_session")
    mapping = _parse_mapping(maps_from)

    with write_txn(conn):
        current = _reference(conn, project, plan_revision, proposed_task_id)
        if current is not None:
            if current["intent_fingerprint"] != intent_fingerprint:
                return _conflict(current["task_id"], "materialization_conflict")
            return {"state": "existing", "task_id": current["task_id"]}

        source = _mapping_source(conn, project, plan_revision, mapping)
        prior = conn.execute(
            "SELECT intent_fingerprint, task_id FROM task_materializations "
            "WHERE project=? AND proposed_task_id=? ORDER BY created_at, plan_revision",
            (project, proposed_task_id),
        ).fetchall()
        matching = next((row for row in prior if row["intent_fingerprint"] == intent_fingerprint), None)
        if matching is not None:
            task_id, state = matching["task_id"], "existing"
        elif source is not None:
            if any(row["task_id"] != source["task_id"] for row in prior):
                return _conflict(prior[0]["task_id"], "materialization_conflict")
            task_id, state = source["task_id"], "mapped"
        elif prior:
            return _conflict(prior[0]["task_id"], "revision_requires_mapping")
        else:
            parents = _parent_tasks(conn, project, plan_revision, parent_proposed)
            # No profile-local project lookup, inherited board worktree or worker
            # idempotency key. create_task composes via a savepoint, not a commit.
            task_id = kb.create_task(conn, title=title, body=body, parents=parents,
                execution_kind=execution_kind, workspace_kind="scratch", project_id="")
            state = "created"

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
