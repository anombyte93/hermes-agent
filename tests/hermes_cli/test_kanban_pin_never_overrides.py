"""S7, closed at the root: a ``HERMES_KANBAN_DB`` pin never overrides a NAMED board.

The dispatcher pins ``HERMES_KANBAN_DB`` into every worker's environment so
the worker's own board (``board=None``) still resolves after ``hermes -p``
rewrites ``HERMES_HOME``. Rounds 1-6 fixed one route at a time (remove_board
opens by path, prune gates, export); the seventh variant (``boards import``
scrubbing the PINNED store instead of the imported one) showed the defect
lives in the shared resolver ``kanban_db._board_path``, which returned the
override before it looked at an explicitly named board.

This file pins the root contract and its three damage routes:

* ``board=None`` keeps opening the pinned store — the pin's whole purpose;
* an explicitly named board opens ITS OWN store under the pin (the pin only
  applies when it resolves to that same file);
* ``boards import`` (kernel, CLI, dashboard) must scrub the IMPORTED store
  and leave the pinned store byte-for-byte ownership-identical;
* ``export_board('<slug>')`` exports the named board's own store;
* ``remove_board`` (S6) keeps refusing exactly as without the pin.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban as cli, kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_transfer as kt
from plugins.kanban.dashboard.plugin_api import router

_INCARNATION = dict(session="holder", id="$5", created="1", pane="%5",
                    pid="4242", backend="claude", conversation="s7-root-fixture")

_OWNER_MODES = ["never_claimed", "active_binding"]


def _clear_pin_env(monkeypatch):
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)


def _own_x_on(board: str, home: Path, owner: str) -> Path:
    """Board ``board`` owns X = ``home/session-worktree`` (outside every board
    directory) via a never-claimed external_session card or an active
    external_claim — the two ownership inputs from the review."""
    from hermes_cli import kanban_db_external as ext
    x = home / "session-worktree"
    if not x.exists():
        x.mkdir()
        (x / "work").write_bytes(b"live work")
    with kbc.connect_closing(board=board) as conn:
        if owner == "never_claimed":
            kb.create_task(conn, title="reserved", execution_kind="external_session",
                           workspace_kind="dir", workspace_path=str(x))
        else:
            tid = kb.create_task(conn, title="live session",
                                 execution_kind="external_session")
            ext.external_claim(conn, tid, board=board, owner_host="s7-host",
                               incarnation=_INCARNATION, worktree=str(x),
                               reservation_id="s7-root", reservation_token="c" * 64)
    return x


def _ownership_counts(store: Path) -> tuple[int, int]:
    """(active bindings, never-claimed reservations) held by the store."""
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM task_external_bindings WHERE state='active'"
        ).fetchone()[0]
        reserved = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE execution_kind='external_session' "
            "AND workspace_path IS NOT NULL"
        ).fetchone()[0]
        return int(active), int(reserved)
    finally:
        conn.close()


def _gate_b(x: Path) -> bool:
    from hermes_cli import kanban_db_workspace as kbw
    return bool(kbw._externally_owned_workspace(None, str(x)))


def _imported_event_count(store: Path) -> int:
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='imported'").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def s7_world(tmp_path, monkeypatch):
    """A worker-style world: home with plain boards ``other`` and ``board-c``,
    and a pinned store C (``HERMES_KANBAN_DB``) holding an owner record on X.

    Returns ``(home, worker_db, x)`` with the pin NOT yet set — each test sets
    it (or not) explicitly.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _clear_pin_env(monkeypatch)
    from hermes_cli import kanban_db_workspace as kbw
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *a, **k: None)
    kb.init_db()
    worker_db = tmp_path / "worker-pin" / "kanban.db"
    worker_db.parent.mkdir(parents=True)
    kb.init_db(db_path=worker_db)
    kb.create_board("other")
    kb.create_board("board-c")
    return home, worker_db


def _sharing_archive(tmp_path: Path, owner: str) -> Path:
    """A valid board archive whose store still carries an exporter binding
    row on X (raw snapshot, no export scrub) — the untrusted shape import
    must scrub on ITS side."""
    src_root = tmp_path / "exporter-home"
    src_root.mkdir()
    saved = {k: os.environ.get(k) for k in
             ("HERMES_HOME", "HERMES_KANBAN_HOME", "HERMES_KANBAN_DB")}
    os.environ["HERMES_HOME"] = str(src_root)
    os.environ["HERMES_KANBAN_HOME"] = str(src_root)
    os.environ.pop("HERMES_KANBAN_DB", None)
    kb._INITIALIZED_PATHS.clear()
    try:
        kb.init_db()
        _own_x_on("default", src_root, owner)
        stage = tmp_path / "archive-stage" / "source"
        stage.mkdir(parents=True)
        kt._snapshot_db(kb.kanban_db_path("default"), stage / "kanban.db")
        (stage / "manifest.json").write_text(json.dumps(
            {"format": kt.ARCHIVE_FORMAT, "format_version": 1,
             "board": "source", "board_name": "Source"}))
        archive = tmp_path / "sharing.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(stage, arcname="source")
        return archive
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        kb._INITIALIZED_PATHS.clear()


