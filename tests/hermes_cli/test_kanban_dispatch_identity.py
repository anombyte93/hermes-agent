"""Regression tests for hermes-agent issue #5, second half — dispatch IDENTITY.

The workspace-lease slice fixed the CONCURRENCY half of issue #5: one
dispatcher pass can no longer spawn two workers into one canonical
workspace. It deliberately did not fix the IDENTITY half — *which* card a
pass selects — because with only one spawn there is no concurrent holder
for a lease to reject.

Two live reproductions, both recorded on issue #5:

1. 2026-09-02 04:15 AWST, board ``my-agents-overnight-20260902``:
   ``hermes kanban dispatch --max 1 --json`` did not claim the sole
   ``ready`` integration card. It re-claimed an already-PASS ``review``
   card as a new run and spawned a worker into that review worktree.
   The operator had no exact-card route around it.
2. 2026-09-02 05:24 AWST, board ``atlas-launcher``: a task had completed
   implementation run 9 with outcome ``review_requested`` and a candidate
   commit. Source status was ``review`` with no active worker. The
   dispatcher re-claimed the SAME implementation task as run 10 (the
   claim event records ``source_status: review``) into the same
   implementation workspace — moving the audit target beneath the
   reviewer.

The contract pinned down here:

* ``dispatch_once(only_task=...)`` selects EXACTLY that card. Releasing
  one card can never select another, in either lane.
* A ``review``-status card whose latest ``review_requested`` event names
  the current assignee as the implementer is NOT dispatchable: a review
  run must be an explicitly assigned reviewer transition.
* A review whose reviewer run already ended with a terminal handoff is
  spent — it needs the manual completion ceremony, not a re-spawn.
* Crash/timeout reclaim retries of a reviewer run are NOT affected.
* A legitimately assigned reviewer card still dispatches autonomously.
* The existing review-lane reservation, caps and workspace lease are
  untouched.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a kanban DB and real-looking profiles.

    Deliberately does NOT purge ``hermes_cli`` from ``sys.modules`` the way
    some older kanban fixtures do: that purge re-imports the module under a
    new identity and detaches observer registrations, which makes unrelated
    test files fail depending on collection order.
    """
    home = tmp_path / ".hermes"
    for prof in ("impl", "reviewer", "other", "default"):
        (home / "profiles" / prof).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db

    kanban_db.init_db()
    with kanban_db.connect_closing() as conn:
        kanban_db.create_board(slug="default", name="Test")
        del conn
    yield kanban_db


def _fake_spawn(task, workspace, **kwargs):
    return 4242


def _mk(kb, conn, title, *, assignee="impl", workspace=None, status=None):
    tid = kb.create_task(
        conn,
        title=title,
        assignee=assignee,
        workspace_kind="dir" if workspace else "scratch",
        workspace_path=str(workspace) if workspace else None,
    )
    if status is not None:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (status, tid),
            )
    return tid


def _status(conn, tid):
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]


def _spawned_ids(res):
    return [tid for (tid, _who, _ws) in res.spawned]


def _handoff_to_review(kb, conn, tid, *, reviewer=None):
    """Drive the real implementation -> review handoff for ``tid``.

    Claims the card from ``ready`` (so a genuine implementation run exists),
    then calls ``request_review`` with that run's id. This reproduces the
    exact shape of live reproduction 2: a ``review``-status card whose last
    run ended ``review_requested``.
    """
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, f"could not claim {tid} for the handoff"
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["current_run_id"]
    ok = kb.request_review(
        conn,
        tid,
        summary="candidate ready",
        reviewer=reviewer,
        expected_run_id=run_id,
    )
    assert ok, f"request_review failed for {tid}"
    return run_id


# --------------------------------------------------------------------------
# 1. Exact-card dispatch selector.
# --------------------------------------------------------------------------


def test_targeted_dispatch_of_ready_card_never_spawns_the_review_card(kb):
    """Live reproduction 1: releasing the ready card must not pick the review one.

    Pre-fix, an untargeted pass on this board selects the review card (it is
    enumerated and spawnable) even though the operator wants the ready
    integration card. There was no exact-card route at all.
    """
    with kb.connect_closing() as conn:
        review_id = _mk(kb, conn, "already-reviewed candidate", status="review")
        ready_id = _mk(kb, conn, "integration card", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, only_task=ready_id, max_spawn=1,
        )

    assert _spawned_ids(res) == [ready_id], (
        f"targeted dispatch of {ready_id} spawned {res.spawned}"
    )
    with kb.connect_closing() as conn:
        assert _status(conn, ready_id) == "running"
        assert _status(conn, review_id) == "review"
        assert conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (review_id,),
        ).fetchone()["claim_lock"] is None


