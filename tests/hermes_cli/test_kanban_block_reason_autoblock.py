"""Blocked cards must carry their current reason as durable task state.

Revision of the behaviour fork PR #99383 proposed and upstream issue #99326
asked for ("Kanban blocked cards can have no authoritative current reason"):
the reason for a block previously lived only in the ``task_events`` payload,
while the ``tasks`` row said nothing. Two concrete failures follow from that:

* A dispatcher auto-block (``_record_task_failure`` flipping the task to
  ``blocked``) records no reason at the moment of the write, so a board
  carrying a non-empty-reason guard trigger aborts the write with
  ``IntegrityError: block reason is required`` and every subsequent
  dispatcher tick dies in reclaim (observed 2026-09-13, 16 ticks lost).
* Humans and tooling reading the card (CLI ``show``, dashboard, worker
  ``kanban_show``) cannot answer "why is this blocked?" without replaying
  events and guessing.

Contract pinned here, end to end:

1. Schema: fresh boards create ``tasks.block_reason`` plus three triggers
   (insert/update require a non-empty reason for ``blocked``; leaving
   ``blocked`` clears it). A DB that already carries the fork's column and
   triggers is re-ensured idempotently and works.
2. Every blocked-write path stamps the column:
   ``block_task`` (missing/blank reason → ValueError BEFORE any SQL),
   ``create_task(initial_status="blocked")`` (reason required), the
   dispatcher auto-block in ``_record_task_failure`` (both the
   release-claim/spawn path and the timeout/crash path), with reasons
   built by one pure helper. The triggers then make a reasonless
   blocked row impossible even for future write paths.
3. Read side: ``Task`` exposes ``block_reason``; unblock clears it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

TRIGGER_NAMES = (
    "trg_tasks_block_reason_insert",
    "trg_tasks_block_reason_update",
    "trg_tasks_block_reason_clear",
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with a fresh kanban DB (schema incl. triggers)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _triggers(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall()
    return {r["name"] for r in rows}


def _reason(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT block_reason FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return None if row is None else row["block_reason"]


def _blocked_row(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT status, block_reason FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row is not None, task_id
    return row


# ---------------------------------------------------------------------------
# 1. Schema: column + triggers on fresh DBs, idempotent on fork-shaped DBs
# ---------------------------------------------------------------------------


def test_fresh_db_has_block_reason_column_and_triggers(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "block_reason" in cols
        names = _triggers(conn)
        assert set(TRIGGER_NAMES) <= names


def test_ensure_schema_is_idempotent_on_fresh_db(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        kb.init_db()  # re-runs the migration pass over a live DB
        assert set(TRIGGER_NAMES) <= _triggers(conn)


def test_db_preseeded_with_fork_column_and_triggers_reensures_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy DB that already carries the fork's column + triggers must be
    re-ensured without error, and blocked writes keep working on it."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect_closing() as conn:
        # Simulate the fork's schema: additive column (fresh from fork DDL)
        # plus its three triggers, created exactly as the fork shipped them.
        conn.execute("ALTER TABLE tasks ADD COLUMN block_reason TEXT")
        for ddl in (
            """
            CREATE TRIGGER trg_tasks_block_reason_insert
            BEFORE INSERT ON tasks
            WHEN NEW.status = 'blocked'
             AND TRIM(COALESCE(NEW.block_reason, '')) = ''
            BEGIN
                SELECT RAISE(ABORT, 'block reason is required');
            END
            """,
            """
            CREATE TRIGGER trg_tasks_block_reason_update
            BEFORE UPDATE ON tasks
            WHEN NEW.status = 'blocked'
             AND TRIM(COALESCE(NEW.block_reason, '')) = ''
            BEGIN
                SELECT RAISE(ABORT, 'block reason is required');
            END
            """,
            """
            CREATE TRIGGER trg_tasks_block_reason_clear
            AFTER UPDATE OF status ON tasks
            WHEN OLD.status = 'blocked'
             AND NEW.status != 'blocked'
             AND NEW.block_reason IS NOT NULL
            BEGIN
                UPDATE tasks SET block_reason = NULL WHERE id = NEW.id;
            END
            """,
        ):
            conn.execute(ddl)
        # Set-up write proves the pre-seeded triggers really fire: a
        # reasonless blocked UPDATE must abort on this very connection.
        tid = kb.create_task(conn, title="legacy board task")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,)
            )

        # Re-ensure over the live fork-shaped schema must not error…
        kb.init_db()
        # …must keep exactly one copy of each trigger (IF NOT EXISTS)…
        n_triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN (?, ?, ?)",
            TRIGGER_NAMES,
        ).fetchone()[0]
        assert n_triggers == 3, "expected exactly the three guard triggers"
        # …and blocked writes on the re-ensured DB still work end to end.
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="still works after re-ensure")
        assert _blocked_row(conn, tid)["status"] == "blocked"
        assert _reason(conn, tid) == "still works after re-ensure"


def test_trigger_refuses_reasonless_blocked_update(kanban_home: Path) -> None:
    """Tonight's incident shape: the DB guard aborts any reasonless blocked
    UPDATE — the write every future code path will attempt."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="guard proof")
        with pytest.raises(sqlite3.IntegrityError, match="block reason is required"):
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,)
            )
        assert conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["status"] != "blocked"


