"""Issue #125: ``--reason`` flag on every kanban verb that records a reason.

``block``, ``schedule`` and ``promote`` took the reason as a trailing
positional (``nargs="*"``), ``request-changes`` as ``nargs="+"`` — with
argparse's greedy first positional chunk that means a reason placed AFTER an
option (``block T --ids U --kind dependency "text"``) is left over and
rejected, and none of them accept ``--reason`` like ``reclaim``/``unblock``/
``reopen-review`` do. These tests pin the unified contract:

* every reason-recording verb accepts ``--reason "<text>"``;
* a positional reason works wherever an option sits before it;
* what gets recorded (comment / event payload / block state) is identical
  whichever form was used;
* positional reason AND ``--reason`` together is a usage error (never a
  silent pick);
* ``request-changes`` keeps requiring a reason, satisfiable by either form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_ready_tasks(conn, *titles: str) -> list[str]:
    ids = [kb.create_task(conn, title=t) for t in titles]
    with kb.write_txn(conn):
        for tid in ids:
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return ids


def _comments(conn, tid: str) -> list[str]:
    return [c.body for c in kb.list_comments(conn, tid)]


def _event_payloads(conn, tid: str, kind: str) -> list[dict]:
    return [
        e.payload or {}
        for e in kb.list_events(conn, tid) if e.kind == kind
    ]


# ---------------------------------------------------------------------------
# (a) every reason-recording verb accepts --reason
# ---------------------------------------------------------------------------


def test_block_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "b1")[0]
    out = kc.run_slash(f'block {tid} --reason "flag form reason"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "blocked"
        assert task.block_reason == "flag form reason"
        assert "BLOCKED: flag form reason" in _comments(conn, tid)
        assert _event_payloads(conn, tid, "blocked")[-1].get("reason") == "flag form reason"


def test_schedule_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "s1")[0]
    out = kc.run_slash(f'schedule {tid} --reason "flag form note"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "scheduled"
        assert "SCHEDULED: flag form note" in _comments(conn, tid)
        assert _event_payloads(conn, tid, "scheduled")[-1].get("reason") == "flag form note"


def test_promote_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        # Born blocked (the R3-gate shape) — promote_task accepts todo/blocked.
        tid = kb.create_task(conn, title="p1", initial_status="blocked",
                             block_reason="awaiting human ops")
    out = kc.run_slash(f'promote {tid} --reason "flag form audit" --dry-run')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        assert not _event_payloads(conn, tid, "promoted_manual")  # dry-run never mutates
    out = kc.run_slash(f'promote {tid} --reason "flag form audit"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        payloads = _event_payloads(conn, tid, "promoted_manual")
        assert payloads, "expected a promoted_manual event"
        assert payloads[-1].get("reason") == "flag form audit"


def test_request_changes_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rc1", assignee="builder")
        run = kb.claim_task(conn, tid, claimer="builder:1")
        assert run is not None
        assert kb.request_review(
            conn, tid, summary="s", reviewer="reviewer",
            expected_run_id=run.current_run_id, with_reason=True,
        )
        review = kb.claim_review_task(conn, tid, claimer="reviewer:1")
        assert review is not None
    out = kc.run_slash(f'request-changes {tid} --reason "flag form changes"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        payloads = _event_payloads(conn, tid, "changes_requested")
        assert payloads, "expected a changes_requested event"
        assert payloads[-1].get("reason") == "flag form changes"


def test_reclaim_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "r1")[0]
        claimed = kb.claim_task(conn, tid, claimer="w")
        assert claimed is not None
    out = kc.run_slash(f'reclaim {tid} --reason "stale claim"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"


def test_unblock_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "u1")[0]
        assert kb.block_task(conn, tid, reason="x")
    out = kc.run_slash(f'unblock {tid} --reason "input arrived"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert "UNBLOCK: input arrived" in _comments(conn, tid)


def test_reopen_review_accepts_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rr1", assignee="builder")
        run = kb.claim_task(conn, tid, claimer="builder:1")
        assert run is not None
        assert kb.request_review(
            conn, tid, summary="s", expected_run_id=run.current_run_id,
            with_reason=True,
        )
    out = kc.run_slash(f'reopen-review {tid} --reason "redo the tests"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert "CHANGES REQUESTED: redo the tests" in _comments(conn, tid)


# ---------------------------------------------------------------------------
# (b) issue failing call 2: positional reason AFTER options records on both
#     tasks (dependency blocks park in todo; block_reason stays NULL by
#     contract — the reason lives in the comment + dependency_wait event)
# ---------------------------------------------------------------------------


def test_block_reason_after_options_records_on_both_tasks(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "first", "second")
    out = kc.run_slash(
        f'block {first} --ids {second} --kind dependency "Parent merge pending"'
    )
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "todo"  # dependency waits in todo
            assert task.block_reason is None  # stamped only on the blocked route
            assert "BLOCKED: Parent merge pending" in _comments(conn, tid)
            payload = _event_payloads(conn, tid, "dependency_wait")[-1]
            assert payload.get("reason") == "Parent merge pending"
            assert payload.get("kind") == "dependency"


# ---------------------------------------------------------------------------
# (c) issue failing call 1: block T U --reason "..." blocks both
# ---------------------------------------------------------------------------


def test_block_two_positional_ids_with_reason_flag(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "two-a", "two-b")
    out = kc.run_slash(f'block {first} {second} --reason "waiting on upstream"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.status == "blocked"
            assert task.block_reason == "waiting on upstream"
            assert "BLOCKED: waiting on upstream" in _comments(conn, tid)


# ---------------------------------------------------------------------------
# (d) positional reason AND --reason together → usage error, never silent pick
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_positional_plus_reason_flag_is_usage_error(
    kanban_home: Path, verb: str
) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="dup")  # no parents -> ready
    out = kc.run_slash(f'{verb} {tid} "positional text" --reason "flag text"')
    assert "usage error" in out, out
    # The error names BOTH competing forms so the user sees the conflict.
    assert "positional text" in out and "flag text" in out, out
    # Nothing was mutated: the error precedes any handler run.
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"


# ---------------------------------------------------------------------------
# (e) legacy workaround still works: reason right after the task id
# ---------------------------------------------------------------------------


def test_positional_reason_right_after_id_still_works(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "legacy-a", "legacy-b")
    out = kc.run_slash(f'block {first} "plain positional reason"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, first)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_reason == "plain positional reason"
        assert not _comments(conn, second)  # single-task form never spreads


def test_positional_reason_after_kind_single_task(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "single-b")[0]
    out = kc.run_slash(f'block {tid} --kind dependency "after kind"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "todo"  # dependency blocks park in todo
        assert "BLOCKED: after kind" in _comments(conn, tid)


def test_schedule_non_id_words_with_reason_flag_is_usage_error(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "single")[0]
    out = kc.run_slash(f'schedule {tid} --reason "later" "also positional"')
    assert "usage error" in out, out


# ---------------------------------------------------------------------------
# request-changes keeps REQUIRING a reason, either form
# ---------------------------------------------------------------------------


def test_request_changes_without_any_reason_is_an_error(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rc2")  # ready; never claimed
    out = kc.run_slash(f'request-changes {tid}')
    # Base enforced this in argparse (nargs="+"); now the handler enforces it so
    # --reason can satisfy it. Either wording proves "still required".
    assert ("request-changes: a reason is required" in out
            or "the following arguments are required: reason" in out), out
    # An empty flag does not satisfy the requirement either.
    out = kc.run_slash(f'request-changes {tid} --reason ""')
    assert "request-changes: a reason is required" in out, out


def test_unknown_option_is_still_a_usage_error(kanban_home: Path) -> None:
    """Intermixed parsing must not swallow a genuinely unknown option: it reaches
    the top-level parser as unrecognized and nothing is mutated."""
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "bogus")[0]
    out = kc.run_slash(f'block {tid} "a reason" --bogus')
    assert "usage error" in out, out
    assert "unrecognized arguments: --bogus" in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"


def test_other_verbs_parse_unchanged(kanban_home: Path) -> None:
    """Verbs outside the four #125 verbs keep stdlib (non-intermixed) parsing:
    ``unblock`` still rejects a task id placed after ``--reason``'s value, exactly
    as on the base parser (intermixing it would have accepted T2)."""
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "u2", "u3")
        assert kb.block_task(conn, first, reason="x")
        assert kb.block_task(conn, second, reason="x")
    out = kc.run_slash(f'unblock {first} --reason "note" {second}')
    assert "usage error" in out, out
    assert f"unrecognized arguments: {second}" in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "blocked"


# ---------------------------------------------------------------------------
# Review r1 finding 1: a reason placed after --ids is read as a task id by
# --ids (nargs="+"). The call is refused before any change, never a partial
# change that drops the reason, and --help says so.
# ---------------------------------------------------------------------------


def _bulk_pair(conn, verb: str) -> list[str]:
    if verb == "promote":  # promote moves todo/blocked tasks
        return [kb.create_task(conn, title=f"{verb}-{n}", initial_status="blocked",
                               block_reason="held") for n in ("a", "b")]
    return _make_ready_tasks(conn, f"{verb}-a", f"{verb}-b")


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_reason_after_ids_is_refused_before_any_change(
    kanban_home: Path, verb: str
) -> None:
    with kbc.connect_closing() as conn:
        first, second = _bulk_pair(conn, verb)
        before = {tid: kb.get_task(conn, tid).status for tid in (first, second)}
        events_before = {tid: len(kb.list_events(conn, tid)) for tid in (first, second)}
    out = kc.run_slash(f'{verb} {first} --ids {second} "text after ids"')
    assert "usage error" in out, out
    assert "text after ids" in out and "--reason" in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == before[tid], (tid, task.status)
            assert len(kb.list_events(conn, tid)) == events_before[tid]
            assert not _comments(conn, tid)


def test_schedule_reason_after_ids_never_schedules_without_reason(kanban_home: Path) -> None:
    """The reviewer's exact case: base scheduled both tasks with reason=None."""
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "later-a", "later-b")
    out = kc.run_slash(f'schedule {first} --ids {second} "later please"')
    assert "usage error" in out, out
    assert "cannot schedule later please" not in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "ready"
            assert not _event_payloads(conn, tid, "scheduled")


