"""``--reason`` on every reason-taking kanban verb, and trailing reasons (#125).

* ``block``, ``schedule``, ``promote`` and ``request-changes`` accept
  ``--reason "<text>"`` beside their existing positional reason; giving both
  is refused before anything changes.
* A positional reason placed after ``--ids``/``--kind`` is kept, not rejected
  as "unrecognized arguments" (argparse on Python 3.11 consumes an empty
  ``nargs='*'`` positional before the options).
* A multi-word reason swallowed by ``--ids`` is refused instead of being
  treated as a task id.
* ``reclaim``, ``unblock``, ``reopen-review`` keep their ``--reason``.
* ``block`` on a triage card says why it is refused and what to do instead.
"""

from __future__ import annotations

import argparse
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
    for var in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _parser():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return parser


def _run_kanban(*tokens):
    return kc.kanban_command(_parser().parse_args(["kanban", *tokens]))


def _new_tasks(count):
    with kb.connect_closing() as conn:
        return [kb.create_task(conn, title=f"task {i}") for i in range(count)]


def _set_status(tid, status):
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()


def _task(tid):
    with kb.connect_closing() as conn:
        return kb.get_task(conn, tid)


def _comments(tid):
    with kb.connect_closing() as conn:
        return [c.body for c in kb.list_comments(conn, tid)]


# ---------------------------------------------------------------------------
# block
# ---------------------------------------------------------------------------


