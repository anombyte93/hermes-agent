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
