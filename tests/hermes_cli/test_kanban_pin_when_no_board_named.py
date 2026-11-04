"""Round 9: the ``HERMES_KANBAN_DB`` pin keeps its base precedence in the
resolver (highest, beats every board source, named or not); only ownership
decisions open a named board's own store, through ``_own_store_path``. The
root pins behave as at base.

The dispatcher pins ``HERMES_KANBAN_DB`` (and the workspaces/attachments
roots) into every worker's environment so the worker's own board still
resolves after ``hermes -p`` rewrites ``HERMES_HOME``. Rounds 7 and 8 moved a
named-board rule into the shared resolver, which broke 37 pre-existing tests
and the documented precedence; round 9 restored the resolver to base and
keeps the S6/S7 guarantees at the ownership sites. The resolver contract:

* ``board=None`` reads the pinned store, whatever the active board is —
  the pin's whole purpose;
* a NAMED board (``default`` included) also reads the pin through the
  resolver, exactly as at base;
* the ``HERMES_KANBAN_WORKSPACES_ROOT`` / ``HERMES_KANBAN_ATTACHMENTS_ROOT``
  pins behave exactly as at base: the pin wins, for named boards too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as cli, kanban_db as kb, kanban_db_connect as kbc

_PIN_VARS = ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
             "HERMES_KANBAN_ATTACHMENTS_ROOT")
_ALL_VARS = _PIN_VARS + ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME")


def _clear_pins(monkeypatch):
    for key in _ALL_VARS:
        monkeypatch.delenv(key, raising=False)


def _open_store(board) -> Path:
    conn = kbc.connect(board=board)
    try:
        return Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Home with boards ``other`` and ``board-c`` plus a distinct pinned store
    C (not any board's own store). Returns ``(home, pin)``; the pin is NOT yet
    set — each test sets it explicitly."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _clear_pins(monkeypatch)
    from hermes_cli import kanban_db_workspace as kbw
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *a, **k: None)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board("other")
    kb.create_board("board-c")
    pin = tmp_path / "worker-pin" / "kanban.db"
    pin.parent.mkdir(parents=True)
    kb.init_db(db_path=pin)
    with kbc.connect_closing(db_path=pin) as conn:
        kb.create_task(conn, title="pinned card", assignee="fixture")
    with kbc.connect_closing(board="other") as conn:
        kb.create_task(conn, title="other's card", assignee="fixture")
    with kbc.connect_closing() as conn:  # default's real store
        kb.create_task(conn, title="default's card", assignee="fixture")
        kb.create_task(conn, title="default's second card", assignee="fixture")
    kb._INITIALIZED_PATHS.clear()
    return home, pin


def _pinned_task_id(pin: Path) -> str:
    with kbc.connect_closing(db_path=pin) as conn:
        return kb.list_tasks(conn)[0].id


# --- Defect 1: board=None opens the pin whatever the ACTIVE board is --------


def test_board_none_uses_the_pin_with_a_named_active_board_from_env(world, monkeypatch):
    """Active board set by HERMES_KANBAN_BOARD (a NON-default board) is not
    "named": a board=None open still reads the pinned store."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    assert kb.kanban_db_path() == pin
    assert _open_store(None) == pin.resolve()


def test_board_none_uses_the_pin_with_a_named_active_board_from_the_current_file(world, monkeypatch):
    """The same, with the active board set the way another session's
    ``boards switch other`` leaves it: the global current-board file."""
    home, pin = world
    kb.set_current_board("other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    assert kb.kanban_db_path() == pin
    assert _open_store(None) == pin.resolve()


def test_board_none_uses_the_pin_with_a_named_active_board_from_cli_board_flag(world, monkeypatch):
    """The same, with the active board set the way the CLI's ``--board other``
    sets it for the duration of a call: a scoped current-board override."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    with kb.scoped_current_board("other"):
        assert kb.kanban_db_path() == pin
        assert _open_store(None) == pin.resolve()


def test_board_none_kanban_tool_call_reads_the_pin(world, monkeypatch):
    """A worker tool call with no board (tools/kanban_tools.py ``_board(None)``)
    reads the pinned store even when the active board is named — the exact
    shape of a dispatched worker whose dispatcher runs another board."""
    from tools import kanban_tools as kt
    home, pin = world
    pinned_tid = _pinned_task_id(pin)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = kt._handle_show({"task_id": pinned_tid, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == pinned_tid, "tool call with board=None missed the pin"


def test_board_none_one_cli_verb_reads_the_pin(world, monkeypatch, capsys):
    """One CLI verb (``kanban list --json``) with no ``--board`` reads the
    pinned store even when the active board is a named board."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "list", "--json"])
    assert cli.kanban_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    titles = {t["title"] for t in payload}
    assert "pinned card" in titles, titles
    assert "other's card" not in titles, titles


def test_disagreeing_roots_worker_finds_its_own_card(world, monkeypatch):
    """The pin's stated purpose (dispatcher pins the worker's board into an env
    whose HERMES_HOME disagrees): the worker's board=None tools find the
    worker's own card through the pin, not whatever the local roots say."""
    from tools import kanban_tools as kt
    home, pin = world
    pinned_tid = _pinned_task_id(pin)
    # root-b has its own boards and its own current board — a root that
    # disagrees with the dispatcher's. The worker must still find its own
    # card through the pin.
    tmp = home.parent
    root_b = tmp / "root-b"
    (root_b / ".hermes").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(root_b / ".hermes"))
    monkeypatch.setenv("HOME", str(root_b))
    monkeypatch.setattr(Path, "home", lambda: root_b)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    import hermes_constants
    saved = hermes_constants._default_hermes_root_memo
    hermes_constants._default_hermes_root_memo = None
    try:
        # root-b's own "other" board exists and is current there, so the
        # active-board fallback resolves to IT, not to the pin.
        kb.create_board("other")
        with kbc.connect_closing(board="other") as conn:
            kb.create_task(conn, title="root-b local card", assignee="fixture")
        out = kt._handle_show({"task_id": pinned_tid, "board": None})
    finally:
        hermes_constants._default_hermes_root_memo = saved
        kb._INITIALIZED_PATHS.clear()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HOME", str(tmp))
    d = json.loads(out)
    assert d["task"]["id"] == pinned_tid, (
        "disagreeing roots: board=None did not find the worker's own card through the pin")