def test_trigger_clears_reason_when_leaving_blocked(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="clear on exit")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="waiting on human")
        assert _reason(conn, tid) == "waiting on human"
        assert kb.unblock_task(conn, tid)
        assert _reason(conn, tid) is None


# ---------------------------------------------------------------------------
# 2. block_task: the manual path
# ---------------------------------------------------------------------------


def test_block_task_stamps_column_and_keeps_event_payload(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="manual block")
        kb.claim_task(conn, tid)
        run = kb.get_task(conn, tid)
        assert run is not None
        assert kb.block_task(
            conn, tid,
            reason="review-required: verify the ACL change",
            expected_run_id=run.current_run_id,
        )
        row = _blocked_row(conn, tid)
        assert row["status"] == "blocked"
        assert row["block_reason"] == "review-required: verify the ACL change"
        # The event payload still carries the reason (immutable history).
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "blocked" in kinds


def test_block_task_without_reason_raises_value_error_and_leaves_row(
    kanban_home: Path,
) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="unreasoned block")
        kb.claim_task(conn, tid)
        with pytest.raises(ValueError, match="reason"):
            kb.block_task(conn, tid, reason=None)
        with pytest.raises(ValueError, match="reason"):
            kb.block_task(conn, tid, reason="   ")  # blank after strip
        row = _blocked_row(conn, tid)
        assert row["status"] == "running"
        assert row["block_reason"] is None


# ---------------------------------------------------------------------------
# 3. create_task(initial_status="blocked")
# ---------------------------------------------------------------------------


def test_create_task_blocked_requires_reason(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="reason"):
            kb.create_task(conn, title="ops card", initial_status="blocked")
        assert conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"] == 0


def test_create_task_blocked_stamps_reason(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="ops card", initial_status="blocked",
            block_reason="R3 gate: needs immediate human ops",
        )
        row = _blocked_row(conn, tid)
        assert row["status"] == "blocked"
        assert row["block_reason"] == "R3 gate: needs immediate human ops"
        # Non-blocked creation is untouched.
        other = kb.create_task(conn, title="plain card")
        assert _reason(conn, other) is None


# ---------------------------------------------------------------------------
# 4. The dispatcher auto-block (tonight's wedge)
# ---------------------------------------------------------------------------


def _seed_running_task(conn: sqlite3.Connection, *, failures: int = 0) -> str:
    tid = kb.create_task(conn, title="auto-block subject")
    kb.claim_task(conn, tid)
    if failures:
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
            (failures, tid),
        )
    return tid


