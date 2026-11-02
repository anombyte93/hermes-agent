"""Canonical plan identity is shared by clients, not a caller's reference file."""
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


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    with dbc.connect_closing(board="default") as conn:
        yield conn


def materialize(conn, **changes):
    api = importlib.import_module("hermes_cli.kanban_db_materialize")
    args = dict(project="fixture-project", plan_revision="r1", proposed_task_id="build",
                intent_fingerprint="intent-1", title="Bounded work",
                execution_kind="external_session")
    args.update(changes)
    return api.materialize_task(conn, **args)


def references(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT project,plan_revision,proposed_task_id,intent_fingerprint,task_id "
        "FROM task_materializations ORDER BY project,plan_revision,proposed_task_id")]


def parse(*argv):
    parser = argparse.ArgumentParser(prog="hermes")
    cli.build_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["kanban", *argv])


def invoke(capsys, *, board="default", revision="r1", proposed="build", fingerprint="intent-1",
           extra=()):
    args = parse("--board", board, "materialize", "--project", "fixture-project",
                 "--plan-revision", revision, "--proposed-id", proposed,
                 "--fingerprint", fingerprint, "--title", "Bounded work",
                 *extra, "--execution-kind", "external_session", "--json")
    rc = cli.kanban_command(args)
    output = capsys.readouterr()
    assert output.err == ""
    return rc, json.loads(output.out)


def test_ruling_six_all_five_cases_and_durable_mapping_event(store):
    first = materialize(store, body="Original acceptance")
    tid = first["task_id"]
    assert first == {"state": "created", "task_id": tid}
    original = kb.get_task(store, tid)
    assert tid.startswith("t_")
    assert original.execution_kind == "external_session"
    assert original.status == "ready"
    assert materialize(store) == {"state": "existing", "task_id": tid}
    assert materialize(store, intent_fingerprint="changed") == {
        "state": "conflict", "task_id": tid, "reason": "materialization_conflict"}
    assert materialize(store, plan_revision="r2") == {"state": "existing", "task_id": tid}
    assert references(store) == [
        ("fixture-project", rev, "build", "intent-1", tid) for rev in ("r1", "r2")]
    before = references(store)
    assert materialize(store, plan_revision="r3", intent_fingerprint="changed") == {
        "state": "conflict", "task_id": tid, "reason": "revision_requires_mapping"}
    assert references(store) == before
    assert materialize(store, plan_revision="r3", intent_fingerprint="changed", maps_from="r2:build") == {
        "state": "mapped", "task_id": tid}
    events = [event for event in kb.list_events(store, tid) if event.kind == "materialization_mapped"]
    assert len(events) == 1
    assert events[0].payload == dict(project="fixture-project", plan_revision="r3",
        proposed_task_id="build", intent_fingerprint="changed", maps_from="r2:build")
    assert materialize(store, plan_revision="r3", intent_fingerprint="changed", maps_from="r2:build") == {
        "state": "existing", "task_id": tid}
    assert len(kb.list_events(store, tid)) == 2  # created + mapping, no replay event
    assert len(kb.list_tasks(store)) == 1
    # Mapping now carries the supplied intent fields, without fresh execution.
    mapped = kb.get_task(store, tid)
    assert mapped.title == original.title and mapped.body is None
    assert mapped.status == original.status and not kb.list_runs(store, tid)


def test_matching_any_earlier_fingerprint_reuses_card_even_after_changed_revision(store):
    tid = materialize(store)["task_id"]
    materialize(store, plan_revision="r2", intent_fingerprint="changed", maps_from="r1:build")
    assert materialize(store, plan_revision="r3") == {"state": "existing", "task_id": tid}
    assert len(references(store)) == 3
    assert len(kb.list_tasks(store)) == 1


def test_rename_mapping_and_true_replacement_are_distinct(store):
    tid = materialize(store)["task_id"]
    assert materialize(store, plan_revision="r2", proposed_task_id="renamed",
                       intent_fingerprint="renamed-intent", maps_from="r1:build") == {
        "state": "mapped", "task_id": tid}
    replacement = materialize(store, plan_revision="r2", proposed_task_id="replacement")
    assert replacement["state"] == "created" and replacement["task_id"] != tid
    assert kb.get_task(store, tid).status == "ready"  # no implicit closure
    assert len(kb.list_tasks(store)) == 2


@pytest.mark.parametrize("maps_from", ["missing:build", "r1:missing"])
@pytest.mark.parametrize("proposed", ["build", "new-name"])
def test_missing_mapping_row_refuses_without_any_write(store, maps_from, proposed):
    materialize(store)
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="mapping_not_found"):
        materialize(store, plan_revision="r2", proposed_task_id=proposed,
                    intent_fingerprint="changed", maps_from=maps_from)
    assert "\n".join(store.iterdump()) == before