def _import_archive(route: str, archive: Path, home: Path) -> None:
    if route == "kernel":
        kt.import_board(str(archive), "imported")
        return
    if route == "dashboard":
        app = FastAPI()
        app.include_router(router)
        response = TestClient(app, raise_server_exceptions=False).post(
            "/boards/import", json={"archive": str(archive), "slug": "imported"})
        assert response.status_code == 200, response.text
        return
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "boards", "import", str(archive), "--as", "imported"])
    rc = cli.kanban_command(args)
    assert rc == 0, "in-process CLI import failed"


def _import_archive_real_cli(archive: Path, home: Path, pin: Path, tmp_path: Path) -> int:
    """The real CLI in a fresh interpreter with a temp HOME and the pin set,
    the way a dispatched worker runs it."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "cli-home"),
        "HERMES_HOME": str(home),
        "HERMES_KANBAN_DB": str(pin),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (tmp_path / "cli-home").mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "boards", "import",
         str(archive), "--as", "imported"],
        cwd=str(_WORKTREE), env=env, capture_output=True, text=True, timeout=180,
    )
    return result.returncode


# --- The root contract -------------------------------------------------------


@pytest.mark.parametrize("owner", _OWNER_MODES)
def test_named_board_opens_its_own_store_under_the_pin(s7_world, monkeypatch, owner):
    """With HERMES_KANBAN_DB pinned, an open that NAMES a board resolves to
    that board's own store; board=None keeps the pinned store."""
    home, worker_db = s7_world
    _own_x_on("other", home, owner)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))

    assert kb.kanban_db_path(board="other") == kb.board_dir("other") / "kanban.db"
    conn = kbc.connect(board="other")
    try:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert opened == (kb.board_dir("other") / "kanban.db").resolve()
    assert opened != worker_db.resolve()

    conn = kbc.connect()
    try:
        opened_none = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert opened_none == worker_db.resolve()


def test_pin_naming_the_boards_own_store_still_applies(s7_world, monkeypatch):
    """Same-file rule: when the pin resolves to the named board's own store,
    the pin applies — including through a symlink (re-anchored layouts)."""
    home, worker_db = s7_world
    own = kb.board_dir("other") / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(own))
    assert kb.kanban_db_path(board="other") == own

    link = home / "pin-link.db"
    link.symlink_to(own)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(link))
    assert kb.kanban_db_path(board="other") == link

    conn = kbc.connect(board="other")
    try:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert opened == own.resolve()


def test_pin_still_wins_for_the_default_board(s7_world, monkeypatch):
    """``default`` keeps today's behaviour: its store IS the root layout the
    pin re-anchors, so the pin applies to board=None and board='default'
    alike (the pin's stated purpose)."""
    home, worker_db = s7_world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    assert kb.kanban_db_path() == worker_db
    assert kb.kanban_db_path(board="default") == worker_db


# --- S7: boards import must scrub the IMPORTED store -------------------------


