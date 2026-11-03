"""Worker dependency blocks need either a material parent change or an answer."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "worker").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_no_parent_dependency_block_survives_five_dispatch_passes(kanban_home, tmp_path):
    """The t_58d209d7 shape must not create another attempt after worker block."""
    spawn = Mock(return_value=None)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="waiting for parent answer", assignee="worker",
            workspace_kind="dir", workspace_path=str(tmp_path),
        )
        first = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert [row[0] for row in first.spawned] == [tid]
        run_id = kb.get_task(conn, tid).current_run_id
        assert run_id is not None
        assert kb.block_task(
            conn, tid, kind="dependency", reason="parent must answer",
            expected_run_id=run_id,
        )
        runs = kb.list_runs(conn, tid)
        assert len(runs) == 1
        assert runs[0].ended_at is not None

        states = [kb.get_task(conn, tid).status]
        for _ in range(5):
            kb.recompute_ready(conn)
            tick = kbd.dispatch_once(conn, spawn_fn=spawn)
            states.append(kb.get_task(conn, tid).status)
            assert not tick.skipped_locked

        assert states == ["blocked"] * 6
        assert kb.list_runs(conn, tid) == runs
        assert spawn.call_count == 1
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "promoted"]


def test_real_dependency_wait_resumes_only_after_parent_finishes(kanban_home):
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="child", assignee="worker")
        assert kb.claim_task(conn, child, claimer="worker") is not None
        parent = kb.create_task(conn, title="unfinished prerequisite", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        assert kb.block_task(conn, child, kind="dependency", reason="await prerequisite")
        runs = kb.list_runs(conn, child)
        for _ in range(5):
            kb.recompute_ready(conn)
            task = kb.get_task(conn, child)
            assert task.status == "todo"
            assert task.block_recurrences == 0
        assert kb.list_runs(conn, child) == runs
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, result="prerequisite delivered")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.list_runs(conn, child) == runs
