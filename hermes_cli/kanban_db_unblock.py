"""Reasoned recovery from the block-loop breaker's triage hold."""

import sqlite3
from typing import Optional


def unblock_loop_triage(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str], now: int,
) -> tuple[bool, Optional[str]]:
    """Called inside unblock_task's write transaction, after checking triage.

    Ignore comments, assignments and other observations, but never let an old
    breaker event authorise recovery after a later status transition.
    """
    from hermes_cli import kanban_db as kb

    entered = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ("
        "'created', 'status', 'claimed', 'claim_rejected', 'promoted', 'promoted_manual', "
        "'blocked', 'dependency_wait', 'block_loop_detected', 'scheduled', 'unblocked', "
        "'review_requested', 'changes_requested', 'review_reopened', 'specified', "
        "'decomposed', 'completed', 'archived', 'descendant_invalidated', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited', 'gave_up', "
        "'reconciled_orphans') ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if entered is None or entered["kind"] != "block_loop_detected":
        return False, "triage did not come from block_loop_detected; use `hermes kanban specify`"
    reason = (reason or "").strip()
    if not reason:
        return False, "a reason is required to unblock block-loop triage (CLI: --reason)"
    recurrences = conn.execute(
        "SELECT block_recurrences FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()["block_recurrences"]
    kb._reclaim_dangling_run(
        conn, task_id, statuses=("triage",), now=now,
        note="invariant recovery on triage unblock",
    )
    conn.execute(
        "UPDATE tasks SET status = ?, current_run_id = NULL, "
        "block_recurrences = 0, block_kind = NULL, block_reason = NULL, "
        "consecutive_failures = 0, last_failure_error = NULL "
        "WHERE id = ? AND status = 'triage'",
        (kb._landing_status_after_parents(conn, task_id), task_id),
    )
    kb._append_event(conn, task_id, "unblocked", {
        "from": "triage", "reason": reason, "recurrences_reset": int(recurrences or 0),
    })
    return True, None
