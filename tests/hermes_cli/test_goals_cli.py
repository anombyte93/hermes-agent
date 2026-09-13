"""``hermes goals`` CLI subcommand tests (rejudge route).

Exercises the real argparse surface end to end inside one process:
parser registration, dispatch, rendering, and exit codes. The judge is
stubbed only at the external network boundary
(``agent.auxiliary_client.call_llm``); sessions, state.db, and the
GoalManager persistence are all real.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _rand_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed(sid: str, *, status: str = "paused", evidence: str = "Done and verified."):
    from hermes_state import SessionDB

    from hermes_cli.goals import GoalState, save_goal

    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, role="user", content="do the work")
        db.append_message(sid, role="assistant", content=evidence)
    finally:
        db.close()
    save_goal(
        sid,
        GoalState(
            goal="Ship the release",
            status=status,
            turns_used=6,
            max_turns=20,
            paused_reason="judge API unreachable 5 turns in a row",
        ),
    )


def _build_parser():
    from hermes_cli.subcommands.goals import build_goals_parser

    import argparse

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_goals_parser(subparsers, cmd_goals=None)
    return parser


def _run_cli(monkeypatch, capsys, argv):
    """Drive the CLI handler in-process; returns (exit_code, output)."""
    from hermes_cli.subcommands import goals as goals_cmd

    parser = _build_parser()
    args = parser.parse_args(["goals"] + argv)
    code = goals_cmd.cmd_goals(args)
    out = capsys.readouterr().out
    return code, out


DONE_REPLY = '{"verdict": "done", "reason": "verified with evidence"}'
CONTINUE_REPLY = '{"verdict": "continue", "reason": "still deploying"}'


def _judge_patch(reply: str):
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=reply))]
        ),
    )


# ── registration surface ─────────────────────────────────────────────


def test_goals_command_registered_in_builtin_set():
    """``hermes goals`` must be a real builtin: no plugin discovery, no
    silent shadowing by a plugin-registered command."""
    from hermes_cli.main import _BUILTIN_SUBCOMMANDS

    assert "goals" in _BUILTIN_SUBCOMMANDS


def test_parser_routes_rejudge_subcommand():
    parser = _build_parser()
    args = parser.parse_args(["goals", "rejudge", "sid-xyz", "--dry-run"])
    assert args.goals_command == "rejudge"
    assert args.session_id == "sid-xyz"
    assert args.dry_run is True


def test_parser_requires_session_id():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["goals", "rejudge"])


# ── rendering + exit codes ───────────────────────────────────────────


def test_rejudge_done_shows_goal_and_evidence_then_transition(
    hermes_home, monkeypatch, capsys
):
    sid = _rand_sid("clidone")
    _seed(sid)

    with _judge_patch(DONE_REPLY):
        code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert code == 0
    # The operator sees the existing goal...
    assert "Ship the release" in out
    # ...the evidence that was judged...
    assert "Done and verified." in out
    # ...and the done transition.
    assert "done" in out.lower()


def test_rejudge_incomplete_is_explicit_nonzero(hermes_home, monkeypatch, capsys):
    sid = _rand_sid("cliinc")
    _seed(sid, evidence="Halfway through the rollout.")

    with _judge_patch(CONTINUE_REPLY):
        code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert code == 1
    assert "incomplete" in out.lower()
    assert "still deploying" in out


def test_rejudge_no_goal_for_unknown_session(hermes_home, monkeypatch, capsys):
    sid = _rand_sid("missing")
    code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])
    assert code == 2
    assert sid in out


def test_rejudge_unreachable_judge(hermes_home, monkeypatch, capsys):
    sid = _rand_sid("cliunreach")
    _seed(sid)

    with patch(
        "agent.auxiliary_client.call_llm",
        side_effect=TimeoutError("connection timed out"),
    ):
        code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert code == 3
    assert "unreachable" in out.lower()


def test_rejudge_dry_run_reports_would_transition_without_doing_it(
    hermes_home, monkeypatch, capsys
):
    from hermes_cli.goals import load_goal

    sid = _rand_sid("cliddry")
    _seed(sid)

    with _judge_patch(DONE_REPLY):
        code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid, "--dry-run"])

    assert code == 0
    assert "dry-run" in out.lower()
    assert load_goal(sid).status == "paused"


def test_rejudge_idempotent_retry_after_done(hermes_home, monkeypatch, capsys):
    """Second invocation after a done transition exits 0 and judges nothing."""
    sid = _rand_sid("cliagain")
    _seed(sid)

    with _judge_patch(DONE_REPLY):
        first_code, _ = _run_cli(monkeypatch, capsys, ["rejudge", sid])
    with _judge_patch(DONE_REPLY) as judge:
        second_code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert first_code == 0
    assert second_code == 0
    assert "already" in out.lower()
    judge.assert_not_called()


# ── gates × dry-run + persisted transitions: CLI controls ────────────


def _raw_goal_row(sid: str):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return db.get_meta(f"goal:{sid}")
    finally:
        db.close()


def test_cli_dry_run_with_gate_executes_nothing_and_writes_nothing(
    hermes_home, tmp_path, monkeypatch, capsys
):
    """Permanent CLI control (parent-mandated): ``--dry-run`` on a gated
    goal must not execute the gate command (marker stays absent), must
    leave the stored row byte-identical across repeated invocations, and
    must report gates_pending — an honest preview, not a certified done."""
    from hermes_cli.goals import GoalManager, load_goal

    sid = _rand_sid("clidrygate")
    marker = tmp_path / "cli-gate-ran"
    _seed(sid, status="active", evidence="I think the deploy finished.")
    GoalManager(session_id=sid).add_gate(
        f"touch {marker} && exit 7", max_retries=3
    )
    before = _raw_goal_row(sid)

    for _ in range(4):
        with _judge_patch(DONE_REPLY) as judge:
            code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid, "--dry-run"])
        assert code == 0
        assert "gates_pending" in out
        assert "not evaluated" in out.lower() or "not run" in out.lower()
        judge.assert_called_once()  # the judge ran on evidence; gates did not
        assert _raw_goal_row(sid) == before

    assert not marker.exists(), "CLI dry-run must not execute gate commands"
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "active", "CLI dry-runs must never pause the goal"
    assert reloaded.gates[0].attempts == 0


def test_cli_gate_exhaustion_pause_is_visible_and_persisted(
    hermes_home, monkeypatch, capsys
):
    """CLI control: exhausting gate retries on a real run pauses the goal
    (persisted in state.db) and the operator SEES the pause."""
    from hermes_cli.goals import GoalManager, load_goal

    sid = _rand_sid("clipause")
    _seed(sid, status="active", evidence="Believe it's done; suite still red.")
    GoalManager(session_id=sid).add_gate("exit 7", max_retries=1)

    with _judge_patch(DONE_REPLY) as judge, patch(
        "hermes_cli.goals.workspace_fingerprint", return_value=""
    ):
        code, _ = _run_cli(monkeypatch, capsys, ["rejudge", sid])
        code2, out2 = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert code == 1 and code2 == 1
    judge.assert_not_called()
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "paused"
    assert reloaded.paused_reason
    # The operator is told the goal was paused — not a silent no-op.
    assert "paused" in out2.lower()


def test_cli_stale_evidence_refusal_is_visible(
    hermes_home, monkeypatch, capsys
):
    """CLI control: evidence predating the goal is refused visibly with
    exit 1 and the judge is never called."""
    from hermes_state import SessionDB

    from hermes_cli.goals import GoalState, save_goal

    sid = _rand_sid("clistale")
    old_ts = 1_700_000_000.0
    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
        db.append_message(
            sid, role="assistant", content="Old goal's final report.",
            timestamp=old_ts,
        )
    finally:
        db.close()
    save_goal(
        sid,
        GoalState(
            goal="Brand new goal",
            status="paused",
            turns_used=6,
            max_turns=20,
            created_at=old_ts + 3_600.0,
        ),
    )

    with _judge_patch(DONE_REPLY) as judge:
        code, out = _run_cli(monkeypatch, capsys, ["rejudge", sid])

    assert code == 1
    assert "stale" in out.lower()
    judge.assert_not_called()
