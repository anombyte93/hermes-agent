"""Board removal cannot discard unreadable external ownership."""
import argparse
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as cli, kanban_db as kb, kanban_db_connect as kbc
from plugins.kanban.dashboard.plugin_api import router


@pytest.mark.parametrize("route", ["kernel", "cli", "dashboard"])
def test_corrupt_board_removal_is_named_and_recoverable(tmp_path, monkeypatch, capsys, route):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(key, raising=False)
    kb.create_board("broken")
    path = kb.board_dir("broken") / "kanban.db"
    path.write_bytes(b"not a sqlite database" * 100)
    before = path.read_bytes()
    if route == "kernel":
        with pytest.raises(ValueError, match="board_unreadable.*hermes kanban.*repair"):
            kb.remove_board("broken")
    elif route == "cli":
        parser = argparse.ArgumentParser()
        cli.build_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["kanban", "boards", "rm", "broken"])
        assert cli.kanban_command(args) == 1
        output = capsys.readouterr()
        text = output.out + output.err
        assert "board_unreadable" in text and "repair" in text and "Traceback" not in text
    else:
        app = FastAPI()
        app.include_router(router)
        response = TestClient(app, raise_server_exceptions=False).delete("/boards/broken")
        assert response.status_code == 400, response.text
        assert "board_unreadable" in response.text and "repair" in response.text
    assert path.read_bytes() == before


# --- Board removal cannot remove a path another store owns (the two gates) ---

_INCARNATION = dict(session="holder", id="$5", created="1", pane="%5",
                    pid="4242", backend="claude", conversation="board-refusal-fixture")


@pytest.fixture
def two_boards(tmp_path, monkeypatch):
    """Boards ``owner-a`` (owns P via an external card) and ``holder-b``
    (whose directory holds P), with P carrying proof bytes."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    from hermes_cli import kanban_db_workspace as kbw
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *a, **k: None)
    kb.init_db()
    kb.create_board("owner-a")
    kb.create_board("holder-b")
    p = kb.workspaces_root(board="holder-b") / "shared"
    p.mkdir(parents=True)
    (p / "proof").write_bytes(b"owned")
    return p


def _own_p_on_a(owner: str, p) -> None:
    """Record on ``owner-a`` either a never-claimed external_session card with
    workspace_kind dir + workspace_path P, or an active external_claim whose
    worktree is P (the two ownership inputs from the review)."""
    from hermes_cli import kanban_db_external as ext
    with kbc.connect_closing(board="owner-a") as a:
        if owner == "never_claimed":
            kb.create_task(a, title="reserved external workspace",
                           execution_kind="external_session",
                           workspace_kind="dir", workspace_path=str(p))
        else:
            tid = kb.create_task(a, title="live session",
                                 execution_kind="external_session")
            ext.external_claim(a, tid, board="owner-a", owner_host="fixture-host",
                               incarnation=_INCARNATION, worktree=str(p),
                               reservation_id="board-refusal", reservation_token="a" * 64)


@pytest.mark.parametrize("owner", ["never_claimed", "active_binding"])
@pytest.mark.parametrize("archive", [False, True])
def test_remove_board_refuses_while_another_store_owns_a_path_inside(two_boards, owner, archive):
    p = two_boards
    _own_p_on_a(owner, p)
    with pytest.raises(ValueError, match="externally_owned_workspace"):
        kb.remove_board("holder-b", archive=archive)
    # The board and the owned path survive, both modes.
    assert kb.board_dir("holder-b").is_dir()
    assert (p / "proof").read_bytes() == b"owned"


@pytest.mark.parametrize("archive", [False, True])
def test_remove_board_without_external_owner_still_removes_or_moves(two_boards, archive):
    # Control: no owner anywhere; both modes must still work.
    assert kb.remove_board("holder-b", archive=archive)["action"] == ("archived" if archive else "deleted")
    assert not kb.board_dir("holder-b").exists()
    if archive:
        assert list((kb.boards_root() / "_archived").glob("holder-b-*"))


# --- S6: remove_board under a HERMES_KANBAN_DB pin (every dispatched worker) ---

@pytest.fixture
def kanban_tmp_home(tmp_path, monkeypatch):
    """Temp kanban home with the default store initialised and pin env cleared."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    from hermes_cli import kanban_db_workspace as kbw
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *a, **k: None)
    kb.init_db()
    return tmp_path