def test_mapping_never_resolves_in_another_project_or_the_current_revision(store):
    materialize(store)
    with pytest.raises(ValueError, match="mapping_not_found"):
        materialize(store, project="other-project", plan_revision="r2", maps_from="r1:build")
    with pytest.raises(ValueError, match="prior revision"):
        materialize(store, proposed_task_id="renamed", maps_from="r1:build")
    assert len(kb.list_tasks(store)) == 1


def test_mapping_cannot_redirect_an_established_logical_id_to_another_card(store):
    first = materialize(store)
    materialize(store, proposed_task_id="other")
    assert materialize(store, plan_revision="r2", intent_fingerprint="changed", maps_from="r1:other") == {
        "state": "conflict", "task_id": first["task_id"], "reason": "materialization_conflict"}
    # Even a valid mapping cannot override the same-revision fingerprint guard.
    assert materialize(store, intent_fingerprint="changed", maps_from="r1:other") == {
        "state": "conflict", "task_id": first["task_id"], "reason": "materialization_conflict"}
    assert len(references(store)) == 2


@pytest.mark.parametrize("changes", [dict(project=""), dict(plan_revision=" "),
    dict(proposed_task_id=""), dict(intent_fingerprint=""), dict(title=""),
    dict(execution_kind="worker"), dict(maps_from="r1"), dict(maps_from=":build"),
    dict(maps_from="r1:"), dict(maps_from="r1:build:extra")])
def test_invalid_request_never_creates_partial_state(store, changes):
    with pytest.raises(ValueError):
        materialize(store, **changes)
    assert not kb.list_tasks(store)
    assert not references(store)


@pytest.mark.parametrize("race", ["same-key", "same-key-changed-intent", "new-revisions"])
def test_real_concurrent_connections_read_back_one_card(store, race):
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(4)

    def contender(index):
        with dbc.connect_closing(path) as conn:
            barrier.wait(timeout=10)
            return materialize(conn,
                plan_revision=f"r{index}" if race == "new-revisions" else "r1",
                intent_fingerprint=f"intent-{index}" if race == "same-key-changed-intent" else "intent-1")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(contender, range(4)))
    assert sum(result["state"] == "created" for result in results) == 1
    assert len({result["task_id"] for result in results}) == 1
    expected = "conflict" if race == "same-key-changed-intent" else "existing"
    assert sum(result["state"] == expected for result in results) == 3
    with dbc.connect_closing(path) as observer:
        assert len(kb.list_tasks(observer)) == 1
        assert len(references(observer)) == (4 if race == "new-revisions" else 1)
        assert observer.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_database_itself_enforces_materialization_key_uniqueness(store):
    tid = materialize(store)["task_id"]
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    # Bypass the kernel on a different connection: every client shares this constraint.
    with dbc.connect_closing(path) as other:
        with pytest.raises(sqlite3.IntegrityError):
            other.execute("INSERT INTO task_materializations "
                "(project,plan_revision,proposed_task_id,intent_fingerprint,task_id,created_at) "
                "VALUES (?,?,?,?,?,?)", ("fixture-project", "r1", "build", "other", tid, 0))
    assert len(references(store)) == 1


def test_card_links_event_and_reference_commit_once_and_roll_back_together(store):
    parent = materialize(store, proposed_task_id="parent")["task_id"]
    trace = []
    store.set_trace_callback(trace.append)
    child = materialize(store, parent_proposed=["parent"], body="Acceptance\nKeep this literal.")["task_id"]
    store.set_trace_callback(None)
    assert sum(sql == "BEGIN IMMEDIATE" for sql in trace) == 1
    assert sum(sql == "COMMIT" for sql in trace) == 1
    assert kb.get_task(store, child).status == "todo"
    assert kb.get_task(store, child).body == "Acceptance\nKeep this literal."
    assert kb.parent_ids(store, child) == [parent]
    # Refuse the final reference write after create_task has made its card and event.
    store.execute("CREATE TEMP TRIGGER refuse_reference BEFORE INSERT ON task_materializations "
                  "BEGIN SELECT RAISE(ABORT, 'fixture insert failure'); END")
    before = "\n".join(store.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match="fixture insert failure"):
        materialize(store, proposed_task_id="failed", parent_proposed=["parent"])
    assert "\n".join(store.iterdump()) == before
    assert len(kb.list_tasks(store)) == 2


def test_unknown_parent_refuses_and_parent_resolution_is_revision_scoped(store):
    materialize(store, proposed_task_id="parent")
    with pytest.raises(ValueError, match="parent"):
        materialize(store, parent_proposed=["missing"])
    with pytest.raises(ValueError, match="parent"):
        materialize(store, plan_revision="r2", parent_proposed=["parent"])
    assert len(kb.list_tasks(store)) == 1
    assert len(references(store)) == 1