def test_reason_after_ids_refused_on_direct_namespace(kanban_home: Path, capsys) -> None:
    """Callers that build the Namespace directly hit the same refusal."""
    import argparse

    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "ns-a", "ns-b")
    ns = argparse.Namespace(kanban_action="block", task_id=first,
                            ids=[second, "text after ids"], reason=[],
                            _reason_opt=None, kind=None)
    assert kc._cmd_block(ns) == 2
    assert "text after ids" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            assert kb.get_task(conn, tid).status == "ready"


def test_reason_before_ids_and_reason_flag_with_ids_still_work(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        a, b, c, d = _make_ready_tasks(conn, "pre-a", "pre-b", "flag-a", "flag-b")
    out = kc.run_slash(f'block {a} "before ids" --ids {b}')
    assert "usage error" not in out, out
    out = kc.run_slash(f'block {c} --ids {d} --reason "flag with ids"')
    assert "usage error" not in out, out
    with kbc.connect_closing() as conn:
        for tid, why in ((a, "before ids"), (b, "before ids"),
                         (c, "flag with ids"), (d, "flag with ids")):
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "blocked"
            assert task.block_reason == why


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_help_says_reason_after_ids_is_refused(kanban_home: Path, verb: str) -> None:
    out = kc.run_slash(f"{verb} -h")
    flat = " ".join(out.split())
    assert "including after --ids" not in flat, out
    assert "not after --ids" in flat, out
    assert "refused before any change" in flat, out


# ---------------------------------------------------------------------------
# Review r1 finding 2: the usage line names the flag's value REASON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_usage_line_shows_reason_metavar(kanban_home: Path, verb: str) -> None:
    out = kc.run_slash(f"{verb} -h")
    assert "_REASON_OPT" not in out, out
    assert "--reason REASON" in out, out


# ---------------------------------------------------------------------------
# Review r1 finding 3: an explicitly given empty --reason beside a positional
# reason is the both-forms usage error, never silently ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_empty_reason_flag_plus_positional_is_usage_error(
    kanban_home: Path, verb: str
) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="empty-flag")  # no parents -> ready
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f'{verb} {tid} "positional text" --reason ""')
    assert "usage error" in out, out
    assert "positional text" in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert len(kb.list_events(conn, tid)) == events_before
        assert not _comments(conn, tid)