def _own_x_outside_home(home, owner: str) -> Path:
    """holder-b's OWN store owns X (never-claimed card or active binding),
    with X outside holder-b's board directory, so gate (b) cannot see it."""
    from hermes_cli import kanban_db_external as ext
    kb.create_board("holder-b")
    x = home / "session-worktree"
    x.mkdir()
    (x / "work").write_bytes(b"live work")
    with kbc.connect_closing(board="holder-b") as b:
        if owner == "never_claimed":
            kb.create_task(b, title="reserved", execution_kind="external_session",
                           workspace_kind="dir", workspace_path=str(x))
        else:
            tid = kb.create_task(b, title="live session", execution_kind="external_session")
            ext.external_claim(b, tid, board="holder-b", owner_host="host-b",
                               incarnation=_INCARNATION, worktree=str(x),
                               reservation_id="s6-fixture", reservation_token="b" * 64)
    return x


def _pin_clean_store(home, monkeypatch) -> Path:
    """A worker-style pin: HERMES_KANBAN_DB naming a clean store C."""
    clean = home / "worker-pin" / "kanban.db"
    clean.parent.mkdir(parents=True, exist_ok=True)
    kb.init_db(db_path=clean)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(clean))
    return clean


def _remove_holder_b(route: str, archive: bool) -> str:
    if route == "kernel":
        try:
            return "removed:" + kb.remove_board("holder-b", archive=archive)["action"]
        except ValueError as exc:
            return f"refused:{exc}"
    if route == "dashboard":
        app = FastAPI()
        app.include_router(router)
        response = TestClient(app, raise_server_exceptions=False).delete(
            "/boards/holder-b" + ("" if archive else "?delete=true"))
        if response.status_code == 200:
            return f"removed:HTTP {response.status_code}"
        return f"refused:HTTP {response.status_code}"
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["kanban", "boards", "rm", "holder-b"] + ([] if archive else ["--delete"]))
    rc = cli.kanban_command(args)
    return f"removed:rc={rc}" if rc == 0 else f"refused:rc={rc}"


@pytest.mark.parametrize("route", ["kernel", "cli", "dashboard"])
@pytest.mark.parametrize("archive", [False, True])
@pytest.mark.parametrize("owner", ["never_claimed", "active_binding"])
def test_pinned_remove_board_reads_the_removed_boards_own_store(
        kanban_tmp_home, monkeypatch, owner, archive, route):
    """S6: the dispatcher pins HERMES_KANBAN_DB into every worker's environment,
    so a name-based store open resolves to the PINNED store, not the board being
    removed. Removal must instead open the removed board's own store by path and
    refuse exactly as it does without the pin. The unpinned halves of these
    contracts are pinned by test_remove_board_refuses_while_another_store_owns_a_path_inside
    and tests/hermes_cli/test_kanban_store_owns_path.py.

    The never-claimed + archive=True cell legitimately archives (no active
    binding; the reserved path lies outside the board directory); every other
    cell must refuse."""
    x = _own_x_outside_home(kanban_tmp_home, owner)
    clean = _pin_clean_store(kanban_tmp_home, monkeypatch)
    # Document the resolver rule the S6 fix relies on: under a pin, an open
    # that NAMES holder-b reads holder-b's own store (the pin only applies to
    # board=None or the same file). S6 keeps its by-path open regardless.
    with kbc.connect_closing(board="holder-b") as probe:
        opened = Path(probe.execute("PRAGMA database_list").fetchone()[2]).resolve()
    assert opened == (kb.board_dir("holder-b") / "kanban.db").resolve()

    outcome = _remove_holder_b(route, archive)
    if archive and owner == "never_claimed":
        # Legitimate: no active binding, and the reserved path lies outside the
        # board directory, so archive=True moves the board and keeps X.
        assert outcome.startswith("removed"), outcome
        assert (x / "work").read_bytes() == b"live work"
        return
    assert outcome.startswith("refused"), (
        f"pinned removal of holder-b (owner={owner}, archive={archive}, route={route}) "
        f"went through: {outcome}")
    assert kb.board_dir("holder-b").is_dir()
    assert (x / "work").read_bytes() == b"live work"


@pytest.mark.parametrize("archive", [False, True])
def test_pinned_remove_board_without_owner_still_removes_or_moves(
        kanban_tmp_home, monkeypatch, archive):
    """Control: with the pin set and no owner on holder-b's own store, both
    modes still delete or move the board."""
    kb.create_board("holder-b")
    _pin_clean_store(kanban_tmp_home, monkeypatch)
    assert kb.remove_board("holder-b", archive=archive)["action"] == (
        "archived" if archive else "deleted")


