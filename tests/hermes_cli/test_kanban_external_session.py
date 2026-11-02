"""External owners are durable bindings, never dispatcher workers.

All stores and profile homes are disposable. Spawns, process probes, signalling
and workspace cleanup are effects we spy on, never live operations.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib
import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_workspace as workspace

# Synthetic reservation secrets, not credentials or captured operator state.
TOKEN = "a" * 64
NEXT_TOKEN = "b" * 64
OWNER_HOST = "fixture-host"
INCARNATION = dict(session="existing-owner", id="$7", created="1000", pane="%9",
                   pid=424242, backend="claude", conversation="fixture-conversation")


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(dispatch, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dispatch, "_system_memory_sample", lambda: {})
    monkeypatch.setattr(dispatch, "_resolve_default_assignee", lambda name: name)
    with dbc.connect_closing(board="default") as conn:
        yield conn


def external_api():
    return importlib.import_module("hermes_cli.kanban_db_external")


def create(conn, **kwargs):
    return kb.create_task(conn, title="existing session", execution_kind="external_session", **kwargs)


def claim(conn, task_id, **kwargs):
    args = dict(board="default", owner_host=OWNER_HOST, incarnation=INCARNATION,
                worktree=str(Path.home() / "worktree"), reservation_id="fixture-r1",
                reservation_token=TOKEN)
    args.update(kwargs)
    return external_api().external_claim(conn, task_id, **args)


def tick(conn, **kwargs):
    spawn = Mock(return_value=None)
    result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_in_progress=100, **kwargs)
    assert not spawn.called
    assert not result.spawned
    return result


@pytest.mark.parametrize("lookup", [None, False, True])
@pytest.mark.parametrize("lane", ["ready", "review"])
@pytest.mark.parametrize("policy", [None, [], ["same-profile"]])
def test_kind_excludes_every_dispatch_selection(store, monkeypatch, lookup, lane, policy):
    tid = create(store, assignee="same-profile")
    store.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    store.commit()
    policy_path = kb.board_metadata_path("default")
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps({"allowed_assignees": policy}), encoding="utf-8")
    monkeypatch.setattr(dispatch, "_profile_exists_fn",
                        lambda: None if lookup is None else lambda name: lookup)
    assert not dispatch._lane_rows(store, lane)
    assert not dispatch.has_spawnable_ready(store)
    assert not dispatch.spawnable_ready_ids(store)
    assert not dispatch.has_spawnable_review(store)
    row = store.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert not dispatch._any_spawnable_review([row])
    tick(store)
    assert kb.get_task(store, tid).status == lane


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("lane", ["ready", "review"])
def test_second_dispatch_guard_precedes_every_effect(store, monkeypatch, dry_run, lane):
    tid = create(store, assignee="same-profile")
    row = store.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    effects = Mock(side_effect=AssertionError("external dispatcher effect"))
    monkeypatch.setattr(dispatch, "_profile_exists_fn", effects)
    result = dispatch.DispatchResult()
    assert not dispatch._dispatch_lane_task(
        store, row, "same-profile", result, lane=lane, dry_run=dry_run,
        ttl_seconds=None, board="default", failure_limit=2, spawn_fn=effects,
        per_profile_cap=None, per_profile_running={})
    assert result.skipped_external_session == [tid]
    assert not effects.called
    assert not kb.list_runs(store, tid)


def test_unreadable_kind_is_not_a_worker(store, monkeypatch):
    tid = create(store)
    row = store.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    omitted = {key: row[key] for key in row.keys() if key != "execution_kind"}
    assert kb.Task.from_row(omitted).execution_kind is None
    assert not dispatch._any_spawnable_review([dict(id=tid, assignee="same-profile")])
    # Stale selection must consult the authoritative row, not trust its snapshot.
    result = dispatch.DispatchResult()
    assert not dispatch._dispatch_lane_task(
        store, dict(id="missing", assignee="same-profile"), "same-profile", result,
        lane="ready", dry_run=True, ttl_seconds=None, board="default", failure_limit=2,
        spawn_fn=Mock(), per_profile_cap=None, per_profile_running={})
    assert not result.spawned


@pytest.mark.parametrize("method", ["claim_task", "claim_review_task"])
def test_only_external_claim_can_open_attempt(store, method):
    tid = create(store)
    assert getattr(kb, method)(store, tid) is None
    assert not kb.list_runs(store, tid)
    event = kb.list_events(store, tid)[-1]
    assert event.kind == "claim_rejected"
    assert event.payload["reason"] == "external_session"


def test_default_assignment_does_not_adopt_external_card(store):
    tid = create(store)
    tick(store, default_assignee="same-profile")
    assert kb.get_task(store, tid).assignee is None
    assert not dispatch._apply_default_assignee(store, tid, "same-profile", dry_run=False)
    assert kb.get_task(store, tid).assignee is None


@pytest.mark.parametrize("options", [dict(goal_mode=True), dict(goal_max_turns=2),
    dict(model_override="fixture-model"), dict(provider_override="fixture-provider"),
    dict(skills=["fixture-skill"])])
def test_external_creation_refuses_worker_configuration(store, options):
    with pytest.raises(ValueError):
        create(store, **options)
    assert not kb.list_tasks(store)


def test_execution_kind_is_sticky_and_legacy_default_is_worker(store):
    worker = kb.create_task(store, title="worker")
    ext = create(store)
    assert kb.get_task(store, worker).execution_kind == "worker"
    for tid, target in [(worker, "external_session"), (ext, "worker")]:
        with pytest.raises(sqlite3.IntegrityError, match="execution_kind"):
            store.execute("UPDATE tasks SET execution_kind=? WHERE id=?", (target, tid))
        store.rollback()
    with pytest.raises(ValueError, match="execution_kind"):
        kb.create_task(store, title="bad", execution_kind="typo")


def test_populated_legacy_migration_twice_preserves_every_row(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as old:
        old.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,
            body TEXT, assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0,
            created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
            completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT, claim_lock TEXT, claim_expires INTEGER)""")
        for n, status in enumerate(["ready", "running", "done", "review", "todo", "blocked"]):
            old.execute("INSERT INTO tasks(id,title,status,created_at) VALUES(?,?,?,?)",
                        (f"legacy-{n}", f"retained-{n}", status, n + 1))
        expected = old.execute("SELECT id,title,status,created_at FROM tasks ORDER BY id").fetchall()
    for _ in range(2):
        dbc.init_db(path)
        with dbc.connect_closing(path) as upgraded:
            assert [tuple(r) for r in upgraded.execute(
                "SELECT id,title,status,created_at FROM tasks ORDER BY id")] == expected
            assert {r[0] for r in upgraded.execute("SELECT execution_kind FROM tasks")} == {"worker"}
            assert upgraded.execute("SELECT COUNT(*) FROM task_external_bindings").fetchone()[0] == 0


