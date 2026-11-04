"""Read-only preflight works before first create, including planned parents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_db_materialize import materialize_task


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(key, raising=False)
    return home


def invoke(capsys, action="materialize-check", *, proposed="q", revision="r1", fingerprint="intent", extra=()):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "--board", "default", action,
        "--project=fixture", f"--plan-revision={revision}", f"--proposed-id={proposed}",
        f"--fingerprint={fingerprint}", "--title=-urgent", "--execution-kind=external_session", "--json", *extra])
    code = cli.kanban_command(args)
    output = capsys.readouterr()
    assert not output.err
    return code, json.loads(output.out)


def files(home):
    # SQLite read-only WAL readers may create an empty WAL and a lock-index SHM.
    # Neither is durable board content. Keep hashing a nonempty WAL, which may
    # contain the newest committed rows, as well as every other source file.
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob("*") if p.is_file()
            and not p.name.endswith("-shm")
            and not (p.name.endswith("-wal") and p.stat().st_size == 0)}


def test_check_initialises_neither_fresh_board_nor_changed_existing_board(home, capsys):
    before = files(home)
    assert invoke(capsys) == (0, {"state": "created", "task_id": None})
    assert files(home) == before
    code, created = invoke(capsys, "materialize")
    assert code == 0 and created["state"] == "created"
    before = files(home)
    assert invoke(capsys) == (0, {"state": "existing", "task_id": created["task_id"]})
    assert invoke(capsys, fingerprint="changed") == (3, {
        "state": "conflict", "task_id": created["task_id"], "reason": "materialization_conflict"})
    assert files(home) == before
    # A plan containing a new row and a changed existing row is refused before either create.
    assert invoke(capsys, proposed="new", revision="r2")[1]["state"] == "created"
    assert invoke(capsys, revision="r2", fingerprint="changed")[1]["reason"] == "revision_requires_mapping"
    assert files(home) == before
    with connect_closing(home / "kanban.db") as conn:
        assert len(kb.list_tasks(conn)) == 1


def test_declared_pending_parents_predict_new_and_mapped_rows_without_writes(home, capsys):
    pending = ("--parent-proposed=p", "--pending-parent-proposed=p")
    assert invoke(capsys, extra=pending) == (0, {"state": "created", "task_id": None})
    assert not (home / "kanban.db").exists()
    _, parent = invoke(capsys, "materialize", proposed="p")
    _, child = invoke(capsys, "materialize", extra=("--parent-proposed=p",))
    before = files(home)
    # A prior canonical parent checked for this plan is passed literally, not guessed.
    pending = ("--maps-from=r1:q", "--parent-proposed=p",
               f"--pending-parent-proposed=p={parent['task_id']}")
    assert invoke(capsys, revision="r2", fingerprint="changed", extra=pending) == (
        0, {"state": "mapped", "task_id": child["task_id"]})
    assert files(home) == before
    invoke(capsys, "materialize", proposed="p", revision="r2")
    actual = invoke(capsys, "materialize", revision="r2", fingerprint="changed", extra=(
        "--maps-from=r1:q", "--parent-proposed=p"))
    assert actual == (0, {"state": "mapped", "task_id": child["task_id"]})
