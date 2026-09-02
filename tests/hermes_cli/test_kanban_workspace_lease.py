"""Regression tests for hermes-agent issue #5 — dispatcher workspace lease.

One dispatcher pass independently walks the ready queue and the review
queue. Both queues can name the SAME directory: the canonical shape is an
implementation card sitting in ``review`` beside a separate reviewer card
pointed at the implementer's worktree. Before the lease, one pass claimed
and spawned both, so two agents edited/committed/tested in one checkout
concurrently — a workspace-integrity defect, not just noisy status.

Live reproduction recorded on issue #5: review card ``t_7e9b53c7`` and
ready card ``t_6e2ef9a9`` were both claimed at Unix time ``1788291806``
against the same resolved workspace; the duplicate was reclaimed 27
seconds later.

The contract pinned down here:

* Across ready AND review, at most one active claim per canonical workspace.
* The invariant holds in either queue order.
* The loser stays unclaimed and reports ``workspace_busy`` naming the
  holder, with no task-body content in the diagnostic.
* Genuinely different workspaces still dispatch concurrently.
* Path aliases (``.``/``..`` segments, trailing slashes, symlinks) resolving
  to one canonical directory are treated as ONE workspace.
* Reclaim/completion releases the workspace for a later dispatch.
* The lease is exclusive — this slice deliberately has no multi-reader
  exception, because ``sdlc-review`` is not a structural read-only boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a kanban DB and two real-looking profiles.

    Deliberately does NOT purge ``hermes_cli`` from ``sys.modules`` the way
    some older kanban fixtures do: that purge re-imports the module under a
    new identity and detaches observer registrations, which makes unrelated
    test files fail depending on collection order.
    """
    home = tmp_path / ".hermes"
    for prof in ("alpha", "beta", "default"):
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
    return 12345


def _mk(kb, conn, title, *, assignee="alpha", workspace=None, status=None):
    """Create a task with a ``dir`` workspace, optionally forced to a status."""
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


def _events(kb, conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else {}
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id",
            (tid, kind),
        )
    ]


def _status(conn, tid):
    return conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (tid,),
    ).fetchone()["status"]


# --------------------------------------------------------------------------
# The core invariant: one workspace, one worker, across both queues.
# --------------------------------------------------------------------------


def test_ready_and_review_same_workspace_spawn_only_one(kb, tmp_path):
    """The live reproduction: one review card + one ready card, one directory.

    Exactly one spawns; the other stays in its source column and reports
    ``workspace_busy`` naming the holder.
    """
    shared = tmp_path / "shared-worktree"
    shared.mkdir()
    with kb.connect_closing() as conn:
        review_id = _mk(
            kb, conn, "impl-awaiting-review", workspace=shared, status="review",
        )
        ready_id = _mk(kb, conn, "independent-reviewer", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1, (
        f"expected exactly one spawn into {shared}, got {res.spawned}"
    )
    assert len(res.skipped_workspace_busy) == 1
    winner = res.spawned[0][0]
    loser, holder, path = res.skipped_workspace_busy[0]
    assert {winner, loser} == {ready_id, review_id}
    assert holder == winner
    assert path == str(shared.resolve())

    with kb.connect_closing() as conn:
        assert _status(conn, winner) == "running"
        # The loser must be left UNCLAIMED in its own source column, not
        # claimed-and-rolled-back into the wrong lane.
        assert _status(conn, loser) in ("ready", "review")
        assert conn.execute(
            "SELECT claim_lock FROM tasks WHERE id = ?", (loser,),
        ).fetchone()["claim_lock"] is None
        payloads = _events(kb, conn, loser, "workspace_busy")
        assert len(payloads) == 1
        assert payloads[0]["holder"] == winner
        assert payloads[0]["workspace"] == str(shared.resolve())


def test_invariant_holds_with_queue_order_reversed(kb, tmp_path):
    """Reverse which lane is favoured by priority: still exactly one spawn."""
    shared = tmp_path / "shared-worktree"
    shared.mkdir()
    with kb.connect_closing() as conn:
        ready_id = _mk(kb, conn, "reviewer", workspace=shared)
        review_id = _mk(
            kb, conn, "impl", workspace=shared, status="review",
        )
        # Push the ready card to the back of its own queue so ordering
        # inside the pass differs from the previous test.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET priority = -10 WHERE id = ?", (ready_id,),
            )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1
    assert {res.spawned[0][0], res.skipped_workspace_busy[0][0]} == {
        ready_id, review_id,
    }