def test_claim_replay_is_one_run_one_event_and_never_stores_token(store):
    tid = create(store, session_id="originating-hermes-chat")
    claimed = claim(store, tid)
    replay = claim(store, tid)
    assert claimed == replay
    assert claimed["binding"]["state"] == "active"
    assert claimed["task"]["session_id"] == "originating-hermes-chat"
    task = kb.get_task(store, tid)
    assert task.status == "running"
    assert task.claim_lock == "external:fixture-r1"
    assert task.claim_expires is None
    run = kb.get_run(store, claimed["run_id"])
    assert run.claim_expires is None and run.worker_pid is None
    assert len(kb.list_runs(store, tid)) == 1
    assert len(kb.list_events(store, tid)) == 2
    dump = "\n".join(store.iterdump())
    assert TOKEN not in dump
    assert "reservation_token_sha256" not in json.dumps(claimed)


@pytest.mark.parametrize("change", [dict(board="wrong"), dict(reservation_token=NEXT_TOKEN),
    dict(incarnation={**INCARNATION, "conversation": "replacement"}),
    dict(worktree="/different/worktree")])
def test_replay_refuses_changed_reservation_or_incarnation(store, change):
    tid = create(store)
    original = claim(store, tid)
    with pytest.raises(ValueError):
        claim(store, tid, **change)
    assert external_api().external_status(store, tid) == original


def test_claim_refuses_wrong_kind_parents_wrong_board_and_worktree(store):
    worker = kb.create_task(store, title="worker")
    child = create(store, parents=[worker])
    with pytest.raises(ValueError):
        claim(store, worker)
    with pytest.raises(ValueError):
        claim(store, child)
    tid = create(store, workspace_kind="dir", workspace_path=str(Path.home() / "expected"))
    with pytest.raises(ValueError, match="worktree"):
        claim(store, tid)
    with pytest.raises(ValueError, match="board"):
        claim(store, tid, board="wrong", worktree=str(Path.home() / "expected"))
    assert not kb.list_runs(store, tid)


