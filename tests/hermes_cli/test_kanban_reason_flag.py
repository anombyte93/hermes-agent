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

import functools
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


def _refusal(verb: str) -> str:
    """The one fixed parse-failure message for a protected verb (review r4)."""
    return f"kanban {verb}: invalid arguments; see --help"


# The handler's refusal when a positional reason and --reason are both given.
# It is checked before any change; parsing itself succeeds (review r4 moved the
# check out of the parser so that parse failures have one message).
_CONFLICT = "pass the reason either as trailing words or as --reason, not both"


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
    assert _CONFLICT in out, out
    # The error names BOTH competing forms by label, never by value (a reason
    # can carry a secret; review r1 of the Codex fix, finding 1).
    assert "trailing words" in out and "--reason" in out, out
    assert "positional text" not in out and "flag text" not in out, out
    # Nothing was mutated: the handler refuses before any change.
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
    assert _CONFLICT in out, out


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
    """Intermixed parsing must not swallow a genuinely unknown option: it is
    refused with the one fixed message, never by its text, and nothing is mutated."""
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "bogus")[0]
    out = kc.run_slash(f'block {tid} "a reason" --bogus')
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal("block"), out
    assert "--bogus" not in out, out
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
    assert "--ids" in out and "--reason" in out, out
    assert "text after ids" not in out, out  # never echoed (finding 1)
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
    err = capsys.readouterr().err
    assert "--ids" in err and "--reason" in err, err
    assert "text after ids" not in err, err  # never echoed (finding 1)
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
    assert _CONFLICT in out, out
    assert "trailing words" in out and "--reason" in out, out
    assert "positional text" not in out, out  # never echoed (finding 1)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert len(kb.list_events(conn, tid)) == events_before
        assert not _comments(conn, tid)


# ---------------------------------------------------------------------------
# Review r1 of the Codex fix (hermes-agent-133-r1), finding 1: usage errors
# never echo reason values. A reason can carry a secret, and the /kanban reply
# goes back to a chat surface. The control secret is a shape the existing
# redactor strips, proving the test would catch a real token.
# ---------------------------------------------------------------------------

# Synthetic, assembled at runtime so no token-shaped literal sits in the file.
_SYNTHETIC_SECRET = "ghp_" + "Synth3ticR3viewT0ken" + "0123456789abcdef"


def test_synthetic_secret_is_a_shape_the_redactor_strips() -> None:
    from agent.redact import redact_sensitive_text

    assert _SYNTHETIC_SECRET not in redact_sensitive_text(_SYNTHETIC_SECRET, force=True)


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_conflict_error_never_echoes_reason_values(kanban_home: Path, verb: str) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="leak")  # no parents -> ready
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f'{verb} {tid} "rotate {_SYNTHETIC_SECRET}" --reason cleanflagvalue')
    assert _CONFLICT in out, out
    # The conflict is named by its labels ...
    assert "trailing words" in out and "--reason" in out, out
    # ... never by the values themselves.
    assert _SYNTHETIC_SECRET not in out, out
    assert "rotate" not in out and "cleanflagvalue" not in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert len(kb.list_events(conn, tid)) == events_before


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_ids_refusal_never_echoes_reason_values(kanban_home: Path, verb: str) -> None:
    with kbc.connect_closing() as conn:
        first, second = _bulk_pair(conn, verb)
    out = kc.run_slash(f'{verb} {first} --ids {second} "rotate {_SYNTHETIC_SECRET}"')
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    assert _SYNTHETIC_SECRET not in out and "rotate" not in out, out


def test_direct_namespace_conflict_never_echoes_reason_values(kanban_home: Path, capsys) -> None:
    import argparse

    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "dn-a", "dn-b")
    ns = argparse.Namespace(kanban_action="schedule", task_id=first, ids=None,
                            reason=["rotate", _SYNTHETIC_SECRET], _reason_opt="cleanflagvalue")
    assert kc._cmd_schedule(ns) == 2
    ns = argparse.Namespace(kanban_action="block", task_id=first,
                            ids=[second, f"rotate {_SYNTHETIC_SECRET}"], reason=[],
                            _reason_opt=None, kind=None)
    assert kc._cmd_block(ns) == 2
    err = capsys.readouterr().err
    assert "--reason" in err and "--ids" in err, err
    assert _SYNTHETIC_SECRET not in err and "rotate" not in err and "cleanflagvalue" not in err, err
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Review r1 of the Codex fix, finding 2: a leading ``--`` keeps every later
# token positional. Intermixed parsing on Python 3.11 dropped the separator,
# so ``schedule -- T --ids U --reason X`` scheduled U too.
# ---------------------------------------------------------------------------