def test_two_ready_tasks_same_workspace_spawn_only_one(kb, tmp_path):
    """The lease is not review-specific: two ready cards collide too."""
    shared = tmp_path / "one-dir"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "a", workspace=shared)
        _mk(kb, conn, "b", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1


# --------------------------------------------------------------------------
# Positive control: distinct workspaces must stay concurrent.
# --------------------------------------------------------------------------


def test_distinct_workspaces_both_spawn(kb, tmp_path):
    """The guard must not serialise unrelated work."""
    a = tmp_path / "ws-a"
    b = tmp_path / "ws-b"
    a.mkdir()
    b.mkdir()
    with kb.connect_closing() as conn:
        id_a = _mk(kb, conn, "a", workspace=a)
        id_b = _mk(kb, conn, "b", workspace=b)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert sorted(s[0] for s in res.spawned) == sorted([id_a, id_b])
    assert res.skipped_workspace_busy == []


def test_scratch_tasks_without_paths_both_spawn(kb):
    """Scratch tasks get per-task directories and must never contend."""
    with kb.connect_closing() as conn:
        id_a = _mk(kb, conn, "a")
        id_b = _mk(kb, conn, "b")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert sorted(s[0] for s in res.spawned) == sorted([id_a, id_b])
    assert res.skipped_workspace_busy == []


# --------------------------------------------------------------------------
# Aliasing: different spellings of one directory are one workspace.
# --------------------------------------------------------------------------


def test_dot_and_dotdot_aliases_are_one_workspace(kb, tmp_path):
    real = tmp_path / "repo" / "wt"
    real.mkdir(parents=True)
    alias = tmp_path / "repo" / "." / "sibling" / ".." / "wt"
    (tmp_path / "repo" / "sibling").mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "canonical", workspace=real)
        _mk(kb, conn, "alias", workspace=alias)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1
    assert res.skipped_workspace_busy[0][2] == str(real.resolve())


@pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="platform has no symlinks",
)
def test_symlink_alias_is_one_workspace(kb, tmp_path):
    real = tmp_path / "real-wt"
    real.mkdir()
    link = tmp_path / "link-wt"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted here")
    with kb.connect_closing() as conn:
        _mk(kb, conn, "via-real", workspace=real)
        _mk(kb, conn, "via-link", workspace=link)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1
    assert res.skipped_workspace_busy[0][2] == str(real.resolve())


def test_trailing_slash_alias_is_one_workspace(kb, tmp_path):
    real = tmp_path / "wt"
    real.mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "plain", workspace=real)
        _mk(kb, conn, "slashed", workspace=str(real) + os.sep)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1


# --------------------------------------------------------------------------
# Release: the lease must not be permanent.
# --------------------------------------------------------------------------


def test_completion_releases_the_workspace(kb, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "a", workspace=shared)
        _mk(kb, conn, "b", workspace=shared)

    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    assert len(first.spawned) == 1
    winner = first.spawned[0][0]
    deferred = first.skipped_workspace_busy[0][0]

    with kb.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', claim_lock = NULL, "
                "worker_pid = NULL WHERE id = ?",
                (winner,),
            )

    with kb.connect_closing() as conn:
        second = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert [s[0] for s in second.spawned] == [deferred]
    assert second.skipped_workspace_busy == []


