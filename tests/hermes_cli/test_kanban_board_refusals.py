"""Board removal cannot discard unreadable external ownership."""
import argparse
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban as cli, kanban_db as kb
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