def test_targeted_dispatch_can_select_a_review_lane_card(kb):
    """The selector is lane-agnostic: it can target a genuine reviewer card."""
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl", assignee="impl")
        _handoff_to_review(kb, conn, impl_id, reviewer="reviewer")
        ready_id = _mk(kb, conn, "unrelated ready", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, only_task=impl_id,
        )

    assert _spawned_ids(res) == [impl_id]
    with kb.connect_closing() as conn:
        assert _status(conn, ready_id) == "ready"


def test_targeted_dispatch_of_unknown_id_spawns_nothing(kb):
    """A typo'd id must spawn nothing rather than falling back to any card."""
    with kb.connect_closing() as conn:
        _mk(kb, conn, "ready a", assignee="impl")
        _mk(kb, conn, "ready b", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, only_task="t_does_not_exist",
        )

    assert res.spawned == []


def test_targeted_dry_run_reports_only_the_targeted_card(kb):
    """Dry run must answer the operator's question about ONE card."""
    with kb.connect_closing() as conn:
        _mk(kb, conn, "decoy", assignee="impl")
        target = _mk(kb, conn, "target", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, only_task=target, dry_run=True,
        )

    assert _spawned_ids(res) == [target]
    with kb.connect_closing() as conn:
        assert _status(conn, target) == "ready"


def test_untargeted_dispatch_is_unchanged(kb):
    """Omitting the selector keeps the historical whole-board behaviour."""
    with kb.connect_closing() as conn:
        a = _mk(kb, conn, "ready a", assignee="impl")
        b = _mk(kb, conn, "ready b", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert sorted(_spawned_ids(res)) == sorted([a, b])


# --------------------------------------------------------------------------
# 2. Accepted-review terminality: the implementer is not re-entered.
# --------------------------------------------------------------------------


def test_review_card_is_not_reclaimed_by_its_own_implementer(kb, tmp_path):
    """Live reproduction 2: implementation card in ``review`` re-claimed as a new run.

    The card's last run ended ``review_requested`` with a candidate commit
    and the assignee is still the implementer. Re-entering that profile in
    the same workspace moves the audit target beneath the reviewer.
    """
    ws = tmp_path / "impl-worktree"
    ws.mkdir()
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl awaiting review", assignee="impl", workspace=ws)
        # No reviewer supplied: the card stays assigned to the implementer,
        # exactly as on the atlas-launcher board.
        _handoff_to_review(kb, conn, impl_id)
        assert _status(conn, impl_id) == "review"
        assert conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (impl_id,),
        ).fetchone()["assignee"] == "impl"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.spawned == [], (
        f"the implementer was re-entered into its own review card: {res.spawned}"
    )
    assert [tid for (tid, _r) in res.skipped_review_not_dispatchable] == [impl_id]
    with kb.connect_closing() as conn:
        assert _status(conn, impl_id) == "review"
        assert conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (impl_id,),
        ).fetchone()["claim_lock"] is None


def test_targeted_dispatch_cannot_bypass_implementer_reentry_gate(kb):
    """The selector chooses a card; it does not license an unsafe transition."""
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl awaiting review", assignee="impl")
        _handoff_to_review(kb, conn, impl_id)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_fake_spawn, only_task=impl_id,
        )

    assert res.spawned == []
    assert [tid for (tid, _r) in res.skipped_review_not_dispatchable] == [impl_id]


def test_assigned_reviewer_card_still_dispatches(kb):
    """A genuine reviewer transition must keep dispatching autonomously."""
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl awaiting review", assignee="impl")
        _handoff_to_review(kb, conn, impl_id, reviewer="reviewer")
        assert conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (impl_id,),
        ).fetchone()["assignee"] == "reviewer"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert _spawned_ids(res) == [impl_id]
    assert res.skipped_review_not_dispatchable == []
    with kb.connect_closing() as conn:
        claimed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'claimed' ORDER BY id DESC LIMIT 1",
            (impl_id,),
        ).fetchone()
        assert json.loads(claimed["payload"])["source_status"] == "review"


def test_review_card_without_provenance_still_dispatches(kb):
    """A human-moved review card (no ``review_requested`` event) is untouched.

    The gate keys off durable implementer provenance. Absent it, we cannot
    know the card is being handed back to its own implementer, so we keep
    the historical behaviour rather than silently freezing control-plane
    lanes that move cards into ``review`` by hand.
    """
    with kb.connect_closing() as conn:
        rid = _mk(kb, conn, "human-moved review", assignee="reviewer", status="review")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert _spawned_ids(res) == [rid]


def test_reassigning_a_reviewer_reopens_dispatch(kb):
    """The operator's route out of the gate is naming a reviewer, not a re-spawn."""
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl awaiting review", assignee="impl")
        _handoff_to_review(kb, conn, impl_id)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert res.spawned == []

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = 'reviewer' WHERE id = ?", (impl_id,),
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert _spawned_ids(res) == [impl_id]


