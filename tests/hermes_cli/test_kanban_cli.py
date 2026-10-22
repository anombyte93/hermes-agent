"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def _run_kanban(*tokens):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *tokens]))


@pytest.mark.parametrize("status", ["triage", "todo", "scheduled"])
def test_kanban_refusal_unsupported_status(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unfinished specification", triage=True)
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()

    assert _run_kanban("complete", tid) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"cannot complete {tid}: status is {status}" in captured.err
    assert "complete accepts running/ready/blocked/review" in captured.err
    assert "kanban archive" in captured.err
    assert "drop" in captured.err
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == status


def test_kanban_refusal_unknown_completion(kanban_home, capsys):
    assert _run_kanban("--board", "default", "complete", "missing-task") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown task id" in captured.err
    assert "missing-task" in captured.err
    assert "board 'default'" in captured.err
    assert "--board" in captured.err


@pytest.mark.parametrize("status", ["done", "archived"])
def test_kanban_refusal_terminal_completion(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="finished task")
        if status == "done":
            assert kb.complete_task(conn, tid)
        else:
            assert kb.archive_task(conn, tid)

    assert _run_kanban("complete", tid) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"already terminal (status is {status})" in captured.err


def test_kanban_refusal_unsatisfied_parents(kanban_home, capsys):
    with kb.connect_closing() as conn:
        parents = [kb.create_task(conn, title=f"parent {n}") for n in range(3)]
        assert kb.archive_task(conn, parents[2])
        tid = kb.create_task(conn, title="dependent task", parents=parents)
        # A parent can reopen after the child has entered an accepted status.
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()

    assert _run_kanban("complete", tid) == 1
    error = capsys.readouterr().err
    assert "unsatisfied parents" in error
    assert all(parent in error for parent in parents[:2])
    assert parents[2] not in error


def test_kanban_refusal_stale_completion_run(kanban_home, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="newer run")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")

    assert _run_kanban("complete", tid) == 1
    assert "run id mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("command", [
    ["show", "missing-task"],
    ["assign", "missing-task", "none"],
    ["set-model", "missing-task", "example-model"],
    ["diagnostics", "--task", "missing-task"],
    ["claim", "missing-task"],
    ["attachments", "missing-task"],
    ["notify-subscribe", "missing-task", "--platform", "example", "--chat-id", "test"],
])
def test_kanban_refusal_not_found_named_board(kanban_home, capsys, command):
    kb.create_board("example")
    assert _run_kanban("--board", "example", *command) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no such task: missing-task" in captured.err
    assert "board 'example'" in captured.err
    assert "resolved from --board" in captured.err


@pytest.mark.parametrize("source", ["env", "current", "default", "stale-env", "db"])
def test_kanban_refusal_not_found_board_source(kanban_home, monkeypatch, capsys, source):
    board = "default"
    if source in {"env", "current", "stale-env"}:
        kb.create_board("example")
        board = "example"
    if source == "env":
        monkeypatch.setenv("HERMES_KANBAN_BOARD", " EXAMPLE ")
        expected_source = "HERMES_KANBAN_BOARD"
    elif source in {"current", "stale-env"}:
        kb.set_current_board(board)
        expected_source = str(kb.current_board_path())
        if source == "stale-env":
            monkeypatch.setenv("HERMES_KANBAN_BOARD", "missing-board")
    elif source == "db":
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "isolated.db"))
        expected_source = "HERMES_KANBAN_DB"
    else:
        expected_source = "built-in default"

    assert _run_kanban("show", "missing-task") == 1
    error = capsys.readouterr().err
    assert f"board {board!r}" in error
    assert f"resolved from {expected_source}" in error
    if source == "db":
        assert str(kanban_home / "isolated.db") in error


@pytest.mark.parametrize("status", ["running", "ready", "blocked", "review"])
def test_kanban_completion_success_unchanged(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="completable task")
        conn.execute(
            "UPDATE tasks SET status=?, block_reason=? WHERE id=?",
            (status, "waiting for verification" if status == "blocked" else None, tid),
        )
        conn.commit()

    assert _run_kanban("complete", tid, "--result", "verified") == 0
    captured = capsys.readouterr()
    assert captured.out == f"Completed {tid}\n"
    assert captured.err == ""
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert task.result == "verified"


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# reclaim + reassign CLI smoke tests
# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash("create 'stuck worker task' --assignee broken-model")
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


