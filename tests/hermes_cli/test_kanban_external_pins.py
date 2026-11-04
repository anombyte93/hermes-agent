"""External reservation board assertions cannot relabel a custom DB pin."""
import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as cli, kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_db_external as ext

IDENTITY = dict(session="fixture", id="$1", created="1", pane="%1", pid="123",
                backend="claude", conversation="fixture")


@pytest.mark.parametrize("action", ["claim", "handover"])
def test_explicit_external_custom_pin_refuses_before_database_initialisation(tmp_path, monkeypatch, capsys, action):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    kb.create_board("x")
    pin = tmp_path / "must-not-create.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    token = tmp_path / "token"
    token.write_text("a" * 64)
    args = ["kanban", "--board", "x", "external", action, "--task", "t_fixture", "--expected-board", "x",
            "--owner-host", "fixture", "--incarnation", json.dumps(IDENTITY), "--worktree", str(tmp_path),
            "--reservation-id", "fixture", "--reservation-token-file", str(token), "--json"]
    if action == "handover":
        args += ["--run-id", "1", "--new-reservation-token-file", str(token), "--evidence", "proof"]
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    assert cli.kanban_command(parser.parse_args(args)) == 1
    assert json.loads(capsys.readouterr().out) == {"state": "refused", "reason": "board_pin_mismatch"}
    assert not pin.exists()



@pytest.mark.parametrize("action", ["claim", "handover"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_external_pin_requires_real_board_file(tmp_path, monkeypatch, capsys, action, explicit, custom):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    kb.create_board("x")
    with dbc.connect_closing(board="x") as conn:
        tid = kb.create_task(conn, title="fixture", execution_kind="external_session")
        run_id = None
        if action == "handover":
            run_id = ext.external_claim(conn, tid, board="x", owner_host="fixture", incarnation=IDENTITY,
                worktree=str(tmp_path), reservation_id="first", reservation_token="a" * 64)["run_id"]
        pin = tmp_path / "custom.db" if custom else kb.kanban_db_path("x")
        if custom:
            with sqlite3.connect(pin) as copy:
                conn.backup(copy)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "x")
    token = tmp_path / "token"
    token.write_text("a" * 64)
    successor = tmp_path / "next-token"
    successor.write_text("b" * 64)
    args = ["kanban", *(["--board", "x"] if explicit else []), "external", action,
            "--task", tid, "--expected-board", "x", "--owner-host", "fixture",
            "--incarnation", json.dumps({**IDENTITY, "pid": "456"}), "--worktree", str(tmp_path),
            "--reservation-id", "second", "--reservation-token-file", str(token), "--json"]
    if action == "handover":
        args += ["--run-id", str(run_id), "--new-reservation-token-file", str(successor), "--evidence", "proof"]
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    with dbc.connect_closing(pin) as observer:
        before = "\n".join(observer.iterdump())
        code = cli.kanban_command(parser.parse_args(args))
        output = capsys.readouterr()
        assert "Traceback" not in output.err
        result = json.loads(output.out)
        if custom:
            assert (code, result) == (1, {"state": "refused", "reason": "board_pin_mismatch"})
            assert "\n".join(observer.iterdump()) == before
        else:
            assert code == 0 and result["binding"]["state"] == "active"
            assert result["binding"]["reservation_id"] == "second"
