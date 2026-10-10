"""Board-level ``allowed_assignees`` (board.json).

A board that serves one contained profile (e.g. a mail-drafting board whose only
legitimate worker is ``mail-drafter``) must not become a route to a terminal-capable
profile: a prompt-injected worker could otherwise ``kanban_create`` a ready card for
``coder``, or a card could be reassigned/unblocked onto one. The list is enforced at
create (CLI, ``kanban_create`` tool, dashboard, swarm) and again by the dispatcher
before every spawn. No list = today's behaviour.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    for prof in ("mail-drafter", "coder"):
        (root / "profiles" / prof).mkdir(parents=True)
        (root / "profiles" / prof / "config.yaml").write_text("model: x\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    return root


def _set_allowed(board: str, value) -> None:
    path = kb.board_metadata_path(board)
    meta = json.loads(path.read_text(encoding="utf-8"))
    if value is _UNSET:
        meta.pop("allowed_assignees", None)
    else:
        meta["allowed_assignees"] = value
    path.write_text(json.dumps(meta), encoding="utf-8")


_UNSET = object()


@pytest.fixture
def mail_board(home):
    kb.create_board("mail")
    _set_allowed("mail", ["mail-drafter"])
    return "mail"


@pytest.fixture
def conn(mail_board):
    c = kbc.connect(board=mail_board)
    yield c
    c.close()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


# --- reading the list --------------------------------------------------------------

def test_absent_list_means_no_restriction(home):
    kb.create_board("open")
    assert kb.board_allowed_assignees(board="open") is None
    c = kbc.connect(board="open")
    try:
        tid = kb.create_task(c, title="anything", assignee="coder")
        assert kb.get_task(c, tid).status == "ready"
    finally:
        c.close()


def test_list_is_read_from_the_board_the_connection_has_open(conn, home):
    # The current board is ``default`` (unrestricted); the conn is on ``mail``.
    assert kb.get_current_board() == "default"
    assert kb.board_slug_for_conn(conn) == "mail"
    assert kb.board_allowed_assignees(conn) == frozenset({"mail-drafter"})


def test_names_are_canonicalised(home):
    kb.create_board("mixed")
    _set_allowed("mixed", [" Mail-Drafter "])
    assert kb.board_allowed_assignees(board="mixed") == frozenset({"mail-drafter"})


@pytest.mark.parametrize("bad", [{"mail-drafter": True}, [1, 2], ["ok", ""], 7])
def test_malformed_list_fails_closed(home, bad):
    kb.create_board("broken")
    _set_allowed("broken", bad)
    assert kb.board_allowed_assignees(board="broken") == frozenset()


# --- create ------------------------------------------------------------------------

def test_create_accepts_an_allowed_assignee(conn):
    tid = kb.create_task(conn, title="draft", assignee="mail-drafter")
    assert kb.get_task(conn, tid).assignee == "mail-drafter"


def test_create_refuses_a_disallowed_assignee_by_name(conn):
    with pytest.raises(ValueError) as exc:
        kb.create_task(conn, title="escalate", assignee="coder")
    msg = str(exc.value)
    assert "coder" in msg and "mail-drafter" in msg and "'mail'" in msg
    assert _count(conn) == 0


def test_human_parked_cards_are_exempt_and_still_never_dispatch(conn):
    tid = kb.create_task(conn, title="decide", assignee="hayden", initial_status="blocked",
                         block_reason="waiting on a human")
    assert kb.get_task(conn, tid).status == "blocked"
    tid2 = kb.create_task(conn, title="look", assignee="coder", triage=True)
    assert kb.get_task(conn, tid2).status == "triage"


def test_empty_list_allows_nobody(home):
    kb.create_board("closed")
    _set_allowed("closed", [])
    c = kbc.connect(board="closed")
    try:
        with pytest.raises(ValueError, match="nobody"):
            kb.create_task(c, title="x", assignee="mail-drafter")
    finally:
        c.close()


def test_cli_create_refuses_a_disallowed_assignee(mail_board, capsys):
    from hermes_cli import kanban as kc

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    refused = kc.kanban_command(parser.parse_args(
        ["kanban", "--board", mail_board, "create", "escalate", "--assignee", "coder"]))
    assert refused != 0
    assert "not allowed on board 'mail'" in capsys.readouterr().err
    ok = kc.kanban_command(parser.parse_args(
        ["kanban", "--board", mail_board, "create", "draft", "--assignee", "mail-drafter"]))
    assert ok == 0
    c = kbc.connect(board=mail_board)
    try:
        assert [r["assignee"] for r in c.execute("SELECT assignee FROM tasks")] == ["mail-drafter"]
    finally:
        c.close()


def test_kanban_create_tool_refuses_a_disallowed_assignee(conn, monkeypatch, mail_board):
    own = kb.create_task(conn, title="draft", assignee="mail-drafter")
    kb.claim_task(conn, own)
    monkeypatch.setenv("HERMES_KANBAN_TASK", own)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", mail_board)
    monkeypatch.setenv("HERMES_PROFILE", "mail-drafter")
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_create({"title": "run a shell", "assignee": "coder"}))
    assert "error" in out and "not allowed" in out["error"]
    assert _count(conn) == 1


def test_swarm_with_a_disallowed_worker_is_refused_whole(conn):
    from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm

    with pytest.raises(ValueError, match="coder"):
        create_swarm(
            conn, goal="reply to the thread",
            workers=[SwarmWorkerSpec(profile="mail-drafter", title="draft", body="b"),
                     SwarmWorkerSpec(profile="coder", title="shell out", body="b")],
            verifier_assignee="mail-drafter", synthesizer_assignee="mail-drafter",
            created_by="mail-drafter",
        )
    assert _count(conn) == 0, "a refused swarm left part of its graph behind"


# --- dispatch ----------------------------------------------------------------------

def _spawned(calls):
    return [t.id for t in calls]


def _tick(conn, board="mail", **kw):
    calls = []

    def spawn(task, workspace, board=None):
        calls.append(task)
        return None

    res = kbd.dispatch_once(conn, spawn_fn=spawn, board=board, **kw)
    return res, calls


def test_dispatch_refuses_a_card_whose_assignee_is_not_allowed(conn, mail_board):
    # Created while the board was open, then the board was locked down: the card
    # reaches the lane without passing the create check.
    _set_allowed(mail_board, _UNSET)
    bad = kb.create_task(conn, title="slipped in", assignee="coder")
    good = kb.create_task(conn, title="draft", assignee="mail-drafter")
    _set_allowed(mail_board, ["mail-drafter"])

    res, calls = _tick(conn)
    assert _spawned(calls) == [good]
    assert res.skipped_assignee_not_allowed == [(bad, "coder")]
    task = kb.get_task(conn, bad)
    assert task.status == "blocked"
    assert "not allowed on board 'mail'" in task.block_reason
    kinds = [e.kind for e in kb.list_events(conn, bad)]
    assert "assignee_not_allowed" in kinds

    # The refusal is durable: the next tick neither spawns nor re-records it.
    res2, calls2 = _tick(conn)
    assert calls2 == [] and res2.skipped_assignee_not_allowed == []
    assert [e.kind for e in kb.list_events(conn, bad)].count("assignee_not_allowed") == 1


def test_dispatch_refuses_a_reassigned_card(conn):
    tid = kb.create_task(conn, title="draft", assignee="mail-drafter")
    kb.assign_task(conn, tid, "coder")
    res, calls = _tick(conn)
    assert calls == []
    assert res.skipped_assignee_not_allowed == [(tid, "coder")]


def test_dispatch_dry_run_reports_without_writing(conn, mail_board):
    _set_allowed(mail_board, _UNSET)
    bad = kb.create_task(conn, title="slipped in", assignee="coder")
    _set_allowed(mail_board, ["mail-drafter"])
    res, calls = _tick(conn, dry_run=True)
    assert res.skipped_assignee_not_allowed == [(bad, "coder")]
    assert kb.get_task(conn, bad).status == "ready"
    assert "assignee_not_allowed" not in [e.kind for e in kb.list_events(conn, bad)]


def test_dispatch_is_unchanged_without_a_list(home):
    kb.create_board("open")
    c = kbc.connect(board="open")
    try:
        tid = kb.create_task(c, title="anything", assignee="coder")
        res, calls = _tick(c, board="open")
        assert _spawned(calls) == [tid]
        assert res.skipped_assignee_not_allowed == []
    finally:
        c.close()


def test_dispatch_reads_the_list_of_the_connections_board_not_the_current_one(conn, mail_board):
    _set_allowed(mail_board, _UNSET)
    bad = kb.create_task(conn, title="slipped in", assignee="coder")
    _set_allowed(mail_board, ["mail-drafter"])
    # board=None: the tick must still apply mail's list, because conn is on mail.
    res, calls = _tick(conn, board=None)
    assert calls == []
    assert res.skipped_assignee_not_allowed == [(bad, "coder")]


# --- a board.json that does not parse ----------------------------------------------

def test_unparseable_board_json_fails_closed(conn, mail_board):
    """A hand-edit typo (here a trailing comma) must not silently lift the list."""
    kb.board_metadata_path(mail_board).write_text(
        '{"slug": "mail", "allowed_assignees": ["mail-drafter"],}', encoding="utf-8")
    assert kb.board_allowed_assignees(conn) == frozenset()
    with pytest.raises(ValueError, match="nobody"):
        kb.create_task(conn, title="escalate", assignee="coder")
    res, calls = _tick(conn)
    assert calls == []


def test_non_object_board_json_fails_closed(home):
    kb.create_board("listy")
    kb.board_metadata_path("listy").write_text('["mail-drafter"]', encoding="utf-8")
    assert kb.board_allowed_assignees(board="listy") == frozenset()


def test_unparseable_board_json_is_not_rewritten_without_the_list(mail_board):
    path = kb.board_metadata_path(mail_board)
    broken = '{"allowed_assignees": ["mail-drafter"],}'
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        kb.write_board_metadata(mail_board, name="renamed")
    assert path.read_text(encoding="utf-8") == broken


# --- review lane --------------------------------------------------------------------

def _to_review(conn, tid, **kw):
    claimed = kb.claim_task(conn, tid)
    ok, reason = kb.request_review(
        conn, tid, summary="done", expected_run_id=claimed.current_run_id,
        with_reason=True, **kw)
    return ok, reason


def test_request_review_refuses_a_disallowed_reviewer(conn):
    """A contained worker must not route its card onto a terminal-capable profile
    by naming it as reviewer."""
    tid = kb.create_task(conn, title="draft", assignee="mail-drafter")
    ok, reason = _to_review(conn, tid, reviewer="coder")
    assert ok is False and "not allowed on board 'mail'" in reason
    task = kb.get_task(conn, tid)
    assert task.assignee == "mail-drafter" and task.status == "running"


def test_request_review_tool_refuses_a_disallowed_reviewer(conn, monkeypatch, mail_board):
    tid = kb.create_task(conn, title="draft", assignee="mail-drafter")
    claimed = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", mail_board)
    monkeypatch.setenv("HERMES_PROFILE", "mail-drafter")
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_request_review({"summary": "done", "reviewer": "coder"}))
    assert "error" in out and "not allowed" in out["error"]
    assert kb.get_task(conn, tid).assignee == "mail-drafter"


def test_request_review_with_an_allowed_reviewer_is_unchanged(conn):
    tid = kb.create_task(conn, title="draft", assignee="mail-drafter")
    ok, reason = _to_review(conn, tid, reviewer="mail-drafter")
    assert ok is True, reason
    assert kb.get_task(conn, tid).status == "review"


def test_dispatch_holds_a_review_card_whose_assignee_is_not_allowed(conn, mail_board):
    """A review card that reached the lane with a disallowed assignee (here the
    board was locked down after review was requested) is moved out of review, so it
    neither spawns nor holds a review slot back from the ready lane every tick."""
    _set_allowed(mail_board, _UNSET)
    rev = kb.create_task(conn, title="review me", assignee="mail-drafter")
    ok, reason = _to_review(conn, rev, reviewer="coder")
    assert ok is True, reason
    _set_allowed(mail_board, ["mail-drafter"])
    good = kb.create_task(conn, title="draft", assignee="mail-drafter")

    # The refused review card reserves no slot: the ready card spawns on the
    # first tick even with max_spawn=1.
    res, calls = _tick(conn, max_spawn=1)
    assert _spawned(calls) == [good], "the ready lane was starved by a refused review card"
    # Once the review loop has budget (the max_spawn slot is taken by the running
    # card, so tick uncapped) it holds the card, which leaves the lane.
    res, calls = _tick(conn)
    assert calls == []
    assert res.skipped_assignee_not_allowed == [(rev, "coder")]
    res, calls = _tick(conn)
    assert res.skipped_assignee_not_allowed == []
    task = kb.get_task(conn, rev)
    assert task.status == "blocked"
    assert "not allowed on board 'mail'" in task.block_reason
    assert [e.kind for e in kb.list_events(conn, rev)].count("assignee_not_allowed") == 1
    assert kbd.has_spawnable_review(conn) is False

    # Unblocking after widening the list resumes review, not ready.
    _set_allowed(mail_board, ["mail-drafter", "coder"])
    assert kb.unblock_task(conn, rev) is True
    assert kb.get_task(conn, rev).status == "review"
