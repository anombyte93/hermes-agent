"""Regression: kanban watch banner must name the board it streams."""

import sys
sys.path.insert(0, '/home/anombyte/.hermes/hermes-agent')
import time
from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


def test_watch_banner_names_board(tmp_path, monkeypatch, capsys):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(__import__("pathlib").Path, "home", lambda: tmp_path)
    # create board + a task
    slug = "zz-banner-test"
    (home / "kanban" / "boards" / slug).mkdir(parents=True)
    with kb.connect_closing(board=slug) as conn:
        pass
    kb.set_current_board(slug)
    # patch out the poll loop: run one iteration then KeyboardInterrupt
    def fake_sleep(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(time, "sleep", fake_sleep)
    args = type("A", (), {"kinds": "completed", "interval": 1,
                          "assignee": None, "tenant": None})()
    rc = cli._cmd_watch(args)
    out = capsys.readouterr().out
    assert f"board '{slug}'" in out, out
    assert rc == 0
