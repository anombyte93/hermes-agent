"""Parent rulings 10-13, exercised against disposable canonical boards."""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_materialize as api
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_external import external_claim, external_release


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    with connect_closing(board="default") as conn:
        yield conn


def row(**changes):
    result = dict(project="fixture", plan_revision="r1", proposed_task_id="q",
                  intent_fingerprint="intent", title="Original", body="Original body")
    result.update(changes)
    return result


def materialize(conn, **changes):
    return api.materialize_task(conn, **row(**changes))


def claim(conn, tid):
    return external_claim(conn, tid, board="default", owner_host="fixture-host",
        incarnation=dict(session="fixture", id="$1", created="1", pane="%1", pid="00123",
                         backend="claude", conversation="fixture-conversation"),
        worktree="/fixture/worktree", reservation_id="fixture-reservation", reservation_token="a" * 64)


def dump(conn):
    return "\n".join(conn.iterdump())


def test_mapping_applies_intent_dependencies_and_gates_claim_atomically(store):
    parent = materialize(store, proposed_task_id="p")["task_id"]
    child = materialize(store)["task_id"]
    materialize(store, proposed_task_id="p", plan_revision="r2")
    changed = row(plan_revision="r2", intent_fingerprint="changed", maps_from="r1:q",
                  title="Changed", body="Changed body", parent_proposed=["p"])
    # The last write must roll back title, body, links, status and mapping event together.
    store.execute("CREATE TEMP TRIGGER refuse_reference BEFORE INSERT ON task_materializations "
                  "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    before = dump(store)
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        api.materialize_task(store, **changed)
    assert dump(store) == before
    store.execute("DROP TRIGGER refuse_reference")
    assert api.materialize_task(store, **changed) == {"state": "mapped", "task_id": child}
    task = kb.get_task(store, child)
    assert (task.title, task.body, task.status) == ("Changed", "Changed body", "todo")
    assert kb.parent_ids(store, child) == [parent]
    assert [e.kind for e in kb.list_events(store, child)] == ["created", "materialization_mapped"]
    with pytest.raises(ValueError, match="parents|ready"):
        claim(store, child)
    # Even a stale writer forcing ready cannot bypass external claim's parent guard.
    store.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
    before = dump(store)
    with pytest.raises(ValueError, match="parents"):
        claim(store, child)
    assert dump(store) == before
    store.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    assert claim(store, child)["task"]["status"] == "running"


@pytest.mark.parametrize("change", [dict(title="Changed"), dict(body="Changed body"),
                                   dict(parent_proposed=["p"])])
@pytest.mark.parametrize("released", [False, True])
def test_started_mapping_refuses_changes_but_accepts_identical_intent_fields(store, change, released):
    materialize(store, proposed_task_id="p", plan_revision="r2")
    tid = materialize(store)["task_id"]
    owned = claim(store, tid)
    if released:
        external_release(store, tid, owned["run_id"], reservation_token="a" * 64, reason="fixture")
    before = dump(store)
    assert materialize(store, plan_revision="r2", intent_fingerprint="changed",
                       maps_from="r1:q", **change) == {
        "state": "conflict", "task_id": tid, "reason": "mapping_card_started"}
    assert dump(store) == before
    assert materialize(store, plan_revision="r2", intent_fingerprint="changed", maps_from="r1:q") == {
        "state": "mapped", "task_id": tid}


def test_mapping_can_remove_dependencies_without_reopening_an_archived_card(store):
    materialize(store, proposed_task_id="p")
    tid = materialize(store, parent_proposed=["p"])["task_id"]
    assert materialize(store, plan_revision="r2", intent_fingerprint="changed", maps_from="r1:q")["state"] == "mapped"
    assert kb.parent_ids(store, tid) == [] and kb.get_task(store, tid).status == "ready"
    assert kb.archive_task(store, tid)
    materialize(store, plan_revision="r3", intent_fingerprint="again", maps_from="r2:q")
    assert kb.get_task(store, tid).status == "archived"


@pytest.mark.parametrize("method", ["delete_task", "delete_archived_task"])
def test_materialized_card_deletion_refuses_named_reason_and_preserves_every_row(store, method):
    tid = materialize(store)["task_id"]
    assert kb.archive_task(store, tid)
    before = dump(store)
    with pytest.raises(ValueError, match="materialized_card_retained"):
        getattr(kb, method)(store, tid)
    assert dump(store) == before
    assert materialize(store)["task_id"] == tid


def test_archive_rm_reports_reason_without_traceback_but_plain_archive_is_allowed(store, capsys):
    import argparse
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    tid = materialize(store)["task_id"]
    assert cli.kanban_command(parser.parse_args(["kanban", "archive", tid])) == 0
    capsys.readouterr()
    assert cli.kanban_command(parser.parse_args(["kanban", "archive", "--rm", tid])) != 0
    output = capsys.readouterr()
    assert "materialized_card_retained" in output.err and "Traceback" not in output.err
    assert kb.get_task(store, tid).status == "archived"


@pytest.mark.parametrize("keep_first", [False, True])
def test_rename_retires_source_in_either_order_and_keeps_project_boundary(store, keep_first):
    tid = materialize(store, proposed_task_id="x")["task_id"]
    if keep_first:
        materialize(store, proposed_task_id="x", plan_revision="r2")
    renamed = materialize(store, proposed_task_id="y", plan_revision="r2", intent_fingerprint="changed",
                          maps_from="r1:x")
    if keep_first:
        assert renamed == {"state": "conflict", "task_id": tid, "reason": "logical_id_retired"}
    else:
        assert renamed == {"state": "mapped", "task_id": tid}
        for revision in ("r2", "r3"):
            before = dump(store)
            assert materialize(store, proposed_task_id="x", plan_revision=revision) == {
                "state": "conflict", "task_id": tid, "reason": "logical_id_retired"}
            assert dump(store) == before
        assert materialize(store, project="another", proposed_task_id="x")["state"] == "created"


def test_racing_keep_and_rename_never_accepts_both(store):
    materialize(store, proposed_task_id="x")
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)

    def contender(rename):
        with connect_closing(path) as conn:
            barrier.wait(timeout=10)
            return materialize(conn, proposed_task_id="y" if rename else "x", plan_revision="r2",
                               intent_fingerprint="changed" if rename else "intent",
                               maps_from="r1:x" if rename else None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, [False, True]))
    assert sorted(r["state"] for r in results).count("conflict") == 1
    assert next(r for r in results if r["state"] == "conflict")["reason"] == "logical_id_retired"
    assert len(kb.list_tasks(store)) == 1


def test_check_matches_real_decisions_and_leaves_no_database_writes(store):
    for changes in ({}, {}, dict(intent_fingerprint="changed"), dict(plan_revision="r2"),
                    dict(plan_revision="r3", intent_fingerprint="changed"),
                    dict(plan_revision="r3", intent_fingerprint="changed", maps_from="r2:q")):
        before, total = dump(store), store.total_changes
        checked = api.check_materialization(store, **row(**changes))
        assert dump(store) == before and store.total_changes == total
        real = materialize(store, **changes)
        expected = dict(real)
        if real["state"] == "created":
            expected["task_id"] = None
        assert checked == expected
    tid = kb.list_tasks(store)[0].id
    claim(store, tid)
    before = dump(store)
    args = row(plan_revision="r4", maps_from="r3:q", intent_fingerprint="again", title="Changed")
    assert api.check_materialization(store, **args) == api.materialize_task(store, **args) == {
        "state": "conflict", "task_id": tid, "reason": "mapping_card_started"}
    assert dump(store) == before
