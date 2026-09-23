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
# create --goal TEXT / --parent validation (issue #42)
# ---------------------------------------------------------------------------


def _build_hermes_like_parser():
    """A throwaway root parser with the kanban subtree attached the same way
    hermes_cli.main attaches it (build_parser over a subparsers action)."""
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def test_create_goal_text_appends_goal_heading_and_bare_flag_unchanged(kanban_home):
    """Issue #42 bug 1: ``--goal "<sentence>"`` used to die with exit 2
    "unrecognized arguments" raised by the TOP-LEVEL parser (because --goal
    was store_true), creating nothing. The sentence must instead be accepted
    and appended to the body under a ``## Goal`` heading, while a bare
    ``--goal`` keeps enabling goal mode without touching the body."""
    parser = _build_hermes_like_parser()

    goal_text = (
        "Ownership resolves when the bag stamp is within the scan's own "
        "duration; state and age ledgered; red test first"
    )

    args = parser.parse_args(
        ["kanban", "create", "stamp ownership", "--body", "seed brief",
         "--goal", goal_text, "--json"]
    )
    rc = kc.kanban_command(args)
    assert rc == 0, f"create --goal '<text>' must succeed, got exit {rc}"

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, kb.list_tasks(conn, limit=1)[0].id)
    assert task is not None
    assert task.goal_mode is True
    assert "## Goal" in (task.body or "")
    assert goal_text in (task.body or "")
    # The original body is preserved ahead of the appended heading.
    assert (task.body or "").startswith("seed brief")

    # Bare --goal: unchanged — goal mode on, no heading injected.
    args_bare = parser.parse_args(
        ["kanban", "create", "bare goal card", "--body", "plain", "--goal", "--json"]
    )
    assert kc.kanban_command(args_bare) == 0
    with kb.connect_closing() as conn:
        rows = [t for t in kb.list_tasks(conn, limit=10) if t.title == "bare goal card"]
        assert len(rows) == 1
        assert rows[0].goal_mode is True
        assert "## Goal" not in (rows[0].body or "")
        assert rows[0].body == "plain"


def test_create_rejects_empty_parent_values(kanban_home, capsys):
    """Issue #42 bug 2: ``--parent ""`` (an unset shell variable) used to be
    silently dropped and the card created parentless in ready — ahead of the
    card it depended on. Empty/whitespace --parent values must be rejected at
    create time with exit 2, the message must name --parent, and no card may
    be created."""
    parser = _build_hermes_like_parser()

    for bad in ("", "   "):
        args = parser.parse_args(
            ["kanban", "create", "orphan risk", "--parent", bad, "--json"]
        )
        rc = kc.kanban_command(args)
        captured = capsys.readouterr()
        assert rc == 2, f"--parent {bad!r} must exit 2, got {rc}"
        assert "--parent" in captured.err, captured.err

    with kb.connect_closing() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    assert count == 0, "no card may be created from a rejected --parent"

    # The DB layer must not silently drop empty parents either — it is the
    # durable trust boundary (children can import kanban_db directly).
    import pytest

    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="parent"):
            kb.create_task(conn, title="db api orphan risk", parents=[""])