def _leaf_namespace(argv: list[str]):
    import argparse

    from hermes_cli.kanban_parser import build_parser

    wrap = argparse.ArgumentParser(prog="wrap", add_help=False)
    parser = build_parser(wrap.add_subparsers(dest="_top"))
    return parser.parse_args(argv)


def test_leading_double_dash_keeps_later_tokens_positional_in_parser() -> None:
    ns = _leaf_namespace(["schedule", "--", "t_a", "--ids", "t_b", "--reason", "maintenance"])
    assert ns.task_id == "t_a"
    assert ns.ids is None
    assert ns._reason_opt is None
    assert ns.reason == ["--ids", "t_b", "--reason", "maintenance"]


def test_double_dash_after_options_keeps_intermixed_reason_in_parser() -> None:
    ns = _leaf_namespace(["block", "t_a", "--kind", "dependency", "--", "--not-an-option", "--"])
    assert ns.task_id == "t_a" and ns.kind == "dependency"
    assert ns.reason == ["--not-an-option", "--"]


@pytest.mark.parametrize("verb", ["block", "schedule"])
def test_leading_double_dash_targets_only_the_first_task(kanban_home: Path, verb: str) -> None:
    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, f"dd-{verb}-a", f"dd-{verb}-b")
        events_before = len(kb.list_events(conn, second))
    out = kc.run_slash(f"{verb} -- {first} --ids {second} --reason maintenance")
    assert "usage error" not in out, out
    expected = f"--ids {second} --reason maintenance"
    status = "blocked" if verb == "block" else "scheduled"
    label = "BLOCKED" if verb == "block" else "SCHEDULED"
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, first)
        assert task is not None and task.status == status
        assert f"{label}: {expected}" in _comments(conn, first)
        assert _event_payloads(conn, first, status)[-1].get("reason") == expected
        other = kb.get_task(conn, second)
        assert other is not None and other.status == "ready"
        assert len(kb.list_events(conn, second)) == events_before
        assert not _comments(conn, second)


# ---------------------------------------------------------------------------
# Review r1 of the Codex fix, finding 3: a repeated --ids cannot hide a reason
# placed after the first one (argparse keeps only the last occurrence).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_repeated_ids_cannot_bypass_the_refusal(kanban_home: Path, verb: str) -> None:
    with kbc.connect_closing() as conn:
        first, second = _bulk_pair(conn, verb)
        third = (kb.create_task(conn, title=f"{verb}-c", initial_status="blocked",
                                block_reason="held") if verb == "promote"
                 else _make_ready_tasks(conn, f"{verb}-c")[0])
        tids = (first, second, third)
        before = {tid: kb.get_task(conn, tid).status for tid in tids}
        events_before = {tid: len(kb.list_events(conn, tid)) for tid in tids}
    out = kc.run_slash(f'{verb} {first} --ids {second} "later please" --ids {third}')
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    assert "later please" not in out, out
    with kbc.connect_closing() as conn:
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == before[tid], (tid, task.status)
            assert len(kb.list_events(conn, tid)) == events_before[tid]
            assert not _comments(conn, tid)


# ---------------------------------------------------------------------------
# Review r2 (hermes-agent-133-r2), finding 1: an unknown option on an
# intermixed verb is refused without its text (since r4: the one fixed
# refusal). On Python 3.11 ``parse_known_intermixed_args`` leaves the reason
# after a typo'd flag (``--reasn "rotate <secret>"``) in the extras, and the
# stdlib echoed every extra into the /kanban reply.
# ---------------------------------------------------------------------------

# The reviewer's literal control value, assembled at runtime like _SYNTHETIC_SECRET.
_R2_CONTROL_SECRET = "SYNTHETIC_" + "SECRET_" + "123"


def _assert_untouched(conn, tid: str, status: str, events_before: int) -> None:
    task = kb.get_task(conn, tid)
    assert task is not None and task.status == status, (tid, task and task.status)
    assert len(kb.list_events(conn, tid)) == events_before
    assert not _comments(conn, tid)


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
@pytest.mark.parametrize("secret", [_R2_CONTROL_SECRET, _SYNTHETIC_SECRET])
def test_unknown_option_typo_never_echoes_the_reason(
    kanban_home: Path, verb: str, secret: str
) -> None:
    """The reviewer's control, on every intermixed verb: a typo before the
    reason is refused without echoing ``--reasn`` or anything it was given."""
    with kbc.connect_closing() as conn:
        tid = _bulk_pair(conn, verb)[0]
        status = kb.get_task(conn, tid).status
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f"{verb} {tid} --reasn 'rotate {secret}'")
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    assert "--reasn" not in out and secret not in out and "rotate" not in out, out
    with kbc.connect_closing() as conn:
        _assert_untouched(conn, tid, status, events_before)


