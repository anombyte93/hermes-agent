"""Regression coverage for parent-owned and independent automatic reviews (#352)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    for name in ("builder", "reviewer"):
        skill = home / "profiles" / name / "skills" / "sdlc-review" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: sdlc-review\ndescription: Review.\n---\nReview.\n")
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: True)
    kb.init_db()
    return home


def handoff(conn, *, reviewer=None, **kwargs):
    tid = kb.create_task(conn, title="finished implementation", assignee="builder", **kwargs)
    impl = kb.claim_task(conn, tid)
    assert impl is not None
    assert kb.request_review(conn, tid, summary="pushed and tested", reviewer=reviewer,
                             expected_run_id=impl.current_run_id)
    return tid, impl.current_run_id


def events(conn, tid, kind):
    return [event for event in kb.list_events(conn, tid) if event.kind == kind]


def test_parent_review_round_trips_cli_tool_and_database(home):
    from tools import kanban_tools as tools

    cli_task = json.loads(kc.run_slash("create 'CLI parent review' --assignee builder --review parent --json"))
    created = json.loads(tools._handle_create({
        "title": "tool parent review", "assignee": "builder", "review": "parent",
    }))
    assert created["ok"] is True
    assert created["review_mode"] == "parent"
    assert tools.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["review"]["enum"] == ["auto", "parent"]
    with kb.connect() as conn:
        for tid in (cli_task["id"], created["task_id"]):
            assert kb.get_task(conn, tid).review_mode == "parent"
            assert kc._task_to_dict(kb.get_task(conn, tid))["review_mode"] == "parent"
            shown = json.loads(tools._handle_show({"task_id": tid}))
            assert shown["task"]["review_mode"] == "parent"
        default = kb.create_task(conn, title="default")
        assert kb.get_task(conn, default).review_mode == "auto"
        with pytest.raises(ValueError, match="review"):
            kb.create_task(conn, title="invalid", review_mode="typo")


def test_parent_review_is_idle_across_ticks_and_does_not_reserve_capacity(home):
    def spawn(task, workspace):
        assert task.title == "ready work"

    with kb.connect() as conn:
        tid, run_id = handoff(conn, review_mode="parent", reviewer="reviewer")
        before = kb.list_runs(conn, tid)
        ready = kb.create_task(conn, title="ready work", assignee="builder")
        for _ in range(3):
            result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
            assert not result.crashed and not result.auto_blocked
            assert tid not in [item[0] for item in result.spawned]
            task = kb.get_task(conn, tid)
            assert (task.status, task.current_run_id, task.claim_lock, task.worker_pid) == ("review", None, None, None)
            assert task.consecutive_failures == 0
            assert kb.list_runs(conn, tid) == before
        assert kb.get_task(conn, ready).status == "running"
        assert kb.get_run(conn, run_id).outcome == "review_requested"
        assert kb.complete_task(conn, tid, summary="parent approved")
        assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("explicit", [False, True])
def test_author_is_never_automatically_claimed_even_with_skill(home, explicit):
    if not explicit:
        # No other eligible reviewer is installed. Do not create a failed run.
        (home / "profiles" / "reviewer" / "skills" / "sdlc-review" / "SKILL.md").unlink()
    with kb.connect() as conn:
        tid, _ = handoff(conn, reviewer="builder" if explicit else None)
        before = kb.list_runs(conn, tid)
        preview_events = kb.list_events(conn, tid)
        preview = kb.dispatch_once(conn, dry_run=True)
        assert not preview.spawned
        assert kb.list_events(conn, tid) == preview_events
        for _ in range(3):
            result = kb.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("self-review spawned"))
            assert not result.spawned and not result.crashed and not result.auto_blocked
            assert kb.get_task(conn, tid).status == "review"
            assert kb.list_runs(conn, tid) == before
        notices = events(conn, tid, "review_unavailable")
        assert len(notices) == 1
        assert "builder" in notices[0].payload["reason"]
        assert "author" in notices[0].payload["reason"].lower()
        from hermes_cli import kanban_diagnostics as kd

        diags = kd.compute_task_diagnostics(
            kb.get_task(conn, tid), kb.list_events(conn, tid), kb.list_runs(conn, tid)
        )
        assert any(d.kind == "review_unavailable" and "author" in d.detail for d in diags)
        skill = home / "profiles" / "reviewer" / "skills" / "sdlc-review" / "SKILL.md"
        skill.write_text("---\nname: sdlc-review\n---\nReview.\n")
        assert kb.assign_task(conn, tid, "reviewer")
        resumed = kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        assert [(item[0], item[1]) for item in resumed.spawned] == [(tid, "reviewer")]
        assert kb.latest_run(conn, tid).profile == "reviewer"


def test_author_check_uses_immutable_run_not_reassigned_handoff(home):
    with kb.connect() as conn:
        tid, _ = handoff(conn, reviewer="builder")
        # Legacy/malformed handoff provenance must not override the run author.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE task_id = ? "
                "AND kind = 'review_requested'",
                (json.dumps({"implementer": "reviewer", "reviewer": "builder"}), tid),
            )
        result = kb.dispatch_once(conn, dry_run=True)
        assert not result.spawned
        assert kb.get_task(conn, tid).status == "review"


def test_independent_reviewer_runs_and_changes_return_to_author(home):
    with kb.connect() as conn:
        tid, _ = handoff(conn, reviewer="reviewer")
        result = kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        assert [(item[0], item[1]) for item in result.spawned] == [(tid, "reviewer")]
        review = kb.get_task(conn, tid)
        assert kb.request_changes(conn, tid, reason="add assertion", expected_run_id=review.current_run_id) == (True, "builder")
        impl = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="fixed", expected_run_id=impl.current_run_id)
        # Prior reviewer runs are not mistaken for implementation authors.
        result = kb.dispatch_once(conn, spawn_fn=lambda *_: None)
        assert [(item[0], item[1]) for item in result.spawned] == [(tid, "reviewer")]


def test_legacy_review_mode_migration_is_additive_and_idempotent(home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy task", assignee="builder")
        conn.execute("ALTER TABLE tasks DROP COLUMN review_mode")
        conn.commit()
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        assert kb.get_task(conn, tid).review_mode == "auto"
        assert kb.get_task(conn, tid).status == "ready"


def test_author_diagnostic_is_not_starved_by_ready_work(home):
    with kb.connect() as conn:
        tid, _ = handoff(conn)
        ready = kb.create_task(conn, title="ready work", assignee="builder")
        result = kb.dispatch_once(conn, max_spawn=1, spawn_fn=lambda *_: None)
        assert [item[0] for item in result.spawned] == [ready]
        assert kb.get_task(conn, tid).status == "review"
        assert len(events(conn, tid, "review_unavailable")) == 1


def test_dispatch_claim_rechecks_assignment_after_preflight(home, monkeypatch):
    real_claim = kb.claim_review_task

    def race(conn, tid, **kwargs):
        assert kb.assign_task(conn, tid, "builder")
        return real_claim(conn, tid, **kwargs)

    monkeypatch.setattr(kb, "claim_review_task", race)
    with kb.connect() as conn:
        tid, _ = handoff(conn, reviewer="reviewer")
        before = kb.list_runs(conn, tid)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("stale reviewer spawned"))
        assert not result.spawned and not result.auto_blocked
        assert kb.get_task(conn, tid).status == "review"
        assert kb.list_runs(conn, tid) == before


@pytest.mark.parametrize("missing", ["sdlc-review", "domain-review"])
def test_missing_review_skill_is_diagnostic_not_crash(home, missing):
    if missing == "sdlc-review":
        (home / "profiles" / "reviewer" / "skills" / "sdlc-review" / "SKILL.md").unlink()
    with kb.connect() as conn:
        tid, _ = handoff(conn, reviewer="reviewer", skills=["domain-review"] if missing == "domain-review" else None)
        before = kb.list_runs(conn, tid)
        for _ in range(3):
            result = kb.dispatch_once(conn, spawn_fn=lambda *_: pytest.fail("unloadable review spawned"))
            assert not result.spawned and not result.crashed and not result.auto_blocked
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.consecutive_failures == 0
            assert task.current_run_id is None and task.claim_lock is None
            assert kb.list_runs(conn, tid) == before
        notices = events(conn, tid, "review_unavailable")
        assert len(notices) == 1
        assert missing in notices[0].payload["missing_skills"]
        assert "reviewer" in notices[0].payload["reason"]