# --- Defect 2 (round 9): the named-default rule moved to the helper --------


def test_named_default_resolver_keeps_base_the_pin_wins(world, monkeypatch):
    """Round 9: ``_board_path`` is base again — a NAMED ``default`` resolves
    to the pin (``HERMES_KANBAN_DB`` has the highest precedence, as at base
    and as environment-variables.md documents). The named-default rule lives
    only in ``_own_store_path``."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    assert kb.kanban_db_path(board="default") == pin
    assert _open_store("default") == pin.resolve()


def test_own_store_path_default_is_the_pin_else_the_real_store(world, monkeypatch):
    """``_own_store_path('default')``: the pin when one is set (it re-anchors
    the legacy default layout, as at base), else ``kanban_home()/kanban.db``."""
    home, pin = world
    real_default = home / "kanban.db"
    assert kb._own_store_path("default") == real_default
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    assert kb._own_store_path("default") == pin
    link = home / "pin-link.db"
    link.symlink_to(real_default)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(link))
    assert kb._own_store_path("default") == link


def test_export_board_default_explicit_keeps_base_the_pin_re_anchors_it(world, monkeypatch, tmp_path):
    """Ruling, default arm: ``_own_store_path('default')`` is the pin when one
    is set (the pin re-anchors the legacy default layout, as at base), so an
    explicit ``export_board('default')`` under a foreign pin exports the PIN's
    cards (1), exactly as at base — default is never diverted to its real
    store by the helper."""
    from hermes_cli import kanban_transfer as kt
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    result = kt.export_board("default", str(tmp_path / "default.tar.gz"))
    assert result["counts"]["tasks"] == 1, result


def test_export_board_default_under_a_pin_to_its_real_store_exports_that_store(world, monkeypatch, tmp_path):
    """``export_board('default')`` under a pin to default's own real store
    exports that store."""
    from hermes_cli import kanban_transfer as kt
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    result = kt.export_board("default", str(tmp_path / "default.tar.gz"))
    assert result["counts"]["tasks"] == 2, result


def test_export_board_no_slug_keeps_base_the_pin_wins(world, monkeypatch, tmp_path):
    """Round 9: an export with NO slug is not an ownership decision on a
    named board — base behaviour. The active board's rows resolve through the
    resolver, where the pin wins: ``export_board(None)`` exports the pin's
    cards (1), not default's real store (2)."""
    from hermes_cli import kanban_transfer as kt
    home, pin = world
    kb.set_current_board("default")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    result = kt.export_board(None, str(tmp_path / "pinned.tar.gz"))
    assert result["counts"]["tasks"] == 1, result
    assert result["board"] == "default"


# --- Defect 3: the root pins behave exactly as at base (the pin wins) -------


def test_workspaces_root_pin_wins_for_a_named_board(world, monkeypatch):
    """Base behaviour: ``HERMES_KANBAN_WORKSPACES_ROOT`` pins the root even
    when a board is named (the pin re-anchors every workspace path)."""
    home, pin = world
    forced = home.parent / "forced-workspaces"
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(forced))
    assert kb.workspaces_root(board="other") == forced
    assert kb.workspaces_root() == forced


def test_attachments_root_pin_wins_for_a_named_board(world, monkeypatch):
    """Base behaviour: ``HERMES_KANBAN_ATTACHMENTS_ROOT`` pins the root even
    when a board is named."""
    home, pin = world
    forced = home.parent / "forced-attachments"
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(forced))
    assert kb.attachments_root(board="other") == forced
    assert kb.task_attachments_dir("t_x", board="other") == forced / "t_x"
