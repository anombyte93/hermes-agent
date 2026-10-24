"""The shared board selector works at every level of the Kanban CLI tree."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


ROOT = Path(__file__).resolve().parents[2]


def _build_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    kanban_parser = kc.build_parser(parser.add_subparsers(dest="command"))
    return parser, kanban_parser


def _required_arguments(parser):
    """Supply only required arguments without executing any command handlers."""
    tokens = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if action.option_strings:
            if not action.required:
                continue
            tokens.append(action.option_strings[0])
        elif action.nargs in ("?", "*"):
            continue
        value = str(next(iter(action.choices))) if action.choices else "1"
        count = action.nargs if isinstance(action.nargs, int) else 1
        tokens.extend([value] * count)
    return tokens


def _command_routes(parser, prefix=()):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Include aliases as separate invocations, not just canonical names.
            for name, child in action.choices.items():
                route = (*prefix, name)
                yield pytest.param(route, _required_arguments(child), id="-".join(route))
                yield from _command_routes(child, route)


@pytest.mark.parametrize("route,required", list(_command_routes(_build_parser()[1])))
def test_board_flag_positions_match_for_every_command_and_alias(route, required):
    parser, _ = _build_parser()
    before = parser.parse_args(["kanban", "--board", "alpha", *route, *required])
    assert before.board == "alpha"
    for boundary in range(1, len(route) + 1):
        for flag in (["--board", "alpha"], ["--board=alpha"]):
            after = parser.parse_args(
                ["kanban", *route[:boundary], *flag, *route[boundary:], *required]
            )
            assert vars(after) == vars(before)
    trailing = parser.parse_args(["kanban", *route, *required, "--board", "alpha"])
    assert vars(trailing) == vars(before)
    assert parser.parse_args(["kanban", *route, *required]).board is None


@pytest.mark.parametrize("tokens,expected", [
    (["--board", "alpha", "list", "--board", "beta"], "beta"),
    (["--board=alpha", "ls", "--board=alpha"], "alpha"),
    (["list", "--board", "alpha", "--board", "beta"], "beta"),
    (["--board", "alpha", "boards", "--board", "beta", "show"], "beta"),
    (["--board", "alpha", "boards", "--board", "beta", "show", "--board", "gamma"], "gamma"),
])
def test_last_explicit_board_wins(tokens, expected):
    parser, _ = _build_parser()
    assert parser.parse_args(["kanban", *tokens]).board == expected


@pytest.mark.parametrize("tokens", [["--board"], ["list", "--board"], ["boards", "show", "--board"]])
def test_board_flag_still_requires_a_value(tokens, capsys):
    parser, _ = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["kanban", *tokens])
    assert exc.value.code == 2
    assert "argument --board: expected one argument" in capsys.readouterr().err


@pytest.fixture
def board_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(name, raising=False)
    kb.create_board("alpha")
    kb.create_board("beta")
    for board in ("default", "alpha", "beta"):
        with kb.connect_closing(board=board) as conn:
            kb.create_task(conn, title=f"{board}-only")
    return home


@pytest.mark.parametrize("selection", [
    "default", "current", "env", "stale-env", "before", "after", "both", "db-pin",
])
def test_board_selection_uses_real_database(board_home, monkeypatch, capsys, selection):
    parser, _ = _build_parser()
    expected = "default"
    tokens = ["list", "--json"]
    if selection != "default":
        kb.set_current_board("beta")
        expected = "beta"
    if selection in {"env", "before", "after", "both", "db-pin"}:
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")
        expected = "alpha"
    if selection == "stale-env":
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "missing-board")
    elif selection == "before":
        tokens = ["--board", "beta", *tokens]
        expected = "beta"
    elif selection in {"after", "both", "db-pin"}:
        if selection == "both":
            tokens = ["--board", "alpha", *tokens]
        tokens.extend(["--board", "beta"])
        expected = "beta"
    if selection == "db-pin":
        monkeypatch.setenv("HERMES_KANBAN_DB", str(board_home / "kanban.db"))
        expected = "default"

    args = parser.parse_args(["kanban", *tokens])
    assert kc.kanban_command(args) == 0
    assert [task["title"] for task in json.loads(capsys.readouterr().out)] == [f"{expected}-only"]
    # The selector scopes this call, not future calls or the persisted pointer.
    assert kb.get_current_board() == (
        "alpha" if selection in {"env", "before", "after", "both", "db-pin"} else
        "default" if selection == "default" else "beta"
    )


@pytest.mark.parametrize("tokens", [
    ["--board", "alpha", "boards", "show"],
    ["boards", "--board", "alpha", "show"],
    ["boards", "show", "--board", "alpha"],
])
def test_board_management_keeps_ignoring_task_routing_override(board_home, capsys, tokens):
    kb.set_current_board("beta")
    parser, _ = _build_parser()
    assert kc.kanban_command(parser.parse_args(["kanban", *tokens])) == 0
    assert capsys.readouterr().out.splitlines()[0] == "Current board: beta"
    assert kb.get_current_board() == "beta"


@pytest.mark.parametrize("argument", ["board", "--board", "--destination"])
def test_existing_subcommand_board_argument_is_preserved(argument):
    parser = argparse.ArgumentParser()
    parser.add_argument("--board", default=None)
    child = parser.add_subparsers(dest="action").add_parser("local")
    kwargs = {"choices": ["local"], "default": "local"}
    if argument == "--destination":
        kwargs["dest"] = "board"
    child.add_argument(argument, **kwargs)
    before = vars(parser.parse_args(["local", *(["local"] if argument == "board" else [])]))

    kc._add_subcommand_board_arguments(parser, "Shared board selector")

    assert vars(parser.parse_args(["local", *(["local"] if argument == "board" else [])])) == before
    if argument.startswith("--"):
        assert parser.parse_args(["--board", "alpha", "local", argument, "local"]).board == "local"
        with pytest.raises(SystemExit):
            parser.parse_args(["local", argument, "alpha"])
    else:
        assert parser.parse_args(["--board", "alpha", "local", "local"]).board == "local"


def test_real_cli_and_slash_share_board_selector(board_home):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for tokens in (
        ["--board", "alpha", "list", "--json"],
        ["list", "--json", "--board", "alpha"],
    ):
        result = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "kanban", *tokens],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert [task["title"] for task in json.loads(result.stdout)] == ["alpha-only"]
        assert [task["title"] for task in json.loads(kc.run_slash(" ".join(tokens)))] == ["alpha-only"]
