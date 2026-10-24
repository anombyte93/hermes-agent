"""Review-hold tests: the supported durable ``review -> blocked`` hold.

``block_task`` historically only accepted ``running``/``ready`` as blockable
source statuses. A card parked in the review lane (``request_review`` moved it
there and closed the implementer run with ``outcome=review_requested``) had no
supported way to be durably held after parent verification fails: the only
choices were leaving it in ``review`` (where the review dispatcher would claim
it) or corrupting/rewriting its evidence. This file pins the new contract:

* ``review`` -> ``blocked`` records ``reason`` + ``source_status=review`` and
  clears only claim state. The implementer's ended ``review_requested`` run and
  every prior event/result/PR-metadata byte are preserved untouched.
* An explicit ``unblock_task`` restores ``review`` (not ``ready``), so the hold
  is reversible without losing the review-lane provenance.
* A stale ``expected_run_id`` is refused (the CAS guard remains).
* A non-blockable status is refused (``block_task`` returns False).
* A held card is invisible to the review dispatcher: ``claim_review_task``
  cannot admit it, and a repeated hold is idempotent (no duplicate events).
"""

from __future__ import annotations

import json
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


def _park_in_review(conn, title: str = "hold me", assignee: str = "worker") -> str:
    """Create -> claim -> request_review, leaving the card parked in ``review``."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(
        conn, tid, summary="impl done", expected_run_id=claimed.current_run_id,
    ) is True
    assert kb.get_task(conn, tid).status == "review"
    assert kb.get_task(conn, tid).current_run_id is None
    return tid


def _runs(conn, tid):
    return conn.execute(
        "SELECT id, status, outcome, summary, ended_at FROM task_runs "
        "WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


# ---------------------------------------------------------------------------
# Happy path: review -> blocked, with source_status recorded
# ---------------------------------------------------------------------------


def test_review_hold_transitions_to_blocked_and_records_source_status(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)

        ok = kb.block_task(conn, tid, reason="verification failed", kind="needs_input")
        assert ok is True

        row = conn.execute(
            "SELECT status, block_kind, block_reason, block_recurrences, "
            "claim_lock, claim_expires, worker_pid, current_run_id "
            "FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["block_kind"] == "needs_input"
        assert row["block_reason"] == "verification failed"
        # Only claim state cleared; nothing else touched.
        assert row["claim_lock"] is None
        assert row["claim_expires"] is None
        assert row["worker_pid"] is None
        assert row["current_run_id"] is None

        blocked = _events(conn, tid, kind="blocked")
        assert len(blocked) == 1
        assert blocked[0][1]["source_status"] == "review"
        assert blocked[0][1]["reason"] == "verification failed"


def test_explicit_unblock_restores_review_not_ready(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"
        # The review dispatcher can claim it again after the hold is released.
        assert kb.claim_review_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# CAS / stale guard + invalid status
# ---------------------------------------------------------------------------


def test_review_hold_stale_expected_run_id_is_refused(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)
        # A card parked in review has no current run, so any expected_run_id
        # is stale and the CAS guard must refuse it.
        ok = kb.block_task(conn, tid, reason="hold", expected_run_id=999)
        assert ok is False
        assert kb.get_task(conn, tid).status == "review"
        assert _events(conn, tid, kind="blocked") == []


def test_block_task_refuses_non_blockable_status(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already done", assignee="worker")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, result="done") is True
        assert kb.get_task(conn, tid).status == "done"

        assert kb.block_task(conn, tid, reason="hold") is False
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# History preservation
# ---------------------------------------------------------------------------


def test_review_hold_preserves_ended_run_history(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)
        before = _runs(conn, tid)
        # Exactly the implementer's ended review_requested run.
        assert len(before) == 1
        assert before[0]["outcome"] == "review_requested"
        before_ended_at = before[0]["ended_at"]
        before_summary = before[0]["summary"]

        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True

        after = _runs(conn, tid)
        # No run was synthesized or rewritten: same single run, same outcome,
        # same ended_at, summary untouched.
        assert [r["id"] for r in after] == [r["id"] for r in before]
        assert after[0]["outcome"] == "review_requested"
        assert after[0]["ended_at"] == before_ended_at
        assert after[0]["summary"] == before_summary


# ---------------------------------------------------------------------------
# Idempotency + dispatcher invisibility
# ---------------------------------------------------------------------------


def test_repeated_hold_is_idempotent_no_duplicate_events(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True
        # A second hold on the already-held card is a no-op under the existing
        # block contract (returns False, no new events).
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is False
        assert len(_events(conn, tid, kind="blocked")) == 1


def test_held_review_fixture_cannot_be_admitted_by_review_dispatcher(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        tid = _park_in_review(conn)
        assert kb.block_task(conn, tid, reason="hold", kind="needs_input") is True
        assert kb.get_task(conn, tid).status == "blocked"

        # The review lane can no longer claim the held card.
        assert kb.claim_review_task(conn, tid) is None
        assert kb.get_task(conn, tid).status == "blocked"
