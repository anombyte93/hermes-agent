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
    if status == "triage":
        assert "kanban specify" in captured.err
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


@pytest.mark.parametrize("status", ["done", "archived"])
def test_kanban_refusal_terminal_before_open_parents(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="reopened parent")
        tid = kb.create_task(conn, title="finished child", parents=[parent])
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
        assert kb.get_task(conn, parent).status != "done"

    assert _run_kanban("complete", tid) == 1
    error = capsys.readouterr().err
    assert f"already terminal (status is {status})" in error
    assert "unsatisfied parents" not in error


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
    if source == "db":
        assert str(kanban_home / "isolated.db") in error
        assert "pinned by HERMES_KANBAN_DB" in error
        assert f"board {board!r}" not in error
    else:
        assert f"board {board!r}" in error
        assert f"resolved from {expected_source}" in error


@pytest.mark.parametrize("command", [
    ["comment", "missing-task", "hello"],
    ["link", "missing-task", "other-missing"],
    ["context", "missing-task"],
])
def test_kanban_db_unknown_task_errors_name_board(kanban_home, capsys, command):
    kb.create_board("example")
    assert _run_kanban("--board", "example", *command) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown task" in captured.err
    assert "missing-task" in captured.err
    assert "board 'example'" in captured.err
    assert "resolved from --board" in captured.err


def test_kanban_pinned_db_ignores_board_selection(kanban_home, monkeypatch, capsys):
    kb.create_board("example")
    pinned = kanban_home / "isolated.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))

    assert _run_kanban("--board", "example", "show", "missing-task") == 1
    error = capsys.readouterr().err
    assert str(pinned) in error
    assert "pinned by HERMES_KANBAN_DB" in error
    assert "board selection ignored" in error
    assert "board 'example'" not in error


def test_resolve_current_board_with_source_matches_get_current_board(
    kanban_home, monkeypatch
):
    kb.create_board("example")
    kb.create_board("other")
    assert kb.resolve_current_board_with_source() == ("default", kb.BOARD_SOURCE_DEFAULT)
    kb.set_current_board("other")
    assert kb.resolve_current_board_with_source() == ("other", kb.BOARD_SOURCE_CURRENT_FILE)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "example")
    assert kb.resolve_current_board_with_source() == ("example", kb.BOARD_SOURCE_ENV)
    with kb.scoped_current_board("other"):
        assert kb.resolve_current_board_with_source() == ("other", kb.BOARD_SOURCE_OVERRIDE)
        assert kb.get_current_board() == "other"
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "missing-board")
    assert kb.resolve_current_board_with_source() == ("other", kb.BOARD_SOURCE_CURRENT_FILE)
    assert kb.get_current_board() == "other"


def test_explain_complete_refusal_is_shared_by_worker_tool(kanban_home, monkeypatch):
    import json
    from tools import kanban_tools

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unfinished specification", triage=True)
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        conn.commit()
        expected = kb.explain_complete_refusal(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    result = json.loads(kanban_tools._handle_complete({"summary": "done"}))
    assert expected in result["error"]
    assert "unknown id or already terminal" not in result["error"]
    assert "status is triage" in result["error"]


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




# ---------------------------------------------------------------------------
# Specific refusal reasons for reclaim / reassign / edit / block / archive
# ---------------------------------------------------------------------------


def _set_status(tid, status):
    with kb.connect_closing() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, block_reason=? WHERE id=?",
            (status, "waiting" if status == "blocked" else None, tid),
        )
        conn.commit()


_UNKNOWN_ID_COMMANDS = {
    "reclaim": ["reclaim", "missing-task"],
    "reassign": ["reassign", "missing-task", "example-profile"],
    "edit": ["edit", "missing-task", "--result", "backfilled"],
    "block": ["block", "missing-task", "waiting"],
    "archive": ["archive", "missing-task"],
}