@pytest.mark.parametrize("pinned", [False, True])
def test_pinned_board_delete_then_sharing_archive_keeps_live_worktree(
        kanban_tmp_home, monkeypatch, pinned):
    """End-to-end damage case: X lives in board-c's managed workspaces and is
    owned by holder-b's ACTIVE binding (a live session). A worker process
    (HERMES_KANBAN_DB pinned to its own clean store) runs `boards rm holder-b
    --delete`; later a sharing scratch worker on board-c is archived. The
    delete must refuse, or the archive silently removes X."""
    from hermes_cli import kanban_db_external as ext
    kb.create_board("holder-b")
    kb.create_board("board-c")
    x = kb.workspaces_root(board="board-c") / "shared"
    x.mkdir(parents=True)
    (x / "work").write_bytes(b"live work")
    with kbc.connect_closing(board="holder-b") as b:
        tid = kb.create_task(b, title="live session", execution_kind="external_session")
        ext.external_claim(b, tid, board="holder-b", owner_host="host-b",
                           incarnation=_INCARNATION, worktree=str(x),
                           reservation_id="s6-damage", reservation_token="d" * 64)
    if pinned:
        _pin_clean_store(kanban_tmp_home, monkeypatch)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "boards", "rm", "holder-b", "--delete"])
    rc = cli.kanban_command(args)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with kbc.connect_closing(board="board-c") as c:
        sharing = kb.create_task(c, title="sharing scratch worker",
                                 workspace_kind="scratch", workspace_path=str(x))
        assert kb.archive_task(c, sharing)
    assert rc != 0, "pinned boards rm --delete succeeded while holder-b holds an active binding"
    assert x.is_dir() and (x / "work").read_bytes() == b"live work"


# --- True refusal reasons (refuse identically, name the real cause) ---

@pytest.mark.parametrize("archive", [False, True])
def test_refusal_names_the_unreadable_store(kanban_tmp_home, archive):
    """Case 1: an unrelated board's store cannot be read, so every board
    removal fails closed — right. The reason must name that store and say
    ownership could not be read, not claim another store owns a path."""
    kb.create_board("victim")
    kb.create_board("broken")
    broken_db = kb.board_dir("broken") / "kanban.db"
    broken_db.write_bytes(b"not a sqlite database" * 100)
    with pytest.raises(ValueError) as exc:
        kb.remove_board("victim", archive=archive)
    msg = str(exc.value)
    assert "externally_owned_workspace" in msg
    assert str(broken_db) in msg, msg
    assert "could not be read" in msg, msg
    assert "another store" not in msg, msg
    assert kb.board_dir("victim").is_dir()
    assert broken_db.read_bytes() == b"not a sqlite database" * 100


@pytest.mark.parametrize("breakage", ["mode000", "dangling"])
def test_refusal_names_the_unopenable_store(kanban_tmp_home, breakage):
    """Defect 3: a store that cannot even be OPENED (mode 000, or a dangling
    kanban.db symlink) gets the same true reason as the garbage-bytes case —
    'kanban store <path> could not be read' — not 'another store's external
    session owns' it. sqlite refuses these at connect(), so the per-store
    open must carry the detail out."""
    import os
    kb.create_board("victim")
    kb.create_board("broken")
    broken_db = kb.board_dir("broken") / "kanban.db"
    if breakage == "mode000":
        broken_db.write_bytes(b"not a sqlite database" * 100)
        os.chmod(broken_db, 0)
    else:  # dangling symlink
        gone = kanban_tmp_home / "gone-store.db"
        broken_db.unlink()
        broken_db.symlink_to(gone)

    with pytest.raises(ValueError) as exc:
        kb.remove_board("victim", archive=True)
    msg = str(exc.value)
    assert "externally_owned_workspace" in msg
    assert str(broken_db) in msg, msg
    assert "could not be read" in msg, msg
    assert "another store" not in msg, msg
    assert kb.board_dir("victim").is_dir()
    if breakage == "mode000":
        os.chmod(broken_db, 0o644)  # restore so tmp cleanup can unlink it
    else:
        assert broken_db.is_symlink()


