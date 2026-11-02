"""Revision and projected-plan contracts from parent rulings 17 and 19."""
import argparse
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as cli, kanban_db as kb
from hermes_cli import kanban_db_materialize as api
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_external import external_claim
from plugins.kanban.dashboard.plugin_api import router


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    with connect_closing(board="default") as conn:
        yield conn


def row(proposed="q", fingerprint="A", title="A", **changes):
    result = dict(proposed_task_id=proposed, intent_fingerprint=fingerprint, title=title)
    result.update(changes)
    return result


def write(conn, revision="r1", **changes):
    return api.materialize_task(conn, project="fixture", plan_revision=revision, **row(**changes))


def claim(conn, tid):
    return external_claim(conn, tid, board="default", owner_host="fixture",
        incarnation=dict(session="owner", id="$1", created="1", pane="%1", pid="123",
                         backend="claude", conversation="fixture"),
        worktree=str(Path.home()), reservation_id="fixture", reservation_token="a" * 64)


def invoke(capsys, action, *, board="default", revision="r1", options=None, extra=()):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    r = row(**(options or {}))
    args = ["kanban", "--board", board, action, "--project=fixture", "--plan-revision=" + revision,
            "--proposed-id=" + r["proposed_task_id"], "--fingerprint=" + r["intent_fingerprint"],
            "--title=" + r["title"], "--execution-kind=external_session", "--json", *extra]
    if r.get("maps_from"):
        args.append("--maps-from=" + r["maps_from"])
    args.extend("--parent-proposed=" + p for p in r.get("parent_proposed", ()))
    code = cli.kanban_command(parser.parse_args(args))
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    return code, json.loads(output.out)


@pytest.mark.parametrize("middle,last", [("f" * 64, "0" * 64), ("0" * 64, "f" * 64)])
@pytest.mark.parametrize("mapping,started,reason", [(False, False, "revision_requires_mapping"),
    (True, False, None), (True, True, "mapping_card_started")])
def test_going_back_uses_latest_row_not_any_hash(store, monkeypatch, capsys, middle, last, mapping, started, reason):
    monkeypatch.setattr(api.time, "time", lambda: 1000)
    parent = write(store, proposed="p")["task_id"]
    tid = write(store, revision=last, parent_proposed=[])["task_id"]
    write(store, revision=middle, fingerprint="B", title="B", maps_from=last + ":q")
    if started:
        claim(store, tid)
    write(store, revision="third", proposed="p")
    request = row(parent_proposed=["p"], maps_from=middle + ":q" if mapping else None)
    before = "\n".join(store.iterdump())
    checked = invoke(capsys, "materialize-check", revision="third", options=request)
    assert "\n".join(store.iterdump()) == before
    written = invoke(capsys, "materialize", revision="third", options=request)
    assert written == checked
    if reason:
        assert written == (3, {"state": "conflict", "task_id": tid, "reason": reason})
        assert "\n".join(store.iterdump()) == before
    else:
        assert written == (0, {"state": "mapped", "task_id": tid})
        assert kb.get_task(store, tid).title == "A"
        assert kb.parent_ids(store, tid) == [parent]
        with pytest.raises(ValueError, match="ready|parents"):
            claim(store, tid)
        event_count = len(kb.list_events(store, tid))
        assert invoke(capsys, "materialize", revision="third", options=request) == (
            0, {"state": "existing", "task_id": tid})
        assert len(kb.list_events(store, tid)) == event_count
        # A valid map onto the same card with the latest fingerprint is existing.
        assert write(store, revision="fourth", maps_from="third:q")["state"] == "existing"


def test_unchanged_fingerprint_never_ignores_other_card_mapping(store, capsys):
    write(store, proposed="x")
    tid = write(store, proposed="y")["task_id"]
    before = "\n".join(store.iterdump())
    for action in ("materialize-check", "materialize"):
        assert invoke(capsys, action, revision="r2", options=row(proposed="y", maps_from="r1:x")) == (
            3, {"state": "conflict", "task_id": tid, "reason": "logical_id_retired"})
    assert "\n".join(store.iterdump()) == before