@pytest.mark.parametrize("target", ["same-card", "same-incarnation"])
def test_concurrent_external_claims_have_one_winner(store, target):
    first, second = create(store), create(store)
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)

    def contender(index):
        with dbc.connect_closing(path) as conn:
            barrier.wait(timeout=10)
            try:
                return claim(conn, first if target == "same-card" or index == 0 else second,
                             reservation_id=f"race-{index}")
            except ValueError:
                return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, [0, 1]))
    assert sum(result is not None for result in results) == 1
    assert store.execute("SELECT COUNT(*) FROM task_external_bindings WHERE state='active'").fetchone()[0] == 1
    assert store.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1


@pytest.mark.parametrize("reaper", ["release_stale_claims", "detect_crashed_workers",
    "detect_stale_running", "enforce_max_runtime", "reconcile_orphaned_running", "startup"])
def test_lost_contact_never_reaps_binding_or_process(store, monkeypatch, reaper):
    tid = create(store)
    claimed = claim(store, tid)
    # Deliberately worker-shaped stale bookkeeping proves the kind is the guard,
    # not the absence of a PID, a remote hostname or the non-expiring claim.
    store.execute("UPDATE tasks SET claim_lock=?,worker_pid=424242,claim_expires=1,"
                  "started_at=1,last_heartbeat_at=1,max_runtime_seconds=1 WHERE id=?",
                  (kb._host_prefix() + "99999999", tid))
    store.execute("UPDATE task_runs SET started_at=1 WHERE id=?", (claimed["run_id"],))
    if reaper in {"reconcile_orphaned_running", "startup"}:
        store.execute("UPDATE tasks SET claim_lock=NULL,claim_expires=NULL WHERE id=?", (tid,))
    store.commit()
    before = "\n".join(store.iterdump())
    effects = Mock(return_value=None)
    monkeypatch.setattr(dispatch, "_terminate_reclaimed_worker", effects)
    monkeypatch.setattr(dispatch, "_kill_fn", effects)
    if reaper == "startup":
        path = Path(store.execute("PRAGMA database_list").fetchone()[2])
        dbc.init_db(path)
        tick(store, stale_timeout_seconds=1)
    elif reaper == "release_stale_claims":
        assert kb.release_stale_claims(store, signal_fn=effects) == 0
    elif reaper == "detect_stale_running":
        assert dispatch.detect_stale_running(store, stale_timeout_seconds=1, signal_fn=effects) == []
    elif reaper == "enforce_max_runtime":
        assert dispatch.enforce_max_runtime(store, signal_fn=effects) == []
    else:
        assert getattr(dispatch, reaper)(store) == []
    assert not effects.called
    assert "\n".join(store.iterdump()) == before


@pytest.mark.parametrize("operation", ["release", "complete", "handover"])
def test_exact_attempt_and_token_required(store, operation):
    tid = create(store)
    active = claim(store, tid)
    api = external_api()
    fn = getattr(api, "external_" + operation)
    extra = dict(reason="give up") if operation == "release" else dict(summary="finished")
    if operation == "handover":
        extra = dict(owner_host=OWNER_HOST, incarnation={**INCARNATION, "pid": 434343},
                     reservation_id="fixture-r2", new_reservation_token=NEXT_TOKEN,
                     worktree=active["binding"]["worktree"], evidence="explicit transfer")
    for rid, token in [(active["run_id"] + 1, TOKEN), (active["run_id"], NEXT_TOKEN)]:
        with pytest.raises(ValueError):
            fn(store, tid, rid, reservation_token=token, **extra)
    assert api.external_status(store, tid) == active


def test_release_serial_reuse_and_stale_attempt_refusal(store):
    first, second = create(store), create(store)
    active = claim(store, first)
    api = external_api()
    api.external_release(store, first, active["run_id"], reservation_token=TOKEN, reason="explicit give-up")
    assert kb.get_task(store, first).status == "ready"
    reused = claim(store, second, reservation_id="fixture-r2", reservation_token=NEXT_TOKEN)
    assert reused["binding"]["state"] == "active"
    with pytest.raises(ValueError):
        api.external_complete(store, first, active["run_id"], reservation_token=TOKEN, summary="stale")
    with pytest.raises(ValueError):
        claim(store, first)  # a closed reservation cannot be replayed as a new attempt
    tick(store)