# --------------------------------------------------------------------------
# 3. A spent review is not re-dispatched; a crashed one still retries.
# --------------------------------------------------------------------------


def test_spent_reviewer_run_is_not_redispatched(kb):
    """A reviewer run that ended with a terminal handoff must not re-spawn.

    Live reproduction 1's shape: the review had already been performed and
    reported, but the card sat in ``review``. Re-claiming it repeats a
    review that already happened instead of waiting for the completion
    ceremony.
    """
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl", assignee="impl")
        _handoff_to_review(kb, conn, impl_id, reviewer="reviewer")

    # Reviewer run 2: claimed from review, ends by handing the candidate
    # back to review (a terminal handoff, not a crash).
    with kb.connect_closing() as conn:
        review_run = kb.claim_review_task(conn, impl_id)
        assert review_run is not None
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (impl_id,),
        ).fetchone()["current_run_id"]
        assert kb.request_review(
            conn, impl_id, summary="reviewed", expected_run_id=run_id,
        )
        assert _status(conn, impl_id) == "review"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.spawned == []
    assert [tid for (tid, _r) in res.skipped_review_not_dispatchable] == [impl_id]


def test_crashed_reviewer_run_still_retries(kb):
    """Retry gates are not weakened: a crashed reviewer run comes back."""
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl", assignee="impl")
        _handoff_to_review(kb, conn, impl_id, reviewer="reviewer")

    with kb.connect_closing() as conn:
        assert kb.claim_review_task(conn, impl_id) is not None
        # Simulate a crash: the run ends with no terminal handoff outcome
        # and the card is reclaimed back to its review source phase.
        kb.reclaim_task(
            conn, impl_id, reason="crashed",
            signal_fn=lambda *_a, **_k: None,
        )
        assert _status(conn, impl_id) == "review"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert _spawned_ids(res) == [impl_id], (
        "a crashed reviewer run must still be retried"
    )


# --------------------------------------------------------------------------
# 4. Neighbouring guarantees are preserved.
# --------------------------------------------------------------------------


def test_gate_does_not_consume_the_review_lane_reservation(kb):
    """A non-dispatchable review card must not hold a slot back from ready.

    The review-lane reservation exists so a sustained ready backlog cannot
    starve genuine reviews. A card the gate will refuse anyway is not
    genuine review work, so it must not tax ready throughput.
    """
    with kb.connect_closing() as conn:
        impl_id = _mk(kb, conn, "impl awaiting review", assignee="impl")
        _handoff_to_review(kb, conn, impl_id)
        ready_id = _mk(kb, conn, "ready work", assignee="other")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=1)

    assert _spawned_ids(res) == [ready_id]


def test_workspace_lease_still_holds_under_targeted_dispatch(kb, tmp_path):
    """The exclusive workspace lease is unchanged by the selector."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        holder = _mk(kb, conn, "holder", assignee="impl", workspace=shared)
        target = _mk(kb, conn, "target", assignee="other", workspace=shared)

    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_fake_spawn, only_task=holder)
    assert _spawned_ids(first) == [holder]

    with kb.connect_closing() as conn:
        second = kb.dispatch_once(conn, spawn_fn=_fake_spawn, only_task=target)

    assert second.spawned == []
    assert [tid for (tid, _h, _w) in second.skipped_workspace_busy] == [target]
    assert second.skipped_workspace_busy[0][1] == holder


# --------------------------------------------------------------------------
# 5. The operator's CLI surface.
# --------------------------------------------------------------------------


def test_cli_dispatch_task_flag_reaches_dispatch_once(kb, monkeypatch):
    """``hermes kanban dispatch --task <id>`` must pass the selector through.

    Without this wiring the operator-facing half of the fix does not exist:
    the engine can target a card but nobody can ask it to.
    """
    from hermes_cli import kanban as kb_cli

    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)

    # Parse through the REAL parser so the flag's own wiring is exercised,
    # not just _cmd_dispatch's attribute read.
    root = argparse.ArgumentParser(prog="hermes")
    kb_cli.build_parser(root.add_subparsers(dest="cmd"))
    args = root.parse_args(["kanban", "dispatch", "--task", "t_target", "--dry-run"])
    kb_cli._cmd_dispatch(args)

    assert captured.get("only_task") == "t_target"


def test_cli_dispatch_without_task_flag_targets_nothing(kb, monkeypatch):
    """Omitting ``--task`` must leave the selector unset, not empty-string it."""
    from hermes_cli import kanban as kb_cli

    captured: dict = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake_dispatch_once)

    args = argparse.Namespace(
        dry_run=True, max=None, failure_limit=2, json=False, task=None,
    )
    kb_cli._cmd_dispatch(args)

    assert captured.get("only_task") is None