def test_unknown_option_with_attached_value_shows_nothing_of_it(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _make_ready_tasks(conn, "attached")[0]
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f"block {tid} 'held up' --token={_R2_CONTROL_SECRET} -x{_R2_CONTROL_SECRET}")
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal("block"), out
    assert "--token" not in out and "-x" not in out and _R2_CONTROL_SECRET not in out, out
    with kbc.connect_closing() as conn:
        _assert_untouched(conn, tid, "ready", events_before)


def _py311_intermixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any interpreter return what Python 3.11 returns for an unknown
    option followed by reason words: the words land in the extras, not in
    ``reason``. Keeps the control meaningful on interpreters (3.12+) whose
    intermixed parser already routes them to ``reason``."""
    import argparse

    original = argparse.ArgumentParser.parse_known_intermixed_args

    def py311_like(self, args=None, namespace=None):
        ns, extras = original(self, args, namespace)
        if extras and getattr(ns, "reason", None):
            extras, ns.reason = list(extras) + list(ns.reason), []
        return ns, extras

    monkeypatch.setattr(argparse.ArgumentParser, "parse_known_intermixed_args", py311_like)


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_reason_left_in_extras_is_never_echoed(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    _py311_intermixed(monkeypatch)
    with kbc.connect_closing() as conn:
        tid = _bulk_pair(conn, verb)[0]
        status = kb.get_task(conn, tid).status
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f"{verb} {tid} --reasn 'rotate {_R2_CONTROL_SECRET}' tail")
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    for word in ("--reasn", _R2_CONTROL_SECRET, "rotate", "tail"):
        assert word not in out, (word, out)
    with kbc.connect_closing() as conn:
        _assert_untouched(conn, tid, status, events_before)


# ---------------------------------------------------------------------------
# Review r2, finding 2: every task-id check is a full match. ``re.match`` with
# ``$`` accepted a final newline, so a quoted ``"t_b\n"`` after ``--ids``
# passed the per-word check; with a repeated ``--ids`` argparse then kept only
# the last occurrence and the first and third tasks were scheduled.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_newline_id_after_repeated_ids_is_refused_before_any_change(
    kanban_home: Path, verb: str
) -> None:
    with kbc.connect_closing() as conn:
        first, second = _bulk_pair(conn, verb)
        third = (kb.create_task(conn, title=f"{verb}-c", initial_status="blocked",
                                block_reason="held") if verb == "promote"
                 else _make_ready_tasks(conn, f"{verb}-c")[0])
        tids = (first, second, third)
        before = {tid: kb.get_task(conn, tid).status for tid in tids}
        events_before = {tid: len(kb.list_events(conn, tid)) for tid in tids}
    out = kc.run_slash(f"{verb} {first} --ids '{second}\n' --ids {third} --reason maintenance")
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    assert "maintenance" not in out, out
    with kbc.connect_closing() as conn:
        for tid in tids:
            _assert_untouched(conn, tid, before[tid], events_before[tid])


@pytest.mark.parametrize("verb", ["block", "schedule", "promote"])
def test_newline_id_as_trailing_word_is_not_an_extra_id(kanban_home: Path, verb: str) -> None:
    with kbc.connect_closing() as conn:
        first, second = _bulk_pair(conn, verb)
        before = {tid: kb.get_task(conn, tid).status for tid in (first, second)}
        events_before = {tid: len(kb.list_events(conn, tid)) for tid in (first, second)}
    out = kc.run_slash(f"{verb} {first} '{second}\n' --reason maintenance")
    assert _CONFLICT in out, out
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            _assert_untouched(conn, tid, before[tid], events_before[tid])


def test_task_id_checks_are_full_matches() -> None:
    import argparse

    from hermes_cli.kanban_parser import ids_not_task_ids, reason_extra_ids, task_id_word

    for word in ("t_b\n", "t_b\nrotate", "t_b "):
        with pytest.raises(argparse.ArgumentTypeError):
            task_id_word(word)
        assert ids_not_task_ids("schedule", [word]) is not None
        assert reason_extra_ids("schedule", [word], "maintenance") == []
    assert task_id_word("t_b") == "t_b"
    assert ids_not_task_ids("schedule", ["t_b"]) is None
    assert reason_extra_ids("schedule", ["t_b"], "maintenance") == ["t_b"]


def test_direct_namespace_newline_id_is_refused(kanban_home: Path, capsys) -> None:
    import argparse

    with kbc.connect_closing() as conn:
        first, second = _make_ready_tasks(conn, "nl-a", "nl-b")
        events_before = {tid: len(kb.list_events(conn, tid)) for tid in (first, second)}
    ns = argparse.Namespace(kanban_action="schedule", task_id=first, ids=[f"{second}\n"],
                            reason=[], _reason_opt="maintenance")
    assert kc._cmd_schedule(ns) == 2
    with kbc.connect_closing() as conn:
        for tid in (first, second):
            _assert_untouched(conn, tid, "ready", events_before[tid])


# ---------------------------------------------------------------------------
# Review r3 (hermes-agent-133-r3), finding 1: an unknown-argument refusal on an
# intermixed verb echoes NO word the caller supplied, not even an option-looking
# label: ``--reasn '--SYNTHETIC_SECRET_123'`` is a long-option-shaped reason
# value, and r2's label filter displayed it whole. Since r4 the refusal is the
# one fixed message, naming the verb only.
# ---------------------------------------------------------------------------

# Every part of the reviewer's control value; none may reach the reply.
_R3_SECRET_PARTS = ("SYNTHETIC", "SECRET", "_123", "Synth3tic", "R3view", "T0ken")

_R3_CONTROLS = {
    "option-shaped value": "--reasn '--{secret}'",
    "attached long value": "--reasn={secret}",
    "attached short value": "-x{secret}",
    "plain typo": "--reasn {secret}",
}


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
@pytest.mark.parametrize("control", sorted(_R3_CONTROLS))
@pytest.mark.parametrize("secret", [_R2_CONTROL_SECRET, _SYNTHETIC_SECRET])
def test_unrecognized_args_echo_no_caller_word(
    kanban_home: Path, verb: str, control: str, secret: str
) -> None:
    with kbc.connect_closing() as conn:
        tid = _bulk_pair(conn, verb)[0]
        status = kb.get_task(conn, tid).status
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f"{verb} {tid} " + _R3_CONTROLS[control].format(secret=secret))
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    for part in _R3_SECRET_PARTS:
        assert part not in out, (part, out)
    # Not even the option-looking label the caller typed.
    assert "--reasn" not in out and "-x" not in out, out
    with kbc.connect_closing() as conn:
        _assert_untouched(conn, tid, status, events_before)


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
def test_unrecognized_args_echo_no_caller_word_with_py311_extras(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """Same refusal when the reason lands in the extras, as on Python 3.11."""
    _py311_intermixed(monkeypatch)
    with kbc.connect_closing() as conn:
        tid = _bulk_pair(conn, verb)[0]
        status = kb.get_task(conn, tid).status
        events_before = len(kb.list_events(conn, tid))
    out = kc.run_slash(f"{verb} {tid} --reasn '--{_R2_CONTROL_SECRET}' tail")
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _refusal(verb), out
    for part in _R3_SECRET_PARTS + ("--reasn", "tail"):
        assert part not in out, (part, out)
    with kbc.connect_closing() as conn:
        _assert_untouched(conn, tid, status, events_before)


def test_refusal_helper_is_fixed_per_verb() -> None:
    from hermes_cli.kanban_parser import invalid_arguments_refusal

    for verb in ("block", "schedule", "promote", "request-changes"):
        assert invalid_arguments_refusal(verb) == _refusal(verb)


# ---------------------------------------------------------------------------
# Review r4 (hermes-agent-133-r4), round 5: one choke point. Whenever argv
# names one of the four protected verbs, ANY argparse failure at ANY level of
# the kanban parser tree (an ArgumentError such as a surplus ``--help=VALUE``,
# leftovers at the leaf, leftovers at the parent ``kanban`` level before the
# verb) is replaced by ONE fixed, token-free message with exit code 2, on both
# surfaces: the ``hermes kanban`` CLI streams and the ``/kanban`` reply.
# ---------------------------------------------------------------------------

_R4_CONTROLS = {
    # r4 finding 1: argparse raises ArgumentError for the surplus value.
    "r4 surplus help value": "request-changes t_a --help={secret}",
    # r4 finding 2: a leftover at the parent (kanban) level, before the verb.
    "r4 parent-level leftover": "--{secret} request-changes t_a --reason clean",
    # r3: an option-shaped value after a typo'd flag.
    "r3 option-shaped value": "request-changes t_a --reasn '--{secret}'",
}

_REFUSAL = _refusal("request-changes")


def _cli(argv: list[str]):
    """Parse ``hermes <argv>`` through the real CLI parser and entry helper.

    Returns the exit code; the parser always exits here. Read the streams
    with ``capsys``.
    """
    from hermes_cli import main as hermes_main

    parser, subparsers = _cli_parser()
    with pytest.raises(SystemExit) as exc:
        hermes_main._parse_cli_args(parser, subparsers, argv)
    return exc.value.code


@functools.lru_cache(maxsize=1)
def _cli_parser():
    """The full ``hermes`` parser, built once: building it is the slow part."""
    from hermes_cli import main as hermes_main

    return hermes_main._build_cli_parser()


@pytest.mark.parametrize("control", sorted(_R4_CONTROLS))
def test_protected_verb_cli_refusal_is_fixed_and_token_free(
    kanban_home: Path, capsys, control: str
) -> None:
    import shlex

    argv = ["kanban"] + shlex.split(_R4_CONTROLS[control].format(secret=_R2_CONTROL_SECRET))
    code = _cli(argv)
    captured = capsys.readouterr()
    streams = captured.out + captured.err
    assert _R2_CONTROL_SECRET not in streams, streams
    assert code == 2, code
    assert captured.err.strip() == _REFUSAL, captured.err
    assert captured.out == "", captured.out


@pytest.mark.parametrize("control", sorted(_R4_CONTROLS))
def test_protected_verb_slash_refusal_is_fixed_and_token_free(
    kanban_home: Path, control: str
) -> None:
    out = kc.run_slash(_R4_CONTROLS[control].format(secret=_R2_CONTROL_SECRET))
    assert _R2_CONTROL_SECRET not in out, out
    assert "usage error" in out, out
    assert out.splitlines()[-1] == _REFUSAL, out


@pytest.mark.parametrize("verb", ["block", "schedule", "promote", "request-changes"])
@pytest.mark.parametrize("tail", [
    "--help={secret}",              # ArgumentError: surplus value
    "--reason",                     # ArgumentError: missing value
    "--reasn '--{secret}'",         # leaf leftovers
    "--ids '{secret}'",             # ArgumentTypeError from the --ids type
])
def test_every_parse_failure_on_a_protected_verb_is_the_one_refusal(
    kanban_home: Path, capsys, verb: str, tail: str
) -> None:
    import shlex

    rest = f"{verb} t_a " + tail.format(secret=_R2_CONTROL_SECRET)
    refusal = f"kanban {verb}: invalid arguments; see --help"
    out = kc.run_slash(rest)
    assert _R2_CONTROL_SECRET not in out and out.splitlines()[-1] == refusal, out
    assert _cli(["kanban"] + shlex.split(rest)) == 2
    captured = capsys.readouterr()
    assert _R2_CONTROL_SECRET not in captured.out + captured.err
    assert captured.err.strip() == refusal, captured.err


def test_protected_verb_anywhere_in_argv_refuses_with_the_resolved_verb(
    kanban_home: Path, capsys
) -> None:
    """A protected verb ANYWHERE in argv arms the refusal; it names the verb
    the call resolved to (a fixed subcommand name, never caller text)."""
    rest = f"unblock t_a --reason request-changes --{_R2_CONTROL_SECRET}"
    out = kc.run_slash(rest)
    assert _R2_CONTROL_SECRET not in out, out
    assert out.splitlines()[-1] == "kanban unblock: invalid arguments; see --help", out
    assert _cli(["kanban"] + rest.split()) == 2
    captured = capsys.readouterr()
    assert _R2_CONTROL_SECRET not in captured.out + captured.err
    assert captured.err.strip() == "kanban unblock: invalid arguments; see --help"


def test_protected_verb_help_still_prints(kanban_home: Path, capsys) -> None:
    """``--help`` is not a failure: it still prints the verb's help, exit 0."""
    out = kc.run_slash("request-changes --help")
    assert "--reason REASON" in out and "invalid arguments" not in out, out
    assert _cli(["kanban", "request-changes", "--help"]) == 0
    captured = capsys.readouterr()
    assert "--reason REASON" in captured.out and "invalid arguments" not in captured.out