def test_complete_and_parent_review_never_clean_or_stop_session(store, monkeypatch):
    tid = create(store)
    child = create(store, parents=[tid])
    active = claim(store, tid)
    cleanup = Mock(side_effect=AssertionError("external owner cleanup"))
    monkeypatch.setattr(workspace, "_cleanup_workspace", cleanup)
    api = external_api()
    review = api.external_complete(store, tid, active["run_id"], reservation_token=TOKEN,
                                   summary="please review", result="evidence", review=True)
    assert review["task"]["status"] == "review"
    assert review["binding"]["state"] == "closed"
    assert review["attempt"]["summary"] == "please review"
    tick(store)
    api.external_request_changes(store, tid, active["run_id"], reservation_token=TOKEN, reason="fix this")
    assert kb.get_task(store, tid).status == "ready"
    next_run = claim(store, tid, reservation_id="fixture-r2", reservation_token=NEXT_TOKEN)
    api.external_complete(store, tid, next_run["run_id"], reservation_token=NEXT_TOKEN,
                          summary="fixed", result="done")
    assert kb.get_task(store, child).status == "ready"
    assert not cleanup.called
    assert api.external_status(store, tid)["binding"]["state"] == "closed"
    assert len(kb.list_events(store, tid)) == 6


def test_handover_keeps_old_binding_and_stale_owner_cannot_act(store):
    tid = create(store)
    active = claim(store, tid)
    api = external_api()
    new = api.external_handover(store, tid, active["run_id"], reservation_token=TOKEN,
        owner_host=OWNER_HOST, incarnation={**INCARNATION, "pid": 434343, "conversation": "successor"},
        reservation_id="fixture-r2", new_reservation_token=NEXT_TOKEN,
        worktree=active["binding"]["worktree"], evidence="explicit parent-approved transfer")
    assert new["run_id"] != active["run_id"]
    old = store.execute("SELECT * FROM task_external_bindings WHERE run_id=?", (active["run_id"],)).fetchone()
    assert old["state"] == "handed_over" and old["closed_at"] is not None
    assert kb.get_run(store, active["run_id"]).outcome == "handed_over"
    with pytest.raises(ValueError):
        api.external_complete(store, tid, active["run_id"], reservation_token=TOKEN, summary="stale")
    assert api.external_status(store, tid) == new
    assert len(kb.list_events(store, tid)) == 3


@pytest.mark.parametrize("transition", ["assign", "reassign", "reclaim", "promote", "unblock",
    "recompute", "parent-unlock", "request-changes", "reopen-review", "retry"])
def test_lifecycle_routes_never_make_external_kind_spawnable(store, transition):
    tid = create(store)
    if transition == "assign":
        assert kb.assign_task(store, tid, "same-profile")
    elif transition == "reassign":
        assert kb.reassign_task(store, tid, "same-profile", reclaim_first=True)
    elif transition == "reclaim":
        claim(store, tid)
        assert not kb.reclaim_task(store, tid, signal_fn=Mock())
    elif transition in {"promote", "unblock", "recompute", "retry"}:
        assert kb.block_task(store, tid, reason="fixture hold")
        if transition == "promote":
            assert kb.promote_task(store, tid, actor="parent")[0]
        elif transition in {"unblock", "retry"}:
            assert kb.unblock_task(store, tid)
        else:
            kb.recompute_ready(store)
    elif transition == "parent-unlock":
        parent = kb.create_task(store, title="parent")
        kb.link_tasks(store, parent, tid)
        assert kb.complete_task(store, parent)
        assert kb.get_task(store, tid).status == "ready"
    else:
        active = claim(store, tid)
        external_api().external_complete(store, tid, active["run_id"], reservation_token=TOKEN,
                                         summary="review", review=True)
        if transition == "request-changes":
            external_api().external_request_changes(store, tid, active["run_id"],
                                                     reservation_token=TOKEN, reason="revise")
        else:
            assert kb.reopen_review_task(store, tid)
    tick(store, default_assignee="same-profile")
    assert kb.get_task(store, tid).execution_kind == "external_session"


