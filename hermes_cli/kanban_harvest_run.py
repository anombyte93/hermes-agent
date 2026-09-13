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
import time
from typing import Any, Optional

from hermes_cli import kanban_harvest as _harvest
from hermes_cli import kanban_db as _kb

__all__ = ["attach_run_harvest"]


def _task_row(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT workspace_path, workspace_kind, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def attempt_base_head_of_run(conn: sqlite3.Connection, run_id: int) -> Optional[str]:
    """``workspace_head_at_claim`` stamped on a run at claim time, if any.

    Read from the run row; used by ``_end_run`` BEFORE the closing UPDATE
    replaces the metadata with the worker's handoff fields.
    """
    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
    ).fetchone()
    if row is None:
        return None
    meta = _kb._json_dict(row["metadata"])
    stamped = meta.get("workspace_head_at_claim")
    return stamped.strip() if isinstance(stamped, str) and stamped.strip() else None


def _attempt_base_head(
    conn: sqlite3.Connection, task_id: str, run_id: int,
    stamped: Optional[str] = None,
) -> Optional[str]:
    """The commit boundary for this attempt, most precise first.

    1. ``stamped`` — the ``workspace_head_at_claim`` snapshotted by
       ``_end_run`` before the worker's metadata replaced it.
    2. The same key read from the run row (crash path: no replacement).
    3. The prior closed run's harvest head / self-reported head.
    """
    if isinstance(stamped, str) and stamped.strip():
        return stamped.strip()
    own = attempt_base_head_of_run(conn, run_id)
    if own:
        return own
    return _prior_run_head_sha(conn, task_id, exclude_run_id=run_id)


def _prior_run_head_sha(
    conn: sqlite3.Connection, task_id: str, exclude_run_id: Optional[int],
) -> Optional[str]:
    """Head sha recorded by a PREVIOUS closed run's harvest (or self-report).

    The commits-of-attempt boundary: attempt N lists ``prior_head..HEAD`` so
    retries don't re-credit attempt N-1's commits. ``exclude_run_id`` is the
    run being closed right now — its own row already carries ``ended_at`` at
    this point and must not become its own base. Falls back to the worker's
    own ``head_sha`` self-report on older runs, else None.
    """
    row = conn.execute(
        "SELECT metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL AND id != ? "
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id, exclude_run_id),
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
    attempt_base: Optional[str] = None,
    board: Optional[str] = None,
) -> None:
    """Harvest workspace evidence onto the just-closed ``run_id`` (in-txn helper).

    Merges ``harvest`` + ``agreement`` into the run's metadata. Must be called
    INSIDE the caller's write txn, right after ``_end_run`` returned the run
    id (the run row still exists; the workspace is not yet cleaned up). A
    missing run id (never-claimed task) or any harvest failure is a no-op:
    evidence collection must never break the transition it observes.

    A ``mismatch`` verdict is surfaced as a dispatcher comment in the same
    txn, so the next worker/human sees the discrepancy without digging
    through run metadata.
    """
    if run_id is None:
        return
    try:
        row = _task_row(conn, task_id)
        if row is None:
            return
        workspace_path = row["workspace_path"]
        if not workspace_path or not str(workspace_path).strip():
            # No workspace at all (scratch never resolved, spawn failed
            # early): still stamp the key so every closed run carries a
            # harvest record, visibly empty rather than absent.
            _merge_harvest_into_run_metadata(
                conn, run_id,
                {"workspace": "none", "commits": [], "diffstat": {},
                 "dirty": 0, "head_sha": None, "branch": None, "test_counts": {}},
                {},
            )
            return
        base_sha = _attempt_base_head(conn, task_id, run_id, stamped=attempt_base)
        log_path = _kb.worker_log_path(task_id, board=board)
        harvest = _harvest.harvest_workspace(
            workspace_path, base_sha, log_path=log_path if log_path.exists() else None,
        )
        # Worker self-report comes from the run's own metadata (the worker
        # supplied it at complete time) — not from prior runs.
        self_report = metadata if isinstance(metadata, dict) else {}
        agreement = _harvest.compare_harvest_to_self_report(harvest, self_report)
        _merge_harvest_into_run_metadata(conn, run_id, harvest, agreement)
        if _harvest.mismatch_fields(agreement):
            _kb._insert_comment(
                conn, task_id, "dispatcher",
                _harvest.format_mismatch_comment(task_id, run_id, harvest, agreement),
                int(time.time()),
            )
    except Exception as exc:  # best-effort by contract
        try:
            _kb._log.debug("kanban harvest: run %s on %s: %s", run_id, task_id, exc)
        except Exception:
            pass


def _merge_harvest_into_run_metadata(
    conn: sqlite3.Connection, run_id: int, harvest: dict, agreement: dict,
) -> None:
    """Read-modify-write ``task_runs.metadata`` adding ``harvest``/``agreement``.

    Preserves keys already on the run — ``workspace_head_at_claim`` (the
    boundary stamp) and the worker's own handoff fields — by merging into the
    row's CURRENT metadata rather than replacing it.
    """
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