@pytest.mark.parametrize("shape", ["keep-rename", "rename-keep", "two-renames", "two-renames-reverse", "reverse-edge", "true-cycle", "revert"])
def test_projected_rows_match_writer_and_do_not_write(store, shape):
    write(store, proposed="x")
    write(store, proposed="a")
    write(store, proposed="b", parent_proposed=["a"])
    plans = {
        "keep-rename": [row(proposed="n"), row(proposed="x"), row(proposed="y", maps_from="r1:x")],
        "rename-keep": [row(proposed="n"), row(proposed="y", maps_from="r1:x"), row(proposed="x")],
        "two-renames": [row(proposed="n"), row(proposed="y", maps_from="r1:x"), row(proposed="z", maps_from="r1:x")],
        "two-renames-reverse": [row(proposed="n"), row(proposed="z", maps_from="r1:x"), row(proposed="y", maps_from="r1:x")],
        "reverse-edge": [row(proposed="b", fingerprint="B", maps_from="r1:b"), row(proposed="a", fingerprint="B", maps_from="r1:a", parent_proposed=["b"])],
        "true-cycle": [row(proposed="b"), row(proposed="a", fingerprint="B", maps_from="r1:a", parent_proposed=["b"])],
        "revert": [row(proposed="x", fingerprint="B", maps_from="r1:x"), row(proposed="x")],
    }
    before, changes = "\n".join(store.iterdump()), store.total_changes
    checked = []
    prior = []
    for request in plans[shape]:
        try:
            checked.append(api.check_materialization(store, project="fixture", plan_revision="r2",
                prior_rows=prior, **request))
        except ValueError as exc:
            checked.append({"refused": str(exc)})
        prior.append(request)
    assert "\n".join(store.iterdump()) == before and store.total_changes == changes
    with sqlite3.connect(":memory:", isolation_level=None) as writer:
        writer.row_factory = sqlite3.Row
        store.backup(writer)
        actual = []
        for request in plans[shape]:
            try:
                result = api.materialize_task(writer, project="fixture", plan_revision="r2", **request)
                if result["state"] == "created":
                    result["task_id"] = None
                actual.append(result)
            except ValueError as exc:
                actual.append({"refused": str(exc)})
    assert checked == actual
    if "rename" in shape:
        assert checked[-1]["reason"] == "logical_id_retired"
    elif shape == "reverse-edge":
        assert [r["state"] for r in checked] == ["mapped", "mapped"]
    elif shape == "true-cycle":
        assert checked[-1] == {"refused": "mapping_dependency_cycle"}


def test_cli_projection_file_and_invalid_pending_parent(store, tmp_path, capsys):
    write(store, proposed="a")
    parent = write(store, proposed="b", parent_proposed=["a"])["task_id"]
    prior = tmp_path / "earlier.json"
    prior.write_text(json.dumps([row(proposed="b", fingerprint="B", maps_from="r1:b")]))
    request = row(proposed="a", fingerprint="B", maps_from="r1:a", parent_proposed=["b"])
    before = "\n".join(store.iterdump())
    assert invoke(capsys, "materialize-check", revision="r2", options=request,
        extra=("--prior-rows-file", str(prior), "--pending-parent-proposed=b=" + parent))[0] == 0
    assert invoke(capsys, "materialize-check", revision="r2", options=row(parent_proposed=["missing"]),
        extra=("--pending-parent-proposed=missing=t_absent",)) == (
            1, {"state": "refused", "reason": "pending_parent_not_found"})
    assert "\n".join(store.iterdump()) == before


@pytest.mark.parametrize("started", [False, True])
def test_mapping_titles_normalise_like_created_titles(store, started):
    tid = write(store, title=" spaced ")["task_id"]
    assert kb.get_task(store, tid).title == "spaced"
    if started:
        claim(store, tid)
    assert write(store, revision="r2", title=" spaced ", fingerprint="B", maps_from="r1:q")["state"] == "mapped"
    assert kb.get_task(store, tid).title == "spaced"