def test_additive_idempotent_migration_preserves_existing_tasks_and_references(store):
    legacy = kb.create_task(store, title="existing worker", body="keep me")
    before = kb.get_task(store, legacy)
    # Simulate a populated pre-materialization board without touching any task storage.
    store.execute("DROP TABLE IF EXISTS task_materializations")
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    dbc.init_db(path)
    first = materialize(store)
    expected = references(store)
    for _ in range(2):
        dbc.init_db(path)
        with dbc.connect_closing(path) as observer:
            assert kb.get_task(observer, legacy) == before
            assert references(observer) == expected
            assert materialize(observer) == {"state": "existing", "task_id": first["task_id"]}


@pytest.mark.parametrize("lookup", [None, "raises", "appeared"])
def test_materialized_external_card_never_launches_a_worker(store, monkeypatch, lookup):
    parent = materialize(store, proposed_task_id="parent")["task_id"]
    child = materialize(store, parent_proposed=["parent"])["task_id"]
    spawn = Mock(return_value=None)
    monkeypatch.setattr(dispatch, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(dispatch, "_system_memory_sample", lambda: {})
    monkeypatch.setattr(dispatch, "_resolve_default_assignee", lambda name: name)
    resolver = Mock(side_effect=RuntimeError("fixture unavailable")) if lookup == "raises" else (
        lambda: None if lookup is None else lambda name: True)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", resolver)
    for tid in (parent, child):
        kb.assign_task(store, tid, "fixture-owner")
    for lane in ("ready", "review"):
        store.execute("UPDATE tasks SET status=? WHERE id IN (?,?)", (lane, parent, child))
        result = dispatch.dispatch_once(store, spawn_fn=spawn, default_assignee="fixture-owner")
        assert not result.spawned
    assert not spawn.called
    assert not kb.list_runs(store, parent) and not kb.list_runs(store, child)
    assert {kb.get_task(store, tid).execution_kind for tid in (parent, child)} == {"external_session"}


def test_exact_cli_contract_body_parents_all_states_and_board_isolation(store, capsys, tmp_path):
    kb.create_board("fixture-board")
    body = tmp_path / "body.md"
    body.write_text("Literal acceptance\n", encoding="utf-8")
    rc, parent = invoke(capsys, board="fixture-board", proposed="parent")
    assert rc == 0 and parent["state"] == "created"
    rc, created = invoke(capsys, board="fixture-board", extra=(
        "--body-file", str(body), "--parent-proposed", "parent"))
    tid = created["task_id"]
    assert rc == 0 and created == {"state": "created", "task_id": tid}
    for revision, fingerprint, extra, state, code, reason in [
        ("r1", "intent-1", (), "existing", 0, None),
        ("r1", "changed", (), "conflict", 3, "materialization_conflict"),
        ("r2", "intent-1", (), "existing", 0, None),
        ("r3", "changed", (), "conflict", 3, "revision_requires_mapping"),
        ("r3", "changed", ("--maps-from", "r2:build"), "mapped", 0, None),
    ]:
        rc, result = invoke(capsys, board="fixture-board", revision=revision,
                            fingerprint=fingerprint, extra=extra)
        expected = {"state": state, "task_id": tid}
        if reason:
            expected["reason"] = reason
        assert (rc, result) == (code, expected)
    assert not kb.list_tasks(store)
    with dbc.connect_closing(board="fixture-board") as conn:
        # The r3 mapping supplied neither a body nor parents, so both are removed.
        assert kb.get_task(conn, tid).body is None
        assert kb.get_task(conn, tid).execution_kind == "external_session"
        assert kb.parent_ids(conn, tid) == []
        assert len(kb.list_tasks(conn)) == 2


def test_cli_missing_mapping_refuses_with_no_card(store, capsys):
    rc, result = invoke(capsys, revision="r2", extra=("--maps-from", "missing:build"))
    assert rc == 1 and result == {"state": "refused", "reason": "mapping_not_found"}
    assert not kb.list_tasks(store) and not references(store)


def test_materialization_respects_delegated_child_mutation_boundary(store, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    with pytest.raises(PermissionError):
        materialize(store)
    args = parse("materialize", "--project", "fixture-project", "--plan-revision", "r1",
                 "--proposed-id", "build", "--fingerprint", "intent-1", "--title", "work",
                 "--execution-kind", "external_session", "--json")
    assert cli.kanban_command(args) != 0
    assert "delegate_task" in capsys.readouterr().err
    assert not kb.list_tasks(store)