@pytest.mark.parametrize("owner", _OWNER_MODES)
@pytest.mark.parametrize("route", ["kernel", "cli", "dashboard"])
def test_pinned_import_scrubs_the_imported_store_not_the_pin(
        s7_world, monkeypatch, tmp_path, owner, route):
    """S7: a worker (pin = its own store, which owns X) imports an archive.
    The scrub must land on the IMPORTED store; the pinned store's ownership,
    gate (b) answer, cards and events are untouched."""
    home, worker_db = s7_world
    # X ownership lives on the PINNED store (the worker's own board).
    from hermes_cli import kanban_db_external as ext
    x = home / "session-worktree"
    x.mkdir()
    (x / "work").write_bytes(b"live work")
    with kbc.connect_closing(db_path=worker_db) as conn:
        if owner == "never_claimed":
            kb.create_task(conn, title="reserved", execution_kind="external_session",
                           workspace_kind="dir", workspace_path=str(x))
        else:
            tid = kb.create_task(conn, title="live session",
                                 execution_kind="external_session")
            ext.external_claim(conn, tid, board="default", owner_host="s7-host",
                               incarnation=_INCARNATION, worktree=str(x),
                               reservation_id="s7-pin", reservation_token="c" * 64)
    archive = _sharing_archive(tmp_path, owner)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    # Gate (b) sees the pinned store's ownership only through the pin (the
    # store sits at a custom path outside the kanban home) — the exact
    # precondition the S7 damage route requires.
    before = _ownership_counts(worker_db)
    assert before in ((1, 0), (0, 1))
    assert _gate_b(x) is True
    if route == "cli":
        rc = _import_archive_real_cli(archive, home, worker_db, tmp_path)
        assert rc == 0, "real CLI import failed under the pin"
    else:
        _import_archive(route, archive, home)

    # The pinned store is untouched: ownership, cards, events.
    assert _ownership_counts(worker_db) == before, (
        "the import scrub landed on the PINNED store (S7)")
    assert _gate_b(x) is True, "gate (b) on X flipped: the pin's ownership was scrubbed"
    assert _imported_event_count(worker_db) == 0
    assert (x / "work").read_bytes() == b"live work"

    # The IMPORTED store is the one scrubbed: the exporter's binding row is gone.
    imported = kb.board_dir("imported") / "kanban.db"
    assert imported.is_file(), "import did not land a store"
    assert _ownership_counts(imported) == (0, 0), (
        "the imported store kept the exporter's binding row (unscrubbed)")


def test_end_to_end_sharing_archive_after_pinned_import_keeps_x(
        s7_world, monkeypatch, tmp_path):
    """The verdict's end-to-end damage: worker-board owns X (inside board-c's
    managed workspaces); a pinned import used to scrub that ownership away, so
    a later sharing scratch archive on board-c removed X. With the root fix
    the ownership survives and the sharing archive keeps X."""
    home, worker_db = s7_world
    x = kb.workspaces_root(board="board-c") / "shared"
    x.mkdir(parents=True)
    (x / "work").write_bytes(b"live work")
    with kbc.connect_closing(db_path=worker_db) as conn:
        from hermes_cli import kanban_db_external as ext
        tid = kb.create_task(conn, title="live session", execution_kind="external_session")
        ext.external_claim(conn, tid, board="default", owner_host="s7-host",
                           incarnation=_INCARNATION, worktree=str(x),
                           reservation_id="s7-e2e", reservation_token="c" * 64)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    assert _gate_b(x) is True

    archive = _sharing_archive(tmp_path, "active_binding")
    _import_archive("kernel", archive, home)

    with kbc.connect_closing(board="board-c") as c:
        sharing = kb.create_task(c, title="sharing scratch worker",
                                 workspace_kind="scratch", workspace_path=str(x))
        assert kb.archive_task(c, sharing)
    assert x.is_dir() and (x / "work").read_bytes() == b"live work", (
        "the sharing scratch archive removed X after the pinned import")


# --- S7 siblings under the pin: export and remove ----------------------------


def test_pinned_export_board_exports_the_named_boards_own_store(
        s7_world, monkeypatch, tmp_path):
    """export_board('<slug>') under a pin exports the named board's own store,
    not the pinned one."""
    home, worker_db = s7_world
    with kbc.connect_closing(board="other") as conn:
        kb.create_task(conn, title="other's own task", assignee="fixture")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    result = kt.export_board("other", str(tmp_path / "other.tar.gz"))
    assert result["counts"]["tasks"] == 1, (
        "export read the pinned store instead of other's own store")


@pytest.mark.parametrize("owner", _OWNER_MODES)
def test_pinned_remove_board_still_refuses_as_round_6(s7_world, monkeypatch, owner):
    """S6 stays closed: under the pin, remove_board reads the removed board's
    own store (by path) and refuses exactly as without the pin."""
    home, worker_db = s7_world
    x = _own_x_on("other", home, owner)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    with pytest.raises(ValueError, match="external_binding_history|active_external_binding"):
        kb.remove_board("other", archive=False)
    assert kb.board_dir("other").is_dir()
    assert (x / "work").read_bytes() == b"live work"


def test_board_none_uses_the_pinned_store(s7_world, monkeypatch):
    """The pin's purpose survives the root fix: board=None (and the worker's
    own tools) keep opening the pinned store."""
    home, worker_db = s7_world
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    assert kb.kanban_db_path() == worker_db
    conn = kbc.connect()
    try:
        opened = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    finally:
        conn.close()
    assert opened == worker_db.resolve()