def test_mapping_inherits_parent_notifications(store):
    from hermes_cli.kanban_db_notify import add_notify_sub, list_notify_subs
    parent = write(store, proposed="p")["task_id"]
    child = write(store)["task_id"]
    add_notify_sub(store, task_id=parent, platform="telegram", chat_id="fixture")
    write(store, revision="r2", proposed="p")
    write(store, revision="r2", fingerprint="B", maps_from="r1:q", parent_proposed=["p"])
    assert list_notify_subs(store, task_id=child)


def test_delete_route_and_bulk_archive_keep_identity_but_continue(store, capsys):
    tid = write(store)["task_id"]
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app, raise_server_exceptions=False).delete("/tasks/" + tid)
    assert response.status_code == 400, response.text
    assert "materialized_card_retained" in response.text
    plain = kb.create_task(store, title="plain")
    assert kb.archive_task(store, plain) and kb.archive_task(store, tid)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    code = cli.kanban_command(parser.parse_args(["kanban", "archive", "--rm", tid, plain]))
    output = capsys.readouterr()
    assert code != 0 and "materialized_card_retained" in output.out + output.err
    assert plain in output.out + output.err and kb.get_task(store, plain) is None
    assert kb.get_task(store, tid) is not None
    assert store.execute("SELECT 1 FROM task_materializations WHERE task_id=?", (tid,)).fetchone()


@pytest.mark.parametrize("revision,fingerprint,state", [("r2", "A", "existing"),
    ("r3", "A", "existing"), ("r3", "B", "mapped")])
def test_renamed_mapping_replay_matches_check_and_writer(store, capsys, revision, fingerprint, state):
    tid = write(store, proposed="x")["task_id"]
    write(store, revision="r2", proposed="y", maps_from="r1:x")
    request = row(proposed="y", fingerprint=fingerprint, maps_from="r1:x")
    before, changes = "\n".join(store.iterdump()), store.total_changes
    expected = (0, {"state": state, "task_id": tid})
    assert invoke(capsys, "materialize-check", revision=revision, options=request) == expected
    assert "\n".join(store.iterdump()) == before and store.total_changes == changes
    assert invoke(capsys, "materialize", revision=revision, options=request) == expected
    assert len(kb.list_tasks(store)) == 1
    assert store.execute("SELECT 1 FROM task_materializations WHERE plan_revision=? AND proposed_task_id='y'", (revision,)).fetchone()
    assert sum(e.kind == "materialization_mapped" for e in kb.list_events(store, tid)) == (2 if state == "mapped" else 1)
    for action in ("materialize-check", "materialize"):
        assert invoke(capsys, action, options=row(proposed="x")) == (
            3, {"state": "conflict", "task_id": tid, "reason": "logical_id_retired"})