def test_auto_block_spawn_path_stamps_reason_not_integrityerror(
    kanban_home: Path,
) -> None:
    """The exact 19:14 wedge: spawn-failure auto-block on a trigger-guarded
    board. Must not raise; must land blocked with an auto-blocked reason."""
    with kbc.connect_closing() as conn:
        tid = _seed_running_task(conn)
        tripped = kbd._record_task_failure(
            conn, tid, "spawn: profile venv missing",
            outcome="spawn_failed", failure_limit=1,
            release_claim=True, end_run=True,
        )
        assert tripped is True
        row = _blocked_row(conn, tid)
        assert row["status"] == "blocked"
        assert (row["block_reason"] or "").startswith("auto-blocked after 1 spawn_failed failure(s)")
        assert "profile venv missing" in row["block_reason"]


def test_auto_block_timeout_path_stamps_reason(kanban_home: Path) -> None:
    """Crash/timeout path (release_claim=False, end_run=False): the caller
    already restored the phase; the trip write must still stamp a reason."""
    with kbc.connect_closing() as conn:
        tid = _seed_running_task(conn)
        tripped = kbd._record_task_failure(
            conn, tid, "worker died: SIGKILL",
            outcome="crashed", failure_limit=1,
        )
        assert tripped is True
        row = _blocked_row(conn, tid)
        assert row["status"] == "blocked"
        assert (row["block_reason"] or "").startswith("auto-blocked after 1 crashed failure(s)")
        assert "worker died: SIGKILL" in row["block_reason"]


def test_auto_block_below_limit_still_ready_and_reasonless(kanban_home: Path) -> None:
    """Below the limit the task returns to ready — no reason, no trigger trip."""
    with kbc.connect_closing() as conn:
        tid = _seed_running_task(conn)
        tripped = kbd._record_task_failure(
            conn, tid, "transient spawn error",
            outcome="spawn_failed", failure_limit=2,
            release_claim=True, end_run=True,
        )
        assert tripped is False
        row = _blocked_row(conn, tid)
        assert row["status"] == "ready"
        assert row["block_reason"] is None


def test_auto_block_reason_helper_pure_contract() -> None:
    """One pure helper builds every auto-block reason: outcome + failure count
    + bounded error excerpt."""
    reason = kbd.auto_block_reason("crashed", 3, "Traceback ... OSError")
    assert reason.startswith("auto-blocked after 3 crashed failure(s)")
    assert "OSError" in reason
    # Long errors are bounded (first 200 chars of the error).
    long_error = "x" * 5_000
    assert len(kbd.auto_block_reason("spawn_failed", 2, long_error)) <= 500
    # Blank error still yields a complete, non-empty reason.
    blank = kbd.auto_block_reason("timed_out", 1, "")
    assert blank.startswith("auto-blocked after 1 timed_out failure(s)")
    assert len(blank.strip()) > 0
    # Random-ish inputs: always non-empty, always bounded.
    import random

    rng = random.Random(1234)
    for _ in range(50):
        outcome = rng.choice(["crashed", "timed_out", "spawn_failed", "x" * 30])
        failures = rng.randint(0, 10_000)
        error = "".join(rng.choice("abc \nξθ") for _ in range(rng.randint(0, 900)))
        r = kbd.auto_block_reason(outcome, failures, error)
        assert 0 < len(r) <= 500
        assert f"after {failures} " in r


# ---------------------------------------------------------------------------
# 5. Read side
# ---------------------------------------------------------------------------


def test_task_object_exposes_block_reason(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="read side")
        kb.claim_task(conn, tid)
        kb.block_task(conn, tid, reason="needs a human decision")
        task = kb.get_task(conn, tid)
        assert task is not None and task.block_reason == "needs a human decision"


def test_swarm_root_create_with_reason_still_activates(kanban_home: Path) -> None:
    """Swarm graph creation parks its root in blocked inside one txn; it must
    stamp a construction reason and still activate cleanly."""
    from hermes_cli import kanban_swarm as ks

    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn,
            goal="ship the release",
            workers=[ks.SwarmWorkerSpec(
                profile="codex", title="w1", body="do the work",
            )],
            verifier_assignee="codex",
            synthesizer_assignee="codex",
            created_by="test",
        )
        root = kb.get_task(conn, created.root_id)
        assert root is not None
        assert root.status == "done"  # root activates (completes) immediately
        assert _reason(conn, created.root_id) is None  # cleared on leaving blocked