def test_reclaim_releases_the_workspace(kb, tmp_path):
    """An operator reclaim frees the directory for the deferred card."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "a", workspace=shared)
        _mk(kb, conn, "b", workspace=shared)

    with kb.connect_closing() as conn:
        first = kb.dispatch_once(conn, spawn_fn=_fake_spawn)
    winner = first.spawned[0][0]
    deferred = first.skipped_workspace_busy[0][0]

    with kb.connect_closing() as conn:
        assert kb.reclaim_task(
            conn, winner, reason="test", signal_fn=lambda *a, **k: None,
        )

    with kb.connect_closing() as conn:
        second = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    # Both cards are now eligible again, but still only one may hold the
    # directory at a time.
    assert len(second.spawned) == 1
    assert len(second.skipped_workspace_busy) == 1
    assert {
        second.spawned[0][0], second.skipped_workspace_busy[0][0],
    } == {winner, deferred}


def test_earlier_tick_holder_blocks_a_later_tick(kb, tmp_path):
    """A worker spawned by a PREVIOUS tick still holds its workspace."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        first_id = _mk(kb, conn, "first", workspace=shared)

    with kb.connect_closing() as conn:
        kb.dispatch_once(conn, spawn_fn=_fake_spawn)
        assert _status(conn, first_id) == "running"

    with kb.connect_closing() as conn:
        second_id = _mk(kb, conn, "second", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    assert res.spawned == []
    assert [b[0] for b in res.skipped_workspace_busy] == [second_id]
    assert res.skipped_workspace_busy[0][1] == first_id


# --------------------------------------------------------------------------
# Dry run must model the guard, not report a fantasy.
# --------------------------------------------------------------------------


def test_dry_run_reports_the_lease_and_writes_nothing(kb, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        a = _mk(kb, conn, "a", workspace=shared)
        b = _mk(kb, conn, "b", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert len(res.spawned) == 1
    assert len(res.skipped_workspace_busy) == 1
    with kb.connect_closing() as conn:
        for tid in (a, b):
            assert _status(conn, tid) == "ready"
            assert _events(kb, conn, tid, "workspace_busy") == []


# --------------------------------------------------------------------------
# Diagnostic hygiene.
# --------------------------------------------------------------------------


def test_workspace_busy_event_leaks_no_task_body(kb, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    secret = "SENSITIVE-BODY-MARKER-do-not-leak"
    with kb.connect_closing() as conn:
        _mk(kb, conn, "a", workspace=shared)
        b = kb.create_task(
            conn, title="b", assignee="alpha", body=secret,
            workspace_kind="dir", workspace_path=str(shared),
        )

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    blob = json.dumps(res.skipped_workspace_busy)
    assert secret not in blob
    if res.skipped_workspace_busy[0][0] == b:
        with kb.connect_closing() as conn:
            payloads = _events(kb, conn, b, "workspace_busy")
        assert secret not in json.dumps(payloads)
        assert set(payloads[0]) == {"holder", "workspace"}


# --------------------------------------------------------------------------
# Supervisor's added acceptance case (issue #5 comment, 2026-09-02):
# one accepted-review card + one ready card, budget for only ONE spawn.
#
# The lease alone is NOT sufficient here and this control says so
# explicitly. With a single spawn there is no concurrent holder for a lease
# to reject, so the invariant this slice owns (never TWO workers in one
# directory) is satisfied even when the dispatcher selects the review card.
# WHICH card a budget-1 pass selects is a separate defect — dispatch
# identity / accepted-review terminality — deliberately deferred to its own
# card rather than smuggled into this diff. See the follow-up task.
# --------------------------------------------------------------------------


def test_one_review_plus_one_ready_never_shares_a_workspace(kb, tmp_path):
    """Budget 1, distinct workspaces: whichever card wins, never both."""
    impl_ws = tmp_path / "impl"
    reviewer_ws = tmp_path / "reviewer"
    impl_ws.mkdir()
    reviewer_ws.mkdir()
    with kb.connect_closing() as conn:
        review_id = _mk(
            kb, conn, "accepted review", workspace=impl_ws, status="review",
        )
        ready_id = _mk(kb, conn, "ready integration", workspace=reviewer_ws)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=1)

    assert len(res.spawned) == 1
    assert res.spawned[0][0] in (review_id, ready_id)
    # The lease is not consulted (different directories) and must not
    # manufacture a false workspace_busy.
    assert res.skipped_workspace_busy == []


def test_one_review_plus_one_ready_same_workspace_budget_one(kb, tmp_path):
    """Budget 1 AND one shared directory: still exactly one worker there."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        review_id = _mk(
            kb, conn, "impl in review", workspace=shared, status="review",
        )
        ready_id = _mk(kb, conn, "reviewer", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, max_spawn=1)

    assert len(res.spawned) == 1
    with kb.connect_closing() as conn:
        running = [
            r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE status = 'running'"
            )
        ]
    assert len(running) == 1
    assert running[0] in (review_id, ready_id)


# --------------------------------------------------------------------------
# Operator surfaces: the diagnostic must be reachable, not just internal.
# --------------------------------------------------------------------------


def test_dispatch_result_field_is_a_plain_serialisable_bucket(kb, tmp_path):
    """``skipped_workspace_busy`` must be JSON-safe for the CLI/--json path."""
    shared = tmp_path / "shared"
    shared.mkdir()
    with kb.connect_closing() as conn:
        _mk(kb, conn, "a", workspace=shared)
        _mk(kb, conn, "b", workspace=shared)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn)

    payload = [
        {"task_id": tid, "holder": holder, "workspace": ws}
        for (tid, holder, ws) in res.skipped_workspace_busy
    ]
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload
    assert set(round_tripped[0]) == {"task_id", "holder", "workspace"}


def test_workspace_busy_tick_is_not_reported_idle(kb, monkeypatch):
    """A tick that only deferred on the lease is activity, not idleness.

    The dispatch-tick observer classifies a tick with no listed activity as
    ``idle``; a workspace_busy deferral is a real, operator-visible outcome
    and must be classified ``ok``.
    """
    seen: dict = {}

    monkeypatch.setattr(
        kb, "_kanban_observer_consumed", lambda name: True,
    )
    import hermes_cli.lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle, "invoke_hook",
        lambda name, **kwargs: seen.update({name: kwargs}),
    )

    empty = kb.DispatchResult()
    kb._fire_dispatch_tick_hook(empty, board="default")
    assert seen["on_kanban_dispatch_tick"]["outcome"] == "idle"

    busy = kb.DispatchResult()
    busy.skipped_workspace_busy.append(("t_b", "t_a", "/tmp/ws"))
    kb._fire_dispatch_tick_hook(busy, board="default")
    assert seen["on_kanban_dispatch_tick"]["outcome"] == "ok"


# --------------------------------------------------------------------------
# Canonicalisation helper: direct unit coverage.
# --------------------------------------------------------------------------


def test_canonical_key_collapses_aliases(kb, tmp_path):
    real = tmp_path / "x" / "y"
    real.mkdir(parents=True)
    key = kb._canonical_workspace_key(str(real))
    assert key == kb._canonical_workspace_key(str(real) + os.sep)
    assert key == kb._canonical_workspace_key(str(tmp_path / "x" / "." / "y"))
    assert key == kb._canonical_workspace_key(
        str(tmp_path / "x" / "y" / ".." / "y")
    )


def test_canonical_key_is_none_for_empty_input(kb):
    assert kb._canonical_workspace_key(None) is None
    assert kb._canonical_workspace_key("") is None
    assert kb._canonical_workspace_key("   ") is None