@pytest.mark.parametrize("verb", sorted(_UNKNOWN_ID_COMMANDS))
def test_kanban_refusal_unknown_id_names_board(kanban_home, capsys, verb):
    kb.create_board("example")
    assert _run_kanban("--board", "example", *_UNKNOWN_ID_COMMANDS[verb]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"cannot {verb}" in captured.err
    assert "missing-task" in captured.err
    assert "unknown task id on board 'example'" in captured.err
    assert "resolved from --board" in captured.err
    assert "unknown id or" not in captured.err


@pytest.mark.parametrize("status", ["todo", "done", "archived"])
def test_kanban_refusal_reclaim_wrong_status(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="not reclaimable")
    _set_status(tid, status)

    assert _run_kanban("reclaim", tid) == 1
    captured = capsys.readouterr()
    accepted = "/".join(kb.RECLAIM_ACCEPTED_STATUSES)
    assert captured.out == ""
    if status == "todo":
        assert f"status is todo; reclaim accepts {accepted}" in captured.err
    else:
        assert f"already terminal (status is {status}); reclaim accepts {accepted}" in captured.err
    assert "unknown" not in captured.err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == status


def test_kanban_refusal_reclaim_ready_without_claim(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="idle task")
    assert _run_kanban("reclaim", tid) == 1
    assert "no active claim to reclaim" in capsys.readouterr().err


def test_kanban_refusal_reassign_running_claimed(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="busy task", assignee="example-profile")
        assert kb.claim_task(conn, tid) is not None

    assert _run_kanban("reassign", tid, "other-profile") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"cannot reassign {tid}: status is running (claimed by a worker)" in captured.err
    assert "reassign accepts every status except running" in captured.err
    assert "--reclaim" in captured.err
    assert "unknown" not in captured.err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "example-profile"

    # Success path unchanged: --reclaim releases the claim, then reassigns.
    assert _run_kanban("reassign", tid, "other-profile", "--reclaim") == 0
    assert capsys.readouterr().out == f"Reassigned {tid} to other-profile (claim reclaimed)\n"


@pytest.mark.parametrize("status", ["todo", "ready", "archived"])
def test_kanban_refusal_edit_wrong_status(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="not done")
    _set_status(tid, status)

    assert _run_kanban("edit", tid, "--result", "backfilled") == 1
    captured = capsys.readouterr()
    accepted = "/".join(kb.EDIT_ACCEPTED_STATUSES)
    assert captured.out == ""
    prefix = "already terminal (status is archived)" if status == "archived" else f"status is {status}"
    assert f"cannot edit {tid}: {prefix}; edit accepts {accepted}" in captured.err
    assert "unknown" not in captured.err


@pytest.mark.parametrize("status", ["todo", "blocked", "done"])
def test_kanban_refusal_block_wrong_status(kanban_home, capsys, status):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="not blockable")
    _set_status(tid, status)

    assert _run_kanban("block", tid, "waiting") == 1
    captured = capsys.readouterr()
    accepted = "/".join(kb.BLOCK_ACCEPTED_STATUSES)
    assert captured.out == ""
    prefix = "already terminal (status is done)" if status == "done" else f"status is {status}"
    assert f"cannot block {tid}: {prefix}; block accepts {accepted}" in captured.err
    assert "unknown" not in captured.err


def test_kanban_refusal_block_stale_run(kanban_home, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="newer run")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "123")

    assert _run_kanban("block", tid, "waiting") == 1
    assert "run id mismatch" in capsys.readouterr().err


def test_kanban_refusal_archive_already_archived(kanban_home, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="old task")
        assert kb.archive_task(conn, tid)

    assert _run_kanban("archive", tid) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"cannot archive {tid}: already terminal (status is archived)" in captured.err
    assert "unknown" not in captured.err


def test_refusal_status_sets_match_sql_guards(kanban_home):
    """The shared constants decide each verb's outcome, not a second copy."""
    outcomes = {
        "edit": (kb.EDIT_ACCEPTED_STATUSES,
                 lambda c, t: kb.edit_completed_task_result(c, t, result="r")),
        "archive": (kb.ARCHIVE_ACCEPTED_STATUSES, kb.archive_task),
        "block": (kb.BLOCK_ACCEPTED_STATUSES,
                  lambda c, t: kb.block_task(c, t, reason="r")),
    }
    for verb, (accepted, run) in outcomes.items():
        for status in kb.STATUS_ORDER:
            with kb.connect_closing() as conn:
                tid = kb.create_task(conn, title=f"{verb} {status}")
            _set_status(tid, status)
            with kb.connect_closing() as conn:
                assert run(conn, tid) == (status in accepted), (verb, status)
