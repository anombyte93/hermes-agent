"""Race and upgrade boundaries for mapped canonical intent."""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_materialize import materialize_task, migrate_materializations
from hermes_cli.kanban_db_external import external_claim


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    with connect_closing(board="default") as conn:
        yield conn


def make(conn, proposed, revision="r1", **changes):
    args = dict(project="fixture", proposed_task_id=proposed, plan_revision=revision,
                intent_fingerprint="original", title="Original")
    args.update(changes)
    return materialize_task(conn, **args)


def test_claim_and_mapping_race_has_no_started_rewrite_or_ungated_claim(store):
    parent = make(store, "p")["task_id"]
    child = make(store, "q")["task_id"]
    make(store, "p", "r2")
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    barrier = threading.Barrier(2)

    def contender(mapping):
        with connect_closing(path) as conn:
            barrier.wait(timeout=10)
            if mapping:
                return make(conn, "q", "r2", intent_fingerprint="changed", title="Changed",
                            parent_proposed=["p"], maps_from="r1:q")
            try:
                return external_claim(conn, child, board="default", owner_host="fixture",
                    incarnation=dict(session="fixture", id="$1", created="1", pane="%1", pid="123",
                                     backend="claude", conversation="fixture"),
                    worktree="/fixture", reservation_id="fixture", reservation_token="a" * 64)
            except ValueError as exc:
                assert "ready" in str(exc) or "parents" in str(exc)
                return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        mapped, claimed = list(pool.map(contender, [True, False]))
    task = kb.get_task(store, child)
    assert task is not None
    if mapped["state"] == "mapped":
        assert claimed is None and task.status == "todo" and task.title == "Changed"
        assert kb.parent_ids(store, child) == [parent] and not kb.list_runs(store, child)
    else:
        assert mapped["reason"] == "mapping_card_started" and claimed["run_id"]
        assert task.status == "running" and task.title == "Original"
        assert kb.parent_ids(store, child) == []


def test_retirement_survives_reconnect_backfills_legacy_events_and_refuses_stale_aliases(store):
    tid = make(store, "x")["task_id"]
    make(store, "y", "r2", intent_fingerprint="changed", maps_from="r1:x")
    store.execute("DROP TABLE task_materialization_retirements")
    migrate_materializations(store)
    migrate_materializations(store)
    assert make(store, "x", "r3") == {
        "state": "conflict", "task_id": tid, "reason": "logical_id_retired"}
    assert make(store, "z", "r3", intent_fingerprint="another", maps_from="r1:x") == {
        "state": "conflict", "task_id": tid, "reason": "logical_id_retired"}
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    with connect_closing(path) as observer:
        assert make(observer, "x", "r2")["reason"] == "logical_id_retired"
        assert len(kb.list_tasks(observer)) == 1