def test_partial_rename_plan_finishes_on_retry_and_chain_replays(store, capsys, tmp_path, monkeypatch):
    tid = write(store, proposed="x")["task_id"]
    renamed = row(proposed="y", maps_from="r1:x")
    assert invoke(capsys, "materialize", revision="r2", options=renamed)[1]["state"] == "mapped"
    child = row(proposed="z", parent_proposed=["y"])
    # A valid second row can fail after preflight. Retry the identical plan,
    # not changed intent disguised as a replay of the same revision.
    before = "\n".join(store.iterdump())
    assert invoke(capsys, "materialize-check", revision="r2", options=child) == (0, {"state": "created", "task_id": None})

    def unavailable(*args, **kwargs):
        raise ValueError("fixture_write_failed")

    with monkeypatch.context() as failure:
        failure.setattr(kb, "create_task", unavailable)
        assert invoke(capsys, "materialize", revision="r2", options=child) == (
            1, {"state": "refused", "reason": "fixture_write_failed"})
    assert "\n".join(store.iterdump()) == before
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps([renamed]))
    assert invoke(capsys, "materialize-check", revision="r2", options=renamed) == (0, {"state": "existing", "task_id": tid})
    assert invoke(capsys, "materialize-check", revision="r2", options=child,
        extra=("--prior-rows-file", str(prior))) == (0, {"state": "created", "task_id": None})
    assert "\n".join(store.iterdump()) == before
    assert invoke(capsys, "materialize", revision="r2", options=renamed)[1]["state"] == "existing"
    created = invoke(capsys, "materialize", revision="r2", options=child)[1]["task_id"]
    assert kb.parent_ids(store, created) == [tid]
    # Another project makes the x -> y -> z chain a single canonical card.
    for revision, proposed, mapping in [("r1", "x", None), ("r2", "y", "r1:x"), ("r3", "z", "r2:y")]:
        request = dict(project="chain", plan_revision=revision, **row(proposed=proposed, maps_from=mapping))
        expected = api.check_materialization(store, **request)
        result = api.materialize_task(store, **request)
        assert result["state"] == expected["state"]
        assert api.check_materialization(store, **request) == api.materialize_task(store, **request) == {"state": "existing", "task_id": result["task_id"]}
    assert store.execute("SELECT count(DISTINCT task_id) FROM task_materializations WHERE project='chain'").fetchone()[0] == 1


def test_rename_chain_replays_one_card(store):
    tid = write(store, proposed="x")["task_id"]
    for revision, proposed, mapping in [("r2", "y", "r1:x"), ("r3", "z", "r2:y")]:
        request = dict(project="fixture", plan_revision=revision, **row(proposed=proposed, maps_from=mapping))
        assert api.materialize_task(store, **request) == {"state": "mapped", "task_id": tid}
        assert api.check_materialization(store, **request) == {"state": "existing", "task_id": tid}
        assert api.materialize_task(store, **request) == {"state": "existing", "task_id": tid}
    assert len(kb.list_tasks(store)) == 1


@pytest.mark.parametrize("revision,fingerprint", [("r2", "A"), ("r3", "A"), ("r3", "B")])
def test_projected_rename_retry_matches_writer_copy(store, revision, fingerprint):
    write(store, proposed="x")
    write(store, revision="r2", proposed="y", maps_from="r1:x")
    plan = [row(proposed="y", fingerprint=fingerprint, maps_from="r1:x"), row(proposed="z", parent_proposed=["y"])]
    before, changes = "\n".join(store.iterdump()), store.total_changes
    with sqlite3.connect(":memory:", isolation_level=None) as writer:
        writer.row_factory = sqlite3.Row
        store.backup(writer)
        for index, request in enumerate(plan):
            checked = api.check_materialization(store, project="fixture", plan_revision=revision,
                prior_rows=plan[:index], **request)
            written = api.materialize_task(writer, project="fixture", plan_revision=revision, **request)
            if written["state"] == "created":
                written["task_id"] = None
            assert checked == written
            assert checked["state"] == ("created" if index else "mapped" if fingerprint == "B" else "existing")
    assert "\n".join(store.iterdump()) == before and store.total_changes == changes


@pytest.mark.parametrize("prior", ["bad", {}, [dict(proposed_task_id="n", intent_fingerprint="A", title="N", unknown=True)],
    [dict(proposed_task_id="n", intent_fingerprint="A", title="N", parent_proposed="missing")],
    [dict(proposed_task_id="n", intent_fingerprint="A", title="N", parent_proposed=[1])]])
def test_invalid_prior_rows_refuse_cleanly_without_writes(store, capsys, tmp_path, prior):
    file = tmp_path / "prior.json"
    file.write_text(json.dumps(prior))
    before = "\n".join(store.iterdump())
    assert invoke(capsys, "materialize-check", extra=("--prior-rows-file", str(file))) == (
        1, {"state": "refused", "reason": "invalid_prior_rows"})
    assert "\n".join(store.iterdump()) == before