def test_block_accepts_reason_flag(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    assert _run_kanban("block", tid, "--reason", "waiting on Hayden") == 0
    assert capsys.readouterr().out == f"Blocked {tid}: waiting on Hayden\n"
    assert _task(tid).status == "blocked"
    assert _comments(tid)[-1] == "BLOCKED: waiting on Hayden"


def test_block_reason_flag_with_ids_and_kind(kanban_home, capsys):
    tid, other = _new_tasks(2)
    assert _run_kanban(
        "block", tid, "--ids", other, "--kind", "needs_input",
        "--reason", "Parked for a decision",
    ) == 0
    for t in (tid, other):
        assert _task(t).status == "blocked"
        assert _comments(t)[-1] == "BLOCKED: Parked for a decision"


@pytest.mark.parametrize("order", ["ids-then-kind", "kind-only"])
def test_block_positional_reason_after_options_is_kept(kanban_home, capsys, order):
    tid, other = _new_tasks(2)
    if order == "ids-then-kind":
        tokens = ["block", tid, "--ids", other, "--kind", "needs_input", "Parked for a decision"]
        blocked = (tid, other)
    else:
        tokens = ["block", tid, "--kind", "needs_input", "Parked for a decision"]
        blocked = (tid,)
    assert _run_kanban(*tokens) == 0
    for t in blocked:
        assert _task(t).status == "blocked"
        assert _comments(t)[-1] == "BLOCKED: Parked for a decision"


def test_block_documented_positional_then_ids_still_works(kanban_home, capsys):
    tid, a, b = _new_tasks(3)
    assert _run_kanban("block", tid, "need input", "--ids", a, b) == 0
    for t in (tid, a, b):
        assert _task(t).status == "blocked"
        assert _comments(t)[-1] == "BLOCKED: need input"


def test_block_refuses_both_positional_and_flag_reason(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    assert _run_kanban("block", tid, "one reason", "--reason", "another") == 2
    err = capsys.readouterr().err
    assert "--reason" in err and "not both" in err
    assert _task(tid).status == "ready"
    assert _comments(tid) == []


def test_block_refuses_reason_swallowed_by_ids(kanban_home, capsys):
    tid, other = _new_tasks(2)
    assert _run_kanban(
        "block", tid, "--kind", "needs_input", "--ids", other, "Parked for a decision",
    ) == 2
    err = capsys.readouterr().err
    assert "'Parked for a decision' is not a task id" in err
    assert "--reason" in err
    for t in (tid, other):
        assert _task(t).status == "ready"
        assert _comments(t) == []


def test_block_triage_refusal_explains_why_and_what_to_do(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    _set_status(tid, "triage")
    assert _run_kanban("block", tid, "--reason", "needs Hayden") == 1
    err = capsys.readouterr().err
    assert f"cannot block {tid}: status is triage" in err
    assert "block accepts running/ready/review" in err
    assert "kanban comment" in err
    assert "kanban specify" in err
    assert _task(tid).status == "triage"


def test_explain_refusal_triage_block_names_next_step(kanban_home):
    (tid,) = _new_tasks(1)
    _set_status(tid, "triage")
    with kb.connect_closing() as conn:
        reason = kb.explain_refusal(conn, tid, "block")
    assert reason.startswith("status is triage; block accepts running/ready/review")
    assert "kanban comment" in reason and "kanban specify" in reason


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


def test_schedule_accepts_reason_flag(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    assert _run_kanban("schedule", tid, "--reason", "after 9am Monday") == 0
    assert _task(tid).status == "scheduled"
    assert _comments(tid)[-1] == "SCHEDULED: after 9am Monday"


def test_schedule_positional_reason_after_ids_is_kept(kanban_home, capsys):
    tid, other = _new_tasks(2)
    assert _run_kanban("schedule", tid, "--ids", other, "--board", "default", "after lunch") == 0
    for t in (tid, other):
        assert _task(t).status == "scheduled"
        assert _comments(t)[-1] == "SCHEDULED: after lunch"


def test_schedule_refuses_both_reasons(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    assert _run_kanban("schedule", tid, "later", "--reason", "sooner") == 2
    assert "not both" in capsys.readouterr().err
    assert _task(tid).status == "ready"


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def _last_promote_reason(tid):
    with kb.connect_closing() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "promoted_manual"]
    return events[-1].payload.get("reason") if events else None


def test_promote_accepts_reason_flag(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    _set_status(tid, "todo")
    assert _run_kanban("promote", tid, "--reason", "parent landed") == 0
    assert _task(tid).status == "ready"
    assert _last_promote_reason(tid) == "parent landed"


def test_promote_positional_reason_still_works(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    _set_status(tid, "todo")
    assert _run_kanban("promote", tid, "parent", "landed") == 0
    assert _last_promote_reason(tid) == "parent landed"


def test_promote_refuses_both_reasons(kanban_home, capsys):
    (tid,) = _new_tasks(1)
    _set_status(tid, "todo")
    assert _run_kanban("promote", tid, "one", "--reason", "two") == 2
    assert "not both" in capsys.readouterr().err
    assert _task(tid).status == "todo"


# ---------------------------------------------------------------------------
# request-changes
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_request_changes(monkeypatch):
    calls = []

    def fake(conn, task_id, *, reason, expected_run_id=None):
        calls.append((task_id, reason))
        return True, None

    monkeypatch.setattr(kb, "request_changes", fake)
    return calls


@pytest.mark.parametrize("tokens", [
    ["--reason", "add a regression test"],
    ["add", "a", "regression", "test"],
])
def test_request_changes_accepts_flag_or_positional(
    kanban_home, capsys, captured_request_changes, tokens,
):
    assert _run_kanban("request-changes", "t_review", *tokens) == 0
    assert captured_request_changes == [("t_review", "add a regression test")]


def test_request_changes_requires_a_reason(kanban_home, capsys, captured_request_changes):
    assert _run_kanban("request-changes", "t_review") == 2
    assert "reason is required" in capsys.readouterr().err
    assert captured_request_changes == []


def test_request_changes_refuses_both_reasons(kanban_home, capsys, captured_request_changes):
    assert _run_kanban("request-changes", "t_review", "one", "--reason", "two") == 2
    assert "not both" in capsys.readouterr().err
    assert captured_request_changes == []


# ---------------------------------------------------------------------------
# Every reason-recording verb parses --reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tokens", [
    ["block", "t_a"],
    ["block", "t_a", "--ids", "t_b", "--kind", "dependency"],
    ["schedule", "t_a"],
    ["promote", "t_a"],
    ["reclaim", "t_a"],
    ["unblock", "t_a"],
    ["reopen-review", "t_a"],
    ["request-changes", "t_a"],
])
def test_every_reason_verb_parses_reason_flag(tokens):
    args = _parser().parse_args(["kanban", *tokens, "--reason", "why it waits"])
    assert kc._cli_reason(args) == ("why it waits", None)