def test_own_card_reserving_path_inside_own_board_is_named(kanban_tmp_home):
    """Case 2: the board's OWN never-claimed card reserves a path inside its
    own directory. Archive=True rightly refuses (moving the board breaks that
    session) and must name this board's own card and store, not "another
    store"; the hard-delete hint must point at the card to resolve, not at an
    archive route that also refuses here."""
    from hermes_cli import kanban_db_workspace as kbw
    kbw  # (import kept local: the helper under test)
    kb.create_board("selfowner")
    x = kb.board_dir("selfowner") / "workspaces" / "reserved"
    x.mkdir(parents=True)
    (x / "proof").write_bytes(b"owned")
    with kbc.connect_closing(board="selfowner") as conn:
        tid = kb.create_task(conn, title="reserved", execution_kind="external_session",
                             workspace_kind="dir", workspace_path=str(x))

    with pytest.raises(ValueError) as archive_exc:
        kb.remove_board("selfowner", archive=True)
    msg = str(archive_exc.value)
    assert "externally_owned_workspace" in msg
    assert tid in msg and str(x) in msg, msg
    assert "its own store" in msg, msg
    assert "another store" not in msg, msg

    with pytest.raises(ValueError) as history_exc:
        kb.remove_board("selfowner", archive=False)
    hint = str(history_exc.value)
    assert "external_binding_history" in hint
    assert tid in hint and str(x) in hint, hint
    assert kb.board_dir("selfowner").is_dir()
    assert (x / "proof").read_bytes() == b"owned"


# --- Defect 2: the history hint must not be a dead end ---

def test_history_refusal_names_the_archive_route_when_it_would_succeed(kanban_tmp_home):
    """Case A (the verdict's fixture A shape): the board's own card reserves a
    path OUTSIDE the board directory. Hard delete refuses on binding history;
    the archive route would succeed (no active binding, gate (b) False on the
    board directory), so the refusal must NAME it — not send the user to a
    release/delete route that refuses."""
    x = _own_x_outside_home(kanban_tmp_home, "never_claimed")
    with pytest.raises(ValueError) as exc:
        kb.remove_board("holder-b", archive=False)
    msg = str(exc.value)
    assert "external_binding_history" in msg
    assert str(x) in msg, msg
    assert "archive" in msg, msg  # the route that actually works is named
    assert "another store" not in msg, msg
    assert kb.board_dir("holder-b").is_dir()
    assert (x / "work").read_bytes() == b"live work"
    # Control: the named route really does succeed.
    assert kb.remove_board("holder-b", archive=True)["action"] == "archived"
    assert (x / "work").read_bytes() == b"live work"


def test_history_refusal_points_only_at_the_card_when_archive_refuses_too(kanban_tmp_home):
    """Case B: the board's own card reserves a path INSIDE the board
    directory. Both routes refuse, so the hint must point at the card and say
    the archive route refuses too — never offer archive as the way out."""
    kb.create_board("selfblock")
    x = kb.board_dir("selfblock") / "workspaces" / "reserved"
    x.mkdir(parents=True)
    (x / "proof").write_bytes(b"owned")
    with kbc.connect_closing(board="selfblock") as conn:
        tid = kb.create_task(conn, title="reserved", execution_kind="external_session",
                             workspace_kind="dir", workspace_path=str(x))
    with pytest.raises(ValueError) as exc:
        kb.remove_board("selfblock", archive=False)
    msg = str(exc.value)
    assert "external_binding_history" in msg
    assert tid in msg and str(x) in msg, msg
    assert "archive route refuses" in msg, msg
    assert kb.board_dir("selfblock").is_dir()
    assert (x / "proof").read_bytes() == b"owned"


# --- Defect 4: the hint's tail must name the REAL cause ----------------------


def test_hint_names_the_unreadable_store_not_a_path_inside(kanban_tmp_home):
    """Defect 4, cause 1: holder-b's own card reserves a path OUTSIDE the
    board, but ANOTHER store is unreadable (mode 000), so gate (b) refuses
    and the archive route refuses with it. The tail must say that store
    could not be read (naming it), not invent 'a reserved path lies inside
    the board directory'."""
    import os
    home = kanban_tmp_home
    x = _own_x_outside_home(home, "never_claimed")
    kb.create_board("broken-holder")
    broken_db = kb.board_dir("broken-holder") / "kanban.db"
    os.chmod(broken_db, 0)
    try:
        with pytest.raises(ValueError) as exc:
            kb.remove_board("holder-b", archive=False)
    finally:
        os.chmod(broken_db, 0o644)
    msg = str(exc.value)
    assert "external_binding_history" in msg
    assert "archive route refuses" in msg, msg
    assert str(broken_db) in msg, msg
    assert "could not be read" in msg, msg
    assert "lies inside the board directory" not in msg, msg
    assert kb.board_dir("holder-b").is_dir()
    assert (x / "work").read_bytes() == b"live work"


