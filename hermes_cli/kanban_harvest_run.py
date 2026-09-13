"""Dispatcher-side wiring for the workspace evidence harvest.

Every run end flows through :func:`hermes_cli.kanban_db._end_run` (completed,
crashed, timed_out, stale, reclaimed, gave_up, changes_requested). That is the
single chokepoint where the dispatcher can derive what the run actually
produced from the workspace + worker log, so the board records facts even when
the worker died before reporting anything (Lane A of the 2026-09-13 Align
wave: six commits on disk, zero metadata).

The heavy lifting lives in the PURE :mod:`hermes_cli.kanban_harvest`
(unit-tested against real log excerpts); this module only resolves the
inputs (workspace, base sha, log path, worker self-report) from a task id and
writes ``metadata.harvest`` + ``metadata.agreement`` onto the closing run.
Everything here is best-effort: a harvest failure must never turn a
completion into a crash, so each stage degrades to a partial record.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from hermes_cli import kanban_harvest as _harvest
from hermes_cli import kanban_db as _kb

__all__ = ["attach_run_harvest"]

# Outcomes that end a worker's attempt on the workspace. ``spawn_failed`` runs
# also flow through _end_run but no workspace work happened yet; harvesting
# them anyway is harmless (the harvest is facts, not a verdict).
_ALL_OUTCOMES = None  # sentinel: harvest at every _end_run


def _task_row(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT workspace_path, workspace_kind, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _prior_run_head_sha(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Head sha recorded by a PREVIOUS closed run's harvest (or self-report).

    The commits-of-attempt boundary: attempt N lists ``prior_head..HEAD`` so
    retries don't re-credit attempt N-1's commits. Falls back to the worker's
    own ``head_sha`` self-report on older runs, else None (first attempt
    harvests from the merge-base-free full history cap).
    """
    row = conn.execute(
        "SELECT metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    metadata = _kb._json_dict(row["metadata"])
    harvest_meta = metadata.get("harvest")
    if isinstance(harvest_meta, dict):
        head = harvest_meta.get("head_sha")
        if isinstance(head, str) and head.strip():
            return head.strip()
    reported = metadata.get("head_sha")
    if isinstance(reported, str) and reported.strip():
        return reported.strip()
    return None


def attach_run_harvest(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
    *,
    metadata: Optional[dict],
    board: Optional[str] = None,
) -> None:
    """Harvest workspace evidence onto the just-closed ``run_id`` (in-txn helper).

    Merges ``harvest`` + ``agreement`` into the run's metadata. Must be called
    INSIDE the caller's write txn, right after ``_end_run`` returned the run
    id (the run row still exists; the workspace is not yet cleaned up). A
    missing run id (never-claimed task) or any harvest failure is a no-op:
    evidence collection must never break the transition it observes.
    """
    if run_id is None:
        return
    try:
        row = _task_row(conn, task_id)
        if row is None:
            return
        workspace_path = row["workspace_path"]
        if not workspace_path or not str(workspace_path).strip():
            return
        base_sha = _prior_run_head_sha(conn, task_id)
        log_path = _kb.worker_log_path(task_id, board=board)
        harvest = _harvest.harvest_workspace(
            workspace_path, base_sha, log_path=log_path if log_path.exists() else None,
        )
        # Worker self-report comes from the run's own metadata (the worker
        # supplied it at complete time) — not from prior runs.
        self_report = metadata if isinstance(metadata, dict) else {}
        agreement = _harvest.compare_harvest_to_self_report(harvest, self_report)
        _merge_harvest_into_run_metadata(conn, run_id, harvest, agreement)
    except Exception as exc:  # best-effort by contract
        try:
            _kb._log.debug("kanban harvest: run %s on %s: %s", run_id, task_id, exc)
        except Exception:
            pass


def _merge_harvest_into_run_metadata(
    conn: sqlite3.Connection, run_id: int, harvest: dict, agreement: dict,
) -> None:
    """Read-modify-write ``task_runs.metadata`` adding ``harvest``/``agreement``."""
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return
    metadata = _kb._json_dict(row["metadata"])
    metadata["harvest"] = harvest
    metadata["agreement"] = agreement
    conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (_kb._json_or_null(metadata), run_id),
    )
