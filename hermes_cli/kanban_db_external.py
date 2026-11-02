"""Token-fenced lifecycle for work owned by an already-running external session.

This module never probes, spawns, signals or cleans up the owner. Moll-E owns
reservation validation across boards; Hermes owns the board-local attempt.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_execution import execution_kind_of

_INCARNATION_FIELDS = frozenset({"session", "id", "created", "pane", "pid", "backend", "conversation"})


def _db():
    from hermes_cli import kanban_db
    return kanban_db


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("reservation_token must be 32 bytes encoded as lowercase hex")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _identity(owner_host, incarnation):
    if not isinstance(owner_host, str) or not owner_host.strip():
        raise ValueError("owner_host is required")
    if not isinstance(incarnation, dict) or set(incarnation) != _INCARNATION_FIELDS:
        raise ValueError("incarnation must contain session, id, created, pane, pid, backend, conversation")
    if any(not isinstance(incarnation[k], str) or not incarnation[k].strip()
           for k in _INCARNATION_FIELDS):
        raise ValueError("incarnation text fields must be nonempty strings")
    # Preserve values and their types. Sorting only makes JSON key order stable.
    return json.dumps(incarnation, sort_keys=True, separators=(",", ":"))


def _require_external(conn, task_id):
    if execution_kind_of(conn, task_id) != "external_session":
        raise ValueError("task is not external_session")
    task = _db().get_task(conn, task_id)
    if task is None:
        raise ValueError("external task is unavailable")
    return task


def _require_board(conn, board):
    if not board or board != _db().board_slug_for_conn(conn):
        raise ValueError("wrong board")


def _require_worktree(task, worktree):
    if not isinstance(worktree, str) or not Path(worktree).is_absolute():
        raise ValueError("worktree must be an absolute path")
    if task.workspace_path is not None and task.workspace_path != worktree:
        raise ValueError("worktree does not match the task workspace")


def _snapshot(conn, task_id):
    task = _require_external(conn, task_id)
    row = conn.execute("SELECT * FROM task_external_bindings WHERE task_id=? ORDER BY run_id DESC LIMIT 1",
                       (task_id,)).fetchone()
    binding = None
    attempt = None
    if row is not None:
        binding = {key: row[key] for key in row.keys() if key != "reservation_token_sha256"}
        binding["incarnation"] = json.loads(binding["incarnation"])
        run = _db().get_run(conn, row["run_id"])
        if run is None:
            raise ValueError("external binding attempt is unavailable")
        attempt = asdict(run)
    return dict(task_id=task_id, run_id=row["run_id"] if row else None,
                task=asdict(task), attempt=attempt, binding=binding)


def external_status(conn: sqlite3.Connection, task_id: str) -> dict:
    """Consistent read of card, latest attempt and binding, never the token/hash."""
    if conn.in_transaction:
        return _snapshot(conn, task_id)
    conn.execute("BEGIN")
    try:
        return _snapshot(conn, task_id)
    finally:
        conn.execute("ROLLBACK")


def _reservation_available(conn, owner_host, incarnation, reservation_id):
    if not isinstance(reservation_id, str) or not reservation_id.strip():
        raise ValueError("reservation_id is required")
    if conn.execute("SELECT 1 FROM task_external_bindings WHERE reservation_id=?",
                    (reservation_id,)).fetchone():
        raise ValueError("reservation_conflict: reservation already used")
    if conn.execute("SELECT 1 FROM task_external_bindings WHERE owner_host=? AND pid=? "
                    "AND conversation=? AND state='active'",
                    (owner_host, incarnation["pid"], incarnation["conversation"])).fetchone():
        raise ValueError("reservation_conflict: process incarnation already bound on this board")


def _open_attempt(conn, task, owner_host, incarnation, identity_json, worktree,
                  reservation_id, token_hash, now):
    lock = "external:" + reservation_id
    updated = conn.execute(
        "UPDATE tasks SET status='running',claim_lock=?,claim_expires=NULL,worker_pid=NULL,"
        "started_at=COALESCE(started_at,?) "
        "WHERE id=? AND execution_kind='external_session' AND status='ready' "
        "AND claim_lock IS NULL AND current_run_id IS NULL",
        (lock, now, task.id))
    if updated.rowcount != 1:
        raise ValueError("external claim lost: task is not ready and unclaimed")
    run_id = conn.execute(
        "INSERT INTO task_runs(task_id,profile,status,claim_lock,started_at) VALUES(?,?,'running',?,?)",
        (task.id, task.assignee, lock, now)).lastrowid
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, task.id))
    conn.execute(
        "INSERT INTO task_external_bindings(task_id,run_id,owner_host,incarnation,pid,conversation,"
        "worktree,reservation_id,reservation_token_sha256,state,bound_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'active',?)",
        (task.id, run_id, owner_host, identity_json, incarnation["pid"], incarnation["conversation"],
         worktree, reservation_id, token_hash, now))
    return run_id


def external_claim(conn: sqlite3.Connection, task_id: str, *, board: str, owner_host: str,
                   incarnation: dict, worktree: str, reservation_id: str, reservation_token: str) -> dict:
    identity_json = _identity(owner_host, incarnation)
    token_hash = _token_hash(reservation_token)
    with write_txn(conn):
        _require_board(conn, board)
        task = _require_external(conn, task_id)
        _require_worktree(task, worktree)
        prior = conn.execute("SELECT * FROM task_external_bindings WHERE reservation_id=?",
                             (reservation_id,)).fetchone()
        if prior is not None:
            if (prior["task_id"] == task_id and prior["owner_host"] == owner_host
                and json.loads(prior["incarnation"]) == incarnation and prior["worktree"] == worktree
                and hmac.compare_digest(prior["reservation_token_sha256"], token_hash)
                and prior["state"] == "active" and task.status == "running"
                and task.current_run_id == prior["run_id"]):
                return _snapshot(conn, task_id)
            raise ValueError("reservation_conflict: reservation replay does not match active binding")
        if task.status != "ready" or not _db()._parents_satisfied(conn, task_id):
            raise ValueError("external claim requires ready task and satisfied parents")
        if conn.execute("SELECT 1 FROM task_external_bindings WHERE task_id=? AND state='active'",
                        (task_id,)).fetchone():
            raise ValueError("external claim refused: live binding exists")
        _reservation_available(conn, owner_host, incarnation, reservation_id)
        run_id = _open_attempt(conn, task, owner_host, incarnation, identity_json, worktree,
                               reservation_id, token_hash, int(time.time()))
        _db()._append_event(conn, task_id, "external_claimed",
                            {"reservation_id": reservation_id, "owner_host": owner_host}, run_id=run_id)
        return _snapshot(conn, task_id)


def _owned_binding(conn, task_id, run_id, token, *, review=False):
    task = _require_external(conn, task_id)
    row = conn.execute("SELECT * FROM task_external_bindings WHERE task_id=? AND run_id=?",
                       (task_id, run_id)).fetchone()
    if row is None or not hmac.compare_digest(row["reservation_token_sha256"], _token_hash(token)):
        raise ValueError("external ownership refused: wrong attempt or reservation token")
    active = (row["state"] == "active" and task.status == "running" and task.current_run_id == run_id)
    latest = conn.execute("SELECT MAX(run_id) FROM task_external_bindings WHERE task_id=?",
                          (task_id,)).fetchone()[0]
    reviewing = (review and task.status == "review" and task.current_run_id is None
                 and latest == run_id and row["state"] == "closed" and row["close_reason"] == "review_requested")
    if not (active or reviewing):
        raise ValueError("external ownership refused: stale attempt")
    return task, row


def _close_attempt(conn, task_id, run_id, *, status, outcome, summary=None, result=None,
                   binding_state="closed"):
    now = int(time.time())
    closed = conn.execute(
        "UPDATE task_external_bindings SET state=?,closed_at=?,close_reason=? "
        "WHERE task_id=? AND run_id=? AND state='active'",
        (binding_state, now, outcome, task_id, run_id))
    if closed.rowcount != 1:
        raise ValueError("external close lost: stale attempt")
    updated = conn.execute(
        "UPDATE tasks SET status=?,result=?,completed_at=?,claim_lock=NULL,claim_expires=NULL,"
        "worker_pid=NULL,current_run_id=NULL WHERE id=? AND status='running' AND current_run_id=?",
        (status, result, now if status == "done" else None, task_id, run_id))
    if updated.rowcount != 1:
        raise ValueError("external close lost: stale attempt")
    ended = conn.execute(
        "UPDATE task_runs SET status=?,outcome=?,summary=?,ended_at=?,claim_lock=NULL,claim_expires=NULL "
        "WHERE task_id=? AND id=? AND ended_at IS NULL",
        (status, outcome, summary, now, task_id, run_id))
    if ended.rowcount != 1:
        raise ValueError("external close lost: attempt already ended")


def external_complete(conn: sqlite3.Connection, task_id: str, run_id: int, *,
                      reservation_token: str, summary=None, result=None, review=False) -> dict:
    with write_txn(conn):
        task, binding = _owned_binding(conn, task_id, run_id, reservation_token, review=not review)
        if not _db()._parents_satisfied(conn, task_id):
            raise ValueError("external completion refused: parents not satisfied")
        status, outcome = ("review", "review_requested") if review else ("done", "completed")
        if binding["state"] == "active":
            _close_attempt(conn, task_id, run_id, status=status, outcome=outcome, summary=summary, result=result)
        else:
            conn.execute("UPDATE tasks SET status='done',completed_at=?,result=? WHERE id=? AND status='review'",
                         (int(time.time()), result, task_id))
            conn.execute("UPDATE task_runs SET status='done',outcome='completed',summary=COALESCE(?,summary) "
                         "WHERE task_id=? AND id=?", (summary, task_id, run_id))
            conn.execute("UPDATE task_external_bindings SET close_reason='completed' WHERE task_id=? AND run_id=?",
                         (task_id, run_id))
        if status == "done":
            conn.execute("UPDATE tasks SET consecutive_failures=0,last_failure_error=NULL,block_kind=NULL,"
                         "block_recurrences=0 WHERE id=?", (task_id,))
        _db()._append_event(conn, task_id, "external_" + outcome,
                            {"summary": summary, "result": result}, run_id=run_id)
        snapshot = _snapshot(conn, task_id)
    if not review:
        _db().recompute_ready(conn)
    return snapshot


def external_release(conn: sqlite3.Connection, task_id: str, run_id: int, *,
                     reservation_token: str, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("release reason is required")
    with write_txn(conn):
        _owned_binding(conn, task_id, run_id, reservation_token)
        status = _db()._landing_status_after_parents(conn, task_id)
        _close_attempt(conn, task_id, run_id, status=status, outcome="released", summary=reason)
        _db()._append_event(conn, task_id, "external_released", {"reason": reason}, run_id=run_id)
        return _snapshot(conn, task_id)


def external_request_changes(conn: sqlite3.Connection, task_id: str, run_id: int, *,
                             reservation_token: str, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("changes reason is required")
    with write_txn(conn):
        task, binding = _owned_binding(conn, task_id, run_id, reservation_token, review=True)
        if task.status != "review" or binding["state"] != "closed":
            raise ValueError("external changes require a completed review handoff")
        status = _db()._landing_status_after_parents(conn, task_id)
        conn.execute("UPDATE tasks SET status=? WHERE id=? AND status='review'", (status, task_id))
        conn.execute("UPDATE task_runs SET status=?,outcome='changes_requested',summary=? WHERE task_id=? AND id=?",
                     (status, reason, task_id, run_id))
        conn.execute("UPDATE task_external_bindings SET close_reason='changes_requested' WHERE task_id=? AND run_id=?",
                     (task_id, run_id))
        _db()._append_event(conn, task_id, "external_changes_requested", {"reason": reason}, run_id=run_id)
        return _snapshot(conn, task_id)


def external_handover(conn: sqlite3.Connection, task_id: str, run_id: int, *, reservation_token: str,
                      owner_host: str, incarnation: dict, worktree: str, reservation_id: str,
                      new_reservation_token: str, evidence: str, board: str | None = None) -> dict:
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("handover evidence is required")
    identity_json = _identity(owner_host, incarnation)
    token_hash = _token_hash(new_reservation_token)
    with write_txn(conn):
        _require_board(conn, board)
        task, _ = _owned_binding(conn, task_id, run_id, reservation_token)
        _require_worktree(task, worktree)
        _close_attempt(conn, task_id, run_id, status="ready", outcome="handed_over",
                        summary=evidence, binding_state="handed_over")
        _reservation_available(conn, owner_host, incarnation, reservation_id)
        successor = _open_attempt(conn, task, owner_host, incarnation, identity_json, worktree,
                                  reservation_id, token_hash, int(time.time()))
        _db()._append_event(conn, task_id, "external_handed_over",
                            {"prior_run_id": run_id, "evidence": evidence}, run_id=successor)
        return _snapshot(conn, task_id)
