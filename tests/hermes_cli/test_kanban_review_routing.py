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


@pytest.mark.parametrize("parked", ["parent", "author", "sdlc-review", "domain-review"])
def test_spawnable_review_probe_excludes_parked_cards(home, parked):
    with kb.connect() as conn:
        kwargs = {"reviewer": "reviewer"}
        if parked == "parent":
            kwargs["review_mode"] = "parent"
        elif parked == "author":
            kwargs["reviewer"] = None
        elif parked == "sdlc-review":
            (home / "profiles" / "reviewer" / "skills" / parked / "SKILL.md").unlink()
        else:
            kwargs["skills"] = [parked]
        tid, _ = handoff(conn, **kwargs)
        before = (kb.list_runs(conn, tid), kb.list_events(conn, tid))
        assert kb.get_task(conn, tid).status == "review"
        assert kb.has_spawnable_review(conn) is False
        assert (kb.list_runs(conn, tid), kb.list_events(conn, tid)) == before
        preview = kb.dispatch_once(conn, dry_run=True)
        assert not preview.spawned


def test_spawnable_review_probe_finds_independent_review_among_parked_cards(home):
    with kb.connect() as conn:
        handoff(conn, review_mode="parent", reviewer="reviewer")
        handoff(conn)
        tid, _ = handoff(conn, reviewer="reviewer")
        assert kb.has_spawnable_review(conn) is True
        preview = kb.dispatch_once(conn, dry_run=True)
        assert [item[0] for item in preview.spawned] == [tid]
        review = kb.claim_review_task(conn, tid, expected_assignee="reviewer")
        assert review is not None
        assert kb.has_spawnable_review(conn) is False


def test_spawnable_review_probe_caches_skills_per_scope_not_between_probes(home, monkeypatch):
    calls = []
    real_missing = kb._missing_worker_skills

    def missing(skills, assignee, cwd):
        calls.append((skills, assignee, cwd))
        return real_missing(skills, assignee, cwd)

    monkeypatch.setattr(kb, "_missing_worker_skills", missing)
    with kb.connect() as conn:
        for _ in range(3):
            handoff(conn, reviewer="reviewer", skills=["domain-review"])
        assert kb.has_spawnable_review(conn) is False
        assert calls == [(["domain-review", "sdlc-review"], "reviewer", None)]
        skill = home / "profiles" / "reviewer" / "skills" / "domain-review" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: domain-review\ndescription: Review.\n---\nReview.\n")
        assert kb.has_spawnable_review(conn) is True
        assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("board_state", ["empty", "parked", "spawnable"])
async def test_gateway_stuck_warning_only_for_spawnable_review(home, monkeypatch, caplog, board_state):
    import logging
    from types import SimpleNamespace

    from gateway import kanban_watchers as watchers
    from hermes_cli import config

    with kb.connect() as conn:
        if board_state == "parked":
            handoff(conn, review_mode="parent", reviewer="reviewer")
            handoff(conn)
            handoff(conn, reviewer="reviewer", skills=["domain-review"])
        elif board_state == "spawnable":
            handoff(conn, reviewer="reviewer")
        assert len(kb.list_tasks(conn, status="review")) == {
            "empty": 0, "parked": 3, "spawnable": 1,
        }[board_state]

    runner = SimpleNamespace(_running=True, _release_kanban_dispatcher_lock=lambda: None)
    ticks = []

    def stalled_dispatch(conn, **kwargs):
        ticks.append(kwargs["board"])
        return SimpleNamespace(spawned=[])

    async def inline(func, *args):
        return func(*args)

    async def sleep(delay):
        if delay == 1 and len(ticks) >= 7:
            runner._running = False

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {
        "dispatch_interval_seconds": 1, "auto_decompose": False,
    }})
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)
    monkeypatch.setattr(kb, "list_boards", lambda **kw: [{"slug": kb.DEFAULT_BOARD}])
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "dispatch_once", stalled_dispatch)
    monkeypatch.setattr(watchers, "_acquire_singleton_lock", lambda path: (None, "held"))
    monkeypatch.setattr(watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(watchers, "_to_thread_process_service", inline)
    monkeypatch.setattr(watchers.asyncio, "sleep", sleep)
    with caplog.at_level(logging.WARNING, logger=watchers.__name__):
        await watchers.GatewayKanbanWatchersMixin._kanban_dispatcher_watcher(runner)
    assert len(ticks) == 7
    assert "unexpected watcher error" not in caplog.text
    warnings = [record for record in caplog.records if "kanban dispatcher stuck" in record.message]
    assert len(warnings) == (1 if board_state == "spawnable" else 0)


@pytest.mark.parametrize("surface", ["worker", "tool", "reviewer"])
def test_review_handoff_guidance_explains_omitted_reviewer(surface):
    from agent.prompt_builder import KANBAN_GUIDANCE
    from tools.kanban_tools import KANBAN_REQUEST_REVIEW_SCHEMA

    schema = KANBAN_REQUEST_REVIEW_SCHEMA
    text = {
        "worker": KANBAN_GUIDANCE,
        "tool": schema["description"],
        "reviewer": schema["parameters"]["properties"]["reviewer"]["description"],
    }[surface]
    assert "independent reviewer" in text
    assert "omit `reviewer`" in text
    assert "waits in `review`" in text
    assert "parent or human" in text
    assert "no self-review" in text
    assert "reviewer" not in schema["parameters"]["required"]
