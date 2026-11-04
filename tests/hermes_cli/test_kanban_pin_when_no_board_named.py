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
import os
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

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


# --- Defect E (round 9): an invalid slug under a root pin returns the pin ---


def test_invalid_slug_with_a_root_pin_returns_the_pin(world, monkeypatch):
    """Defect E: with ``HERMES_KANBAN_WORKSPACES_ROOT`` set, an invalid slug
    such as ``workspaces_root(board='Bad Slug!')`` returns the pin, exactly
    as at base — the env override is checked before the slug is normalised.
    Rounds 7/8 normalised first, so the same call raised ValueError."""
    home, pin = world
    forced = home.parent / "forced-workspaces"
    forced_att = home.parent / "forced-attachments"
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(forced))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(forced_att))
    assert kb.workspaces_root(board="Bad Slug!") == forced
    assert kb.attachments_root(board="Bad Slug!") == forced_att
    # And without a pin the invalid slug still raises, as at base.
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    with pytest.raises(ValueError):
        kb.workspaces_root(board="Bad Slug!")


# --- Defect G (round 10): boards export with no slug keeps the pin ----------


def _export_titles(archive: Path) -> list[str]:
    """Task titles inside an export archive, read straight from the packed
    store (``<slug>/kanban.db`` member)."""
    import sqlite3
    import tarfile

    with tarfile.open(archive, "r:gz") as tar:
        member = next(m for m in tar.getmembers()
                      if PurePosixPath(m.name).name == "kanban.db")
        extracted = tar.extractfile(member)
        assert extracted is not None
        data = extracted.read()
    fd, tmp = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute("SELECT title FROM tasks ORDER BY title")]
        finally:
            conn.close()
    finally:
        os.unlink(tmp)


def _cli_boards_export(home: Path, pin: Path, tmp_path: Path,
                       out: Path, *, board_env: str) -> subprocess.CompletedProcess:
    """``hermes kanban boards export`` in a fresh interpreter with a temp HOME
    and the pin set, the way a dispatched worker runs it. ``home`` is the
    worker's own ``HERMES_HOME`` (root B in the disagreeing-roots shape)."""
    cli_home = tmp_path / "cli-home"
    cli_home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cli_home),
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_DB": str(pin),
        "HERMES_KANBAN_BOARD": board_env,
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "boards", "export",
         "-o", str(out)],
        cwd=str(_WORKTREE), env=env, capture_output=True, text=True, timeout=180,
    )


def test_boards_export_no_slug_env_active_board_reads_the_pin(world, monkeypatch, tmp_path):
    """Defect G: under a ``HERMES_KANBAN_DB`` pin, ``boards export`` with no
    slug is NOT an explicit slug — it must export the pin's store (1 card),
    not the active board's own store, when the active board comes from
    ``HERMES_KANBAN_BOARD``."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    args = argparse.Namespace(
        boards_action="export", slug=None, output=str(tmp_path / "e1.tar.gz"),
        no_attachments=False, include_logs=False, json=True,
    )
    assert cli._dispatch_boards(args) == 0
    titles = _export_titles(tmp_path / "e1.tar.gz")
    assert titles == ["pinned card"], titles


def test_boards_export_no_slug_current_file_active_board_reads_the_pin(world, monkeypatch, tmp_path):
    """Defect G: the same, with the active board set the way another session's
    ``boards switch other`` leaves it — the global current-board file. The
    export still reads the pin, not other's own store."""
    home, pin = world
    kb.set_current_board("other")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    args = argparse.Namespace(
        boards_action="export", slug=None, output=str(tmp_path / "e2.tar.gz"),
        no_attachments=False, include_logs=False, json=True,
    )
    assert cli._dispatch_boards(args) == 0
    titles = _export_titles(tmp_path / "e2.tar.gz")
    assert titles == ["pinned card"], titles


def test_boards_export_no_slug_disagreeing_roots_real_cli_reads_the_pin(world, tmp_path):
    """Defect G, the dispatched-worker shape, real CLI entry point: the pin
    points at root A's worker-board store, ``HERMES_KANBAN_BOARD=worker-board``
    and the worker's own root B disagrees. ``boards export`` with no slug must
    export the pin's store (the worker's own card), not root B's local card."""
    home, pin = world
    # Root B has its own worker-board with its own card; the pin stays on
    # root A's store (the fixture's pin holds "pinned card").
    root_b = tmp_path / "root-b"
    (root_b / ".hermes").mkdir(parents=True)
    saved_env = {k: os.environ.get(k) for k in
                 ("HERMES_HOME", "HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD")}
    saved_home = Path.home
    os.environ["HERMES_HOME"] = str(root_b / ".hermes")
    os.environ["HOME"] = str(root_b)
    Path.home = lambda: root_b
    try:
        kb._INITIALIZED_PATHS.clear()
        import hermes_constants
        saved_memo = hermes_constants._default_hermes_root_memo
        hermes_constants._default_hermes_root_memo = None
        try:
            kb.create_board("worker-board")
            with kbc.connect_closing(board="worker-board") as conn:
                kb.create_task(conn, title="root-b local card", assignee="fixture")
        finally:
            hermes_constants._default_hermes_root_memo = saved_memo
            kb._INITIALIZED_PATHS.clear()
    finally:
        Path.home = saved_home
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    out = tmp_path / "worker-export.tar.gz"
    result = _cli_boards_export(root_b / ".hermes", pin, tmp_path, out,
                                board_env="worker-board")
    assert result.returncode == 0, result.stderr
    titles = _export_titles(out)
    assert titles == ["pinned card"], titles


def test_boards_export_typed_slug_under_the_pin_reads_that_boards_own_store(world, monkeypatch, tmp_path):
    """A TYPED slug stays explicit: ``boards export other`` under the pin
    exports other's own store, not the pin."""
    home, pin = world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pin))
    args = argparse.Namespace(
        boards_action="export", slug="other", output=str(tmp_path / "e4.tar.gz"),
        no_attachments=False, include_logs=False, json=True,
    )
    assert cli._dispatch_boards(args) == 0
    titles = _export_titles(tmp_path / "e4.tar.gz")
    assert titles == ["other's card"], titles