def test_worker_apis_cannot_close_active_external_attempt(store):
    tid = create(store)
    active = claim(store, tid)
    assert not kb.complete_task(store, tid, expected_run_id=active["run_id"])
    assert not kb.request_review(store, tid, expected_run_id=active["run_id"], force=True)
    assert not kb.request_changes(store, tid, reason="wrong API", expected_run_id=active["run_id"])[0]
    assert not kb.block_task(store, tid, reason="wrong API")
    assert not kb.schedule_task(store, tid, reason="wrong API")
    assert not kb.archive_task(store, tid, signal_fn=Mock())
    assert not kb.heartbeat_claim(store, tid, claimer="external:fixture-r1")
    assert external_api().external_status(store, tid) == active


def parse(argv):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["kanban", *argv])


def test_cli_create_claim_status_show_complete(store, capsys):
    args = parse(["create", "external CLI", "--execution-kind", "external_session", "--json"])
    assert cli.kanban_command(args) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    args = parse(["external", "claim", "--task", tid, "--incarnation", json.dumps(INCARNATION),
        "--owner-host", OWNER_HOST, "--worktree", str(Path.home() / "worktree"), "--reservation-id", "fixture-cli",
        "--reservation-token", TOKEN, "--json"])
    assert cli.kanban_command(args) == 0
    active = json.loads(capsys.readouterr().out)
    for argv in [["external", "status", "--task", tid, "--json"], ["show", tid, "--json"]]:
        assert cli.kanban_command(parse(argv)) == 0
        output = capsys.readouterr().out
        body = json.loads(output)
        assert body["task"]["execution_kind"] == "external_session"
        assert body["binding"]["incarnation"]["session"] == INCARNATION["session"]
        assert TOKEN not in output
    assert cli.kanban_command(parse(["show", tid])) == 0
    assert "external_session" in capsys.readouterr().out
    assert cli.kanban_command(parse(["external", "complete", "--task", tid,
        "--run-id", str(active["run_id"]), "--reservation-token", TOKEN,
        "--summary", "verified", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["task"]["status"] == "done"


def test_idempotency_key_cannot_return_the_other_execution_kind(store):
    kb.create_task(store, title="worker", idempotency_key="shared-key")
    with pytest.raises(ValueError, match="execution_kind"):
        create(store, idempotency_key="shared-key")


@pytest.mark.parametrize("field,replacement", [("session", "other"), ("id", "$8"),
    ("created", "1001"), ("pane", "%10"), ("pid", 434343),
    ("backend", "codex"), ("conversation", "other-conversation")])
def test_replay_compares_every_literal_identity_field(store, field, replacement):
    tid = create(store)
    active = claim(store, tid)
    assert active["binding"]["incarnation"] == INCARNATION
    assert active["binding"]["owner_host"] == OWNER_HOST
    with pytest.raises(ValueError):
        claim(store, tid, incarnation={**INCARNATION, field: replacement})
    assert external_api().external_status(store, tid) == active


def test_simultaneous_replay_returns_same_attempt(store):
    tid = create(store)
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)

    def replay(_):
        with dbc.connect_closing(path) as conn:
            barrier.wait(timeout=10)
            return claim(conn, tid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(replay, [0, 1])
    assert first == second
    assert len(kb.list_runs(store, tid)) == 1
    assert len(kb.list_events(store, tid)) == 2


def test_handover_conflict_rolls_back_original_owner(store):
    first, second = create(store), create(store)
    active = claim(store, first)
    occupied = {**INCARNATION, "pid": 434343}
    claim(store, second, incarnation=occupied, reservation_id="occupied")
    with pytest.raises(ValueError, match="reservation_conflict"):
        external_api().external_handover(store, first, active["run_id"], reservation_token=TOKEN,
            owner_host=OWNER_HOST, incarnation=occupied, worktree=active["binding"]["worktree"],
            reservation_id="successor", new_reservation_token=NEXT_TOKEN, evidence="transfer")
    assert external_api().external_status(store, first) == active
    assert len(kb.list_events(store, first)) == 2


def test_parent_reopen_cannot_revoke_active_binding(store, monkeypatch):
    parent = kb.create_task(store, title="parent")
    assert kb.complete_task(store, parent)
    tid = create(store, parents=[parent])
    active = claim(store, tid)
    effects = Mock()
    monkeypatch.setattr(dispatch, "_terminate_reclaimed_worker", effects)
    invalidated = kb.invalidate_descendants_for_parent_reopen(store, parent, author="parent")
    assert invalidated == {"invalidated": [], "terminations": []}
    assert not effects.called
    assert external_api().external_status(store, tid) == active


def test_startup_with_missing_attempt_pointer_does_not_synthesise_a_worker(store):
    tid = create(store)
    claim(store, tid)
    store.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,))
    store.commit()
    before = "\n".join(store.iterdump())
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    dbc.init_db(path)
    assert "\n".join(store.iterdump()) == before
    tick(store)
    assert "\n".join(store.iterdump()) == before


def test_normal_workers_still_dispatch_with_external_cards_present(store):
    ext = create(store, assignee="same-profile")
    worker = kb.create_task(store, title="worker", assignee="same-profile",
                            workspace_kind="dir", workspace_path=str(Path.home() / "worker"))
    spawn = Mock(return_value=None)
    result = dispatch.dispatch_once(store, spawn_fn=spawn, max_in_progress=100)
    assert [item[0] for item in result.spawned] == [worker]
    assert spawn.call_count == 1
    assert kb.get_task(store, ext).status == "ready"
    assert kb.get_task(store, worker).status == "running"


@pytest.mark.parametrize("verb", ["release", "request-changes", "handover", "approve"])
def test_cli_other_external_lifecycle_verbs(store, capsys, verb):
    tid = create(store)
    active = claim(store, tid)
    if verb in {"request-changes", "approve"}:
        external_api().external_complete(store, tid, active["run_id"], reservation_token=TOKEN,
                                         summary="review", review=True)
    action = "complete" if verb == "approve" else verb
    argv = ["external", action, "--task", tid, "--run-id", str(active["run_id"]),
            "--reservation-token", TOKEN, "--json"]
    if verb in {"release", "request-changes"}:
        argv += ["--reason", "explicit parent action"]
    if verb == "handover":
        argv += ["--owner-host", OWNER_HOST, "--incarnation", json.dumps({**INCARNATION, "pid": 434343}),
                 "--worktree", active["binding"]["worktree"], "--reservation-id", "next-cli",
                 "--new-reservation-token", NEXT_TOKEN, "--evidence", "parent transfer"]
    assert cli.kanban_command(parse(argv)) == 0
    output = capsys.readouterr().out
    body = json.loads(output)
    expected = {"release": "ready", "request-changes": "ready", "handover": "running", "approve": "done"}
    assert body["task"]["status"] == expected[verb]
    assert TOKEN not in output and NEXT_TOKEN not in output
    tick(store)


def test_real_same_named_profile_and_unavailable_lookup_never_spawn(store, monkeypatch):
    from hermes_cli import profiles

    profile_dir = profiles.get_profile_dir("same-profile")
    assert profile_dir.is_relative_to(Path.home())
    profile_dir.mkdir(parents=True)
    assert profiles.profile_exists("same-profile")
    tid = create(store, assignee="same-profile")
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: profiles.profile_exists)
    tick(store)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: None)
    tick(store)
    assert kb.get_task(store, tid).status == "ready"
    assert not kb.list_runs(store, tid)


def test_closed_binding_still_excludes_workspace_and_tmux_cleanup(store, monkeypatch):
    tid = create(store)
    active = claim(store, tid)
    external_api().external_complete(store, tid, active["run_id"], reservation_token=TOKEN, summary="done")
    effects = Mock(side_effect=AssertionError("owner cleanup"))
    monkeypatch.setattr(workspace, "_cleanup_worker_tmux", effects)
    monkeypatch.setattr(workspace, "_cleanup_worktree_workspace", effects)
    store.execute("UPDATE tasks SET workspace_kind='worktree' WHERE id=?", (tid,))
    store.commit()
    workspace._cleanup_workspace(store, tid)
    assert not effects.called
    assert Path(active["binding"]["worktree"]).parent == Path.home()


def test_cli_wrong_token_refuses_as_json_without_effects(store, capsys):
    tid = create(store)
    active = claim(store, tid)
    assert cli.kanban_command(parse(["external", "release", "--task", tid,
        "--run-id", str(active["run_id"]), "--reservation-token", NEXT_TOKEN,
        "--reason", "wrong owner", "--json"])) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["state"] == "refused"
    assert TOKEN not in output and NEXT_TOKEN not in output
    assert external_api().external_status(store, tid) == active