def test_hint_names_another_store_owning_a_path_inside_the_board(kanban_tmp_home):
    """Defect 4, cause 2: holder-b's own card reserves a path OUTSIDE the
    board, and ANOTHER store owns a path INSIDE holder-b's directory, so
    gate (b) refuses and the archive route refuses with it. The tail must
    say another store owns a path inside the board, not invent 'a reserved
    path lies inside the board directory'."""
    home = kanban_tmp_home
    x = _own_x_outside_home(home, "never_claimed")
    kb.create_board("owner-c")
    inside = kb.board_dir("holder-b") / "workspaces" / "c-session"
    inside.mkdir(parents=True)
    (inside / "proof").write_bytes(b"owned by c")
    with kbc.connect_closing(board="owner-c") as c:
        kb.create_task(c, title="c's session", execution_kind="external_session",
                       workspace_kind="dir", workspace_path=str(inside))
    with pytest.raises(ValueError) as exc:
        kb.remove_board("holder-b", archive=False)
    msg = str(exc.value)
    assert "external_binding_history" in msg
    assert "archive route refuses" in msg, msg
    assert "another store" in msg or "store" in msg, msg
    assert "lies inside the board directory" not in msg, msg
    assert kb.board_dir("holder-b").is_dir()
    assert (x / "work").read_bytes() == b"live work"
    assert (inside / "proof").read_bytes() == b"owned by c"


# --- Defect 5: a mode-000 board DIRECTORY names the store, not an owner -----


def test_mode000_board_directory_remove_board_names_the_store(kanban_tmp_home):
    """Defect 5, remove_board route: a board DIRECTORY with mode 000 makes
    ``db.exists()`` raise EACCES inside gate (b); the refusal must say the
    kanban store could not be read (naming it), not 'another store's
    external session'."""
    import os
    home = kanban_tmp_home
    x = _own_x_outside_home(home, "never_claimed")
    kb.create_board("locked-dir")
    locked_dir = kb.board_dir("locked-dir")
    os.chmod(locked_dir, 0)
    try:
        with pytest.raises(ValueError) as exc:
            kb.remove_board("holder-b", archive=False)
    finally:
        os.chmod(locked_dir, 0o755)
    msg = str(exc.value)
    assert "could not be read" in msg, msg
    assert str(locked_dir / "kanban.db") in msg, msg
    assert "another store's external session" not in msg, msg
    assert kb.board_dir("holder-b").is_dir()
    assert (x / "work").read_bytes() == b"live work"


def test_mode000_board_directory_gc_prune_names_ownership_unreadable(kanban_tmp_home, tmp_path):
    """Defect 5, worktree_gc route: a board DIRECTORY with mode 000 makes
    the kept line say ownership could not be read, not 'external session
    owns the worktree'."""
    import os
    from hermes_cli import worktree_gc
    home = kanban_tmp_home
    kb.create_board("locked-dir")
    locked_dir = kb.board_dir("locked-dir")
    os.chmod(locked_dir, 0)
    try:
        gate = worktree_gc._externally_owned_or_unanswerable(str(home / "some-tree"))
    finally:
        os.chmod(locked_dir, 0o755)
    owned, reason = gate if isinstance(gate, tuple) else (gate, None)
    assert owned is True
    assert reason is not None and "could not be read" in reason, gate
    assert "owns the worktree" not in (reason or ""), gate


def test_mode000_board_directory_ops_prune_names_ownership_unreadable(kanban_tmp_home):
    """Defect 5, worktree_ops route: same shape through
    ``_externally_owned_or_unanswerable``."""
    import os
    from hermes_cli import worktree_ops
    home = kanban_tmp_home
    kb.create_board("locked-dir")
    locked_dir = kb.board_dir("locked-dir")
    os.chmod(locked_dir, 0)
    try:
        gate = worktree_ops._externally_owned_or_unanswerable(str(home / "some-tree"))
    finally:
        os.chmod(locked_dir, 0o755)
    owned, reason = gate if isinstance(gate, tuple) else (gate, None)
    assert owned is True
    assert reason is not None and "could not be read" in reason, gate
    assert "owns the worktree" not in (reason or ""), gate