def test_projected_rename_can_replay_before_any_write(store):
    tid = write(store, proposed="x")["task_id"]
    request = row(proposed="y", maps_from="r1:x")
    before, changes = "\n".join(store.iterdump()), store.total_changes
    assert api.check_materialization(store, project="fixture", plan_revision="r2", prior_rows=[request],
        **request) == {"state": "existing", "task_id": tid}
    assert "\n".join(store.iterdump()) == before and store.total_changes == changes
    assert api.materialize_task(store, project="fixture", plan_revision="r2", **request)["state"] == "mapped"
    assert api.materialize_task(store, project="fixture", plan_revision="r2", **request) == {"state": "existing", "task_id": tid}


def test_pending_current_reference_is_not_an_output_id(store, capsys, tmp_path):
    file = tmp_path / "prior.json"
    file.write_text(json.dumps([row(proposed="n")]))
    before = "\n".join(store.iterdump())
    assert invoke(capsys, "materialize-check", revision="r2", options=row(proposed="n"),
        extra=("--prior-rows-file", str(file))) == (0, {"state": "existing", "task_id": None})
    assert "\n".join(store.iterdump()) == before


@pytest.mark.parametrize("shape", ["fingerprint", "mapping"])
def test_duplicate_prior_conflict_never_leaks_pending_id_or_gets_swallowed(store, capsys, tmp_path, shape):
    write(store, proposed="x")
    second = row(proposed="n", fingerprint="B") if shape == "fingerprint" else row(proposed="n", maps_from="r1:x")
    file = tmp_path / "prior.json"
    file.write_text(json.dumps([row(proposed="n"), second]))
    before = "\n".join(store.iterdump())
    code, result = invoke(capsys, "materialize-check", revision="r2", extra=("--prior-rows-file", str(file)))
    assert code == 3
    assert result == {"state": "conflict", "task_id": None,
        "reason": "materialization_conflict" if shape == "fingerprint" else "logical_id_retired"}
    assert "pending:" not in json.dumps(result)
    assert "\n".join(store.iterdump()) == before


def test_corrupt_check_is_json_and_preserves_bytes(store, capsys):
    path = Path(store.execute("PRAGMA database_list").fetchone()[2])
    store.close()
    path.write_bytes(b"not a database" * 100)
    before = path.read_bytes()
    assert invoke(capsys, "materialize-check") == (1, {"state": "refused", "reason": "board_unreadable"})
    assert path.read_bytes() == before


@pytest.mark.parametrize("action", ["materialize", "materialize-check", "list"])
def test_board_pin_mismatch_refuses_before_any_write(store, monkeypatch, capsys, action):
    kb.create_board("testb")
    kb.create_board("otherb")
    pin = kb.board_dir("otherb") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    if action == "list":
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers(dest="command"))
        code = cli.kanban_command(parser.parse_args(["kanban", "--board=testb", "list", "--json"]))
        result = json.loads(capsys.readouterr().out)
    else:
        code, result = invoke(capsys, action, board="testb")
    assert (code, result) == (1, {"state": "refused", "reason": "board_pin_mismatch"})
    for board in ("testb", "otherb"):
        with connect_closing(kb.board_dir(board) / "kanban.db") as conn:
            assert not kb.list_tasks(conn)


@pytest.mark.parametrize("board,symlink", [("default", False), ("otherb", False), ("otherb", True)])
def test_matching_board_pin_and_implicit_worker_board_work(store, monkeypatch, tmp_path, capsys, board, symlink):
    if board != "default":
        kb.create_board(board)
    path = kb.kanban_db_path(board)
    if symlink:
        alias = tmp_path / "alias.db"
        alias.symlink_to(path)
        path = alias
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    assert invoke(capsys, "materialize", board=board)[0] == 0
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    assert cli.kanban_command(parser.parse_args(["kanban", "list", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)
