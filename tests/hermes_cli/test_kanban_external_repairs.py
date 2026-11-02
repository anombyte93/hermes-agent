"""Parent additions: external-session edges remain non-worker-owned."""
import argparse
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban as cli, kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_db_external as ext
from hermes_cli.kanban_execution import execution_kind_of

TOKEN = "a" * 64
IDENTITY = dict(session="fixture", id="$1", created="1", pane="%1", pid="123",
                backend="claude", conversation="fixture")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: True)
    monkeypatch.setattr(dispatch, "_system_memory_sample", lambda: {})
    with dbc.connect_closing(board="default") as conn:
        yield conn


def create(conn, **kw):
    return kb.create_task(conn, title="original", body="original body", execution_kind="external_session", **kw)


def claim(conn, tid, **kw):
    args = dict(board="default", owner_host="fixture", incarnation=IDENTITY, worktree=str(Path.home()),
                reservation_id="X", reservation_token=TOKEN)
    args.update(kw)
    return ext.external_claim(conn, tid, **args)


def handover(conn, tid, run, evidence="proof"):
    return ext.external_handover(conn, tid, run, reservation_token=TOKEN, board="default",
        owner_host="fixture", incarnation={**IDENTITY, "pid": "456", "conversation": "successor"},
        worktree=str(Path.home()), reservation_id="Y", new_reservation_token="b" * 64, evidence=evidence)


def test_reservation_cannot_move_cards_on_same_board_but_new_or_other_board_works(store):
    a, b = create(store), create(store)
    run = claim(store, a)["run_id"]
    ext.external_complete(store, a, run, reservation_token=TOKEN)
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="reservation_conflict"):
        claim(store, b)
    assert "\n".join(store.iterdump()) == before
    assert claim(store, b, reservation_id="Y")["binding"]["state"] == "active"
    kb.create_board("other")
    with dbc.connect_closing(board="other") as other:
        assert claim(other, create(other), board="other")["binding"]["state"] == "active"


@pytest.mark.parametrize("route", ["kernel", "cli"])
def test_external_pr_contract_creation_refuses_without_rows(store, capsys, route):
    before = "\n".join(store.iterdump())
    contract = "https://github.com/o/r/pull/1"
    if route == "kernel":
        with pytest.raises(ValueError, match="external_session_refuses_completion_contract"):
            create(store, completion_contract=contract)
    else:
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["kanban", "create", "rejected", "--execution-kind", "external_session",
                                  "--completion-contract", contract, "--json"])
        assert cli.kanban_command(args) != 0
        output = capsys.readouterr()
        assert "external_session_refuses_completion_contract" in output.out + output.err
    assert "\n".join(store.iterdump()) == before


def test_external_block_loop_stays_blocked(store):
    tid = create(store)
    for _ in range(3):
        assert kb.block_task(store, tid, reason="same", kind="needs_input")
        assert kb.get_task(store, tid).status == "blocked"
        assert kb.unblock_task(store, tid)


@pytest.mark.parametrize("route", ["kernel", "specify", "decompose"])
def test_legacy_external_triage_refuses_before_aux_and_keeps_intent(store, monkeypatch, route):
    from hermes_cli import kanban_specify as specify, kanban_decompose as decompose
    tid = create(store)
    store.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    before = "\n".join(store.iterdump())
    spy = Mock(return_value=(None, "fixture"))
    monkeypatch.setattr(specify, "_call_aux", spy)
    monkeypatch.setattr(decompose, "_call_aux", spy)
    monkeypatch.setattr(decompose, "_load_routing", lambda: decompose._Routing("fixture", "fixture", False, [], {"fixture"}))
    if route == "kernel":
        with pytest.raises(ValueError, match="external_session_refuses_triage"):
            kb.specify_triage_task(store, tid, title="changed", body="changed")
    else:
        outcome = specify.specify_task(tid) if route == "specify" else decompose.decompose_task(tid)
        spy.assert_not_called()
        assert not outcome.ok and "external_session_refuses_triage" in outcome.reason
    spy.assert_not_called()
    assert "\n".join(store.iterdump()) == before
    # Legacy rows can be explicitly archived; no automatic specification/rewrite.
    assert kb.archive_task(store, tid)
    assert kb.get_task(store, tid).status == "archived"


@pytest.mark.parametrize("route", ["create", "setter"])
def test_external_reasoning_refuses_without_writes(store, route):
    tid = create(store)
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="external_session.*reasoning"):
        create(store, reasoning_effort="high") if route == "create" else kb.set_reasoning_effort(store, tid, "high")
    assert "\n".join(store.iterdump()) == before


@pytest.mark.parametrize("action,outcome", [("release", "released"), ("handover", "handed_over")])
def test_closed_external_attempt_status_is_released(store, action, outcome):
    tid = create(store)
    run = claim(store, tid)["run_id"]
    if action == "release":
        ext.external_release(store, tid, run, reservation_token=TOKEN, reason="pause")
    else:
        handover(store, tid, run)
    old = kb.get_run(store, run)
    assert old.status == "released" and old.outcome == outcome


def test_external_assignee_does_not_consume_worker_slot(store):
    tid = create(store, assignee="fixture")
    claim(store, tid)
    assert dispatch.count_running_by_assignee(store) == {}
    worker = kb.create_task(store, title="worker", assignee="fixture")
    assert kb.claim_task(store, worker, claimer="fixture:1")
    assert dispatch.count_running_by_assignee(store) == {"fixture": 1}


def test_unreadable_execution_kind_never_defaults_to_worker():
    with sqlite3.connect(":memory:") as conn:
        # No tasks table: exercise DatabaseError alone, not the missing-row arm.
        assert execution_kind_of(conn, "unknown") is None


@pytest.mark.parametrize("evidence", ["", "   "])
def test_empty_handover_evidence_keeps_old_binding_and_attempt(store, evidence):
    tid = create(store)
    run = claim(store, tid)["run_id"]
    before = "\n".join(store.iterdump())
    with pytest.raises(ValueError, match="handover evidence is required"):
        handover(store, tid, run, evidence=evidence)
    assert "\n".join(store.iterdump()) == before
    assert ext.external_status(store, tid)["binding"]["state"] == "active"
