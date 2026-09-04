"""Timeout circuit-breaker must produce a valid typed blocked state.

Live failure (issue #18, boards wow-vision 2026-09-03 and
stash-hmv-automation t_f7f1a35e 2026-09-04): ``enforce_max_runtime``
committed the ``timed_out`` transition and returned the card to
``ready``, then ``_record_task_failure`` tripped the per-task breaker
(``max_retries: 1``) and tried ``status='blocked'`` WITHOUT setting
``block_reason``. The kernel trigger ``trg_tasks_block_reason_update``
raised ``sqlite3.IntegrityError: block reason is required``, dispatch
crashed mid-tick, and the card was stranded ``ready`` with a durable
``timed_out`` event but no breaker state.

These tests pin the fixed contract: the breaker trip always writes a
non-empty human-readable reason derived from the actual failure, no
IntegrityError escapes, and manual blocking with a supplied reason and
typed kind persists both.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _spawn_overrun_task(conn, **create_kwargs) -> str:
    """Create + claim a task whose active run started 30s ago with a 1s cap."""
    tid = kb.create_task(
        conn, title="overrun job", assignee="worker",
        max_runtime_seconds=1, **create_kwargs,
    )
    kb.claim_task(conn, tid)
    kb._set_worker_pid(conn, tid, os.getpid())
    old_started = int(time.time()) - 30
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )
    return tid


def test_timeout_at_final_retry_blocks_with_reason(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out task at its per-task failure limit (max_retries=1) must
    land in ``blocked`` with a non-empty reason — not crash with
    ``sqlite3.IntegrityError: block reason is required`` and strand the
    card in ``ready``."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    conn = kb.connect()
    try:
        tid = _spawn_overrun_task(conn, max_retries=1)

        # The live defect raised sqlite3.IntegrityError from inside
        # enforce_max_runtime's _record_task_failure call.
        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda *_: None)
        assert tid in timed_out

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"breaker at max_retries=1 must block, got {task.status}"
        )
        assert (task.block_reason or "").strip(), (
            "blocked card must carry a non-empty reason"
        )
        # Reason must be traceable to the actual failure.
        assert "timed_out" in task.block_reason
        assert task.worker_pid is None and task.claim_lock is None

        events = kb.list_events(conn, tid)
        kinds = [e.kind for e in events]
        assert kinds.count("timed_out") == 1
        assert kinds.count("gave_up") == 1
    finally:
        conn.close()


def test_timeout_below_retry_limit_returns_ready_without_block(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: a timeout below the failure limit retries via ``ready`` and
    never touches blocked state (no reason required, no trigger fired)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)

    conn = kb.connect()
    try:
        tid = _spawn_overrun_task(conn, max_retries=5)
        timed_out = kb.enforce_max_runtime(conn, signal_fn=lambda *_: None)
        assert tid in timed_out
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.block_reason is None
    finally:
        conn.close()


def test_manual_block_with_reason_and_kind_persists(kanban_home: Path) -> None:
    """Manual ``kanban block`` with a non-empty reason and typed kind must
    persist both (third live reproduction on issue #18 hit the trigger
    even with a reason supplied)."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="needs human", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.block_task(
            conn, tid,
            reason="waiting on operator credentials",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_reason == "waiting on operator credentials"
        assert task.block_kind == "needs_input"
    finally:
        conn.close()
