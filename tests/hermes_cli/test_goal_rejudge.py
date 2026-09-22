"""Scoped goal rejudge: rejudge_goal unit tests.

Covers the outcome contract for ``hermes goals rejudge``: wrong session,
no goal, cleared goal, no evidence, true incomplete, paused-auth recovery
into a genuine done transition, gate enforcement, transport failure,
idempotent retry, and dry-run. The judge is stubbed only at the external
network boundary (``agent.auxiliary_client.call_llm``), exactly like the
existing goal-judge tests.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so state.db writes never touch the real one."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _rand_sid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed_transcript(
    sid: str, assistant_text: str, *, evidence_timestamp: float | None = None
) -> None:
    """Create a real session row + messages through the real SessionDB."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, role="user", content="please do the thing")
        if evidence_timestamp is not None:
            db.append_message(
                sid, role="assistant", content=assistant_text,
                timestamp=evidence_timestamp,
            )
        else:
            db.append_message(sid, role="assistant", content=assistant_text)
    finally:
        db.close()


def _seed_goal(sid: str, *, status: str = "paused", goal: str = "Deploy and verify",
               paused_reason: str | None = None,
               created_at: float = 0.0) -> None:
    from hermes_cli.goals import GoalState, save_goal

    state = GoalState(
        goal=goal,
        status=status,
        turns_used=5,
        max_turns=20,
        paused_reason=paused_reason,
        created_at=created_at,
    )
    save_goal(sid, state)


def _raw_goal_row(sid: str) -> str | None:
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return db.get_meta(f"goal:{sid}")
    finally:
        db.close()


def _judge_patch(reply: str):
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content=reply))]
        ),
    )


DONE_REPLY = '{"verdict": "done", "reason": "evidence is complete"}'
CONTINUE_REPLY = '{"verdict": "continue", "reason": "replicas still draining"}'


# ── scoping refusals ─────────────────────────────────────────────────


def test_unknown_session_is_refused(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("ghost")
    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid)
    assert result["outcome"] == "no_session"
    assert sid in result["reason"]
    assert result["changed"] is False
    judge.assert_not_called()


def test_wrong_session_with_goal_elsewhere_is_no_goal(hermes_home):
    """A goal on session A must not be rejudged by naming session B."""
    from hermes_cli.goals import rejudge_goal

    sid_a = _rand_sid("owner")
    sid_b = _rand_sid("bystander")
    _seed_transcript(sid_a, "Deployed. Health check green.")
    _seed_transcript(sid_b, "Unrelated work in another session.")
    _seed_goal(sid_a)

    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid_b)
    assert result["outcome"] == "no_goal"
    assert sid_b in result["reason"]
    assert result["changed"] is False
    judge.assert_not_called()


def test_cleared_goal_is_refused(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("cleared")
    _seed_transcript(sid, "Some earlier response.")
    _seed_goal(sid, status="cleared")

    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid)
    assert result["outcome"] == "cleared"
    assert result["changed"] is False
    judge.assert_not_called()


def test_session_without_transcript_has_no_evidence(hermes_home):
    from hermes_state import SessionDB

    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("silent")
    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
    finally:
        db.close()
    _seed_goal(sid)

    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid)
    assert result["outcome"] == "no_evidence"
    assert result["changed"] is False
    judge.assert_not_called()


# ── judge outcomes ───────────────────────────────────────────────────


def test_true_incomplete_leaves_state_untouched(hermes_home):
    from hermes_cli.goals import load_goal, rejudge_goal

    sid = _rand_sid("inc")
    _seed_transcript(sid, "Half done: 1 of 2 replicas upgraded.")
    _seed_goal(sid, paused_reason="judge API unreachable 5 turns in a row")
    before = _raw_goal_row(sid)

    with _judge_patch(CONTINUE_REPLY):
        result = rejudge_goal(sid)

    assert result["outcome"] == "incomplete"
    assert result["changed"] is False
    assert result["verdict"] == "continue"
    assert "replicas still draining" in result["reason"]
    # No state write happened at all.
    assert _raw_goal_row(sid) == before
    assert load_goal(sid).status == "paused"


def test_paused_auth_recovery_transitions_to_done(hermes_home):
    """The user story: auth was paused, judge now reachable, verdict done."""
    from hermes_cli.goals import load_goal, rejudge_goal

    sid = _rand_sid("authrec")
    _seed_transcript(
        sid,
        "Deployed. Health check green at /healthz, version 1.2.3 on all "
        "3 replicas, rollout complete.",
    )
    _seed_goal(
        sid,
        paused_reason=(
            "judge API unreachable 5 turns in a row (check "
            "auxiliary.goal_judge provider/key in config.yaml)"
        ),
    )

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid)

    assert result["outcome"] == "done"
    assert result["changed"] is True
    reloaded = load_goal(sid)
    assert reloaded.status == "done"
    assert reloaded.last_verdict == "done"
    assert reloaded.last_reason == "evidence is complete"


def test_wait_verdict_is_reported_incomplete(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("waiter")
    _seed_transcript(sid, "Waiting on the CI pipeline.")
    _seed_goal(sid)

    with _judge_patch(
        '{"verdict": "wait", "wait_on_pid": 4242, "reason": "CI running"}'
    ):
        result = rejudge_goal(sid)

    assert result["outcome"] == "incomplete"
    assert result["verdict"] == "wait"
    assert result["changed"] is False


def test_transport_failure_is_unreachable_and_writes_nothing(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("dark")
    _seed_transcript(sid, "Deployed everything, all green.")
    _seed_goal(sid, paused_reason="judge API unreachable 5 turns in a row")
    before = _raw_goal_row(sid)

    with patch(
        "agent.auxiliary_client.call_llm",
        side_effect=TimeoutError("connection timed out"),
    ):
        result = rejudge_goal(sid)

    assert result["outcome"] == "unreachable"
    assert result["changed"] is False
    assert _raw_goal_row(sid) == before


def test_unparseable_judge_reply_is_explicit(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("garble")
    _seed_transcript(sid, "Work finished with receipts.")
    _seed_goal(sid)

    with _judge_patch("the goal seems fine I guess"):
        result = rejudge_goal(sid)

    assert result["outcome"] == "unparseable"
    assert result["changed"] is False


# ── idempotence + dry-run ────────────────────────────────────────────


def test_already_done_is_idempotent(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("finished")
    _seed_transcript(sid, "Done and verified.")
    _seed_goal(sid, status="done")
    before = _raw_goal_row(sid)

    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid)

    assert result["outcome"] == "already_done"
    assert result["changed"] is False
    judge.assert_not_called()
    assert _raw_goal_row(sid) == before


def test_dry_run_judges_without_transitioning(hermes_home):
    from hermes_cli.goals import load_goal, rejudge_goal

    sid = _rand_sid("dryrun")
    _seed_transcript(sid, "All replicas green, rollout verified.")
    _seed_goal(sid)

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid, dry_run=True)

    assert result["outcome"] == "done"
    assert result["changed"] is False
    assert result["dry_run"] is True
    assert load_goal(sid).status == "paused"


# ── quality gates ────────────────────────────────────────────────────


def test_failing_gate_blocks_done_without_calling_judge(hermes_home):
    from hermes_cli.goals import GoalManager, rejudge_goal

    sid = _rand_sid("gateblock")
    _seed_transcript(sid, "I believe the deploy is complete.")
    _seed_goal(sid, status="active")
    GoalManager(session_id=sid).add_gate("exit 7")

    with _judge_patch(DONE_REPLY) as judge, patch(
        "hermes_cli.goals.workspace_fingerprint", return_value=""
    ):
        result = rejudge_goal(sid)

    assert result["outcome"] == "incomplete"
    assert result["verdict"] == "gate_failed"
    assert "exit 7" in result["reason"]
    judge.assert_not_called()


def test_passing_gate_allows_done(hermes_home):
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("gatepass")
    _seed_transcript(sid, "Deployed and the smoke suite passed.")
    _seed_goal(sid, status="active")
    GoalManager(session_id=sid).add_gate("true")

    with _judge_patch(DONE_REPLY), patch(
        "hermes_cli.goals.workspace_fingerprint", return_value=""
    ):
        result = rejudge_goal(sid)

    assert result["outcome"] == "done"
    assert load_goal(sid).status == "done"


# ── gates × dry-run: gates deferred, nothing executed or written ─────


def test_dry_run_with_failing_gate_defers_execution_and_writes_nothing(
    hermes_home, tmp_path
):
    """``--dry-run`` must not EXECUTE gate commands, persist gate
    bookkeeping, or consume the gate retry budget.

    Regression (parent control against c99ba6a4): a dry-run ran gates
    through ``_check_gates`` — executing arbitrary shell commands and
    persisting attempts/exit codes — while promising "no state written".
    """
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("drygate")
    marker = tmp_path / "gate-ran-drygate"
    _seed_transcript(sid, "I think the deploy finished.")
    _seed_goal(sid, status="active")
    GoalManager(session_id=sid).add_gate(
        f"touch {marker} && exit 7", max_retries=3
    )
    before = _raw_goal_row(sid)

    for i in range(4):  # repeated dry-runs must consume nothing
        with _judge_patch(DONE_REPLY) as judge:
            result = rejudge_goal(sid, dry_run=True)
        assert result["outcome"] == "gates_pending", f"dry-run #{i + 1}"
        assert result["verdict"] == "done"  # the judge DID run, on evidence
        assert result["changed"] is False
        assert result["gates_deferred"] is True
        assert result["dry_run"] is True
        judge.assert_called_once()
        assert _raw_goal_row(sid) == before, f"dry-run #{i + 1} wrote state"

    assert not marker.exists(), "dry-run must not execute gate commands"
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "active", "dry-runs must never pause the goal"
    assert reloaded.gates[0].attempts == 0
    assert reloaded.gates[0].last_exit_code is None


def test_dry_run_with_passing_gate_still_defers_it(hermes_home):
    """Even a gate that would pass is not executed under --dry-run; the
    verdict stays an honest preview, not a certification."""
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("drypass")
    _seed_transcript(sid, "Deployed; smoke suite green.")
    _seed_goal(sid, status="active")
    GoalManager(session_id=sid).add_gate("true")
    before = _raw_goal_row(sid)

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid, dry_run=True)

    assert result["outcome"] == "gates_pending"
    assert result["changed"] is False
    assert _raw_goal_row(sid) == before
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.gates[0].last_exit_code is None, "gate must not have run"


def test_dry_run_done_without_gates_is_a_full_preview(hermes_home):
    """With no gates, a dry-run done verdict is the complete story."""
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("drynogate")
    _seed_transcript(sid, "All replicas green, rollout verified.")
    _seed_goal(sid)
    GoalManager(session_id=sid)  # no gate added

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid, dry_run=True)

    assert result["outcome"] == "done"
    assert result["dry_run"] is True
    assert result["changed"] is False
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "paused"


def test_gate_exhaustion_pause_persists_and_reports_changed(hermes_home):
    """A real (non-dry) rejudge that exhausts gate retries persists the
    pause and must report it: ``changed=True`` (a state transition WAS
    written) with ``status_after="paused"``. Attempt bookkeeping alone
    (goal still active) is not a transition and reports ``changed=False``.
    """
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("gatepause")
    _seed_transcript(sid, "Believe it's done; suite still red though.")
    _seed_goal(sid, status="active")
    GoalManager(session_id=sid).add_gate("exit 7", max_retries=1)

    with _judge_patch(DONE_REPLY) as judge, patch(
        "hermes_cli.goals.workspace_fingerprint", return_value=""
    ):
        first = rejudge_goal(sid)
        second = rejudge_goal(sid)

    assert first["outcome"] == "incomplete"
    assert first["changed"] is False  # attempt bookkeeping only, still active
    assert first["status_after"] == "active"
    assert second["outcome"] == "incomplete"
    assert second["changed"] is True  # persisted active -> paused transition
    assert second["status_after"] == "paused"
    judge.assert_not_called()

    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "paused"
    assert reloaded.paused_reason
    assert "quality gate exhausted" in reloaded.paused_reason


# ── evidence freshness + concurrent-goal identity ────────────────────


def test_stale_evidence_predating_goal_creation_is_refused(hermes_home):
    """The loop only ever judges responses produced AFTER the goal was
    set. A newest response that predates ``created_at`` belongs to an
    earlier goal on the same session — judging it against this goal
    could certify a false done. Refused explicitly."""
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("stale")
    old_ts = 1_700_000_000.0
    _seed_transcript(sid, "Previous goal finished ages ago.", evidence_timestamp=old_ts)
    _seed_goal(sid, created_at=old_ts + 3_600.0)  # goal set an hour later

    with _judge_patch(DONE_REPLY) as judge:
        result = rejudge_goal(sid)

    assert result["outcome"] == "stale_evidence"
    assert result["changed"] is False
    assert result["exit_code"] == 1
    judge.assert_not_called()


def test_fresh_evidence_after_goal_creation_judges_normally(hermes_home):
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("fresh")
    start = 1_700_000_000.0
    _seed_transcript(
        sid, "Rollout complete, all green.", evidence_timestamp=start + 90.0
    )
    _seed_goal(sid, created_at=start)

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid)

    assert result["outcome"] == "done"
    assert result["changed"] is True


def test_legacy_goal_without_created_at_still_judges(hermes_home):
    """Goals stored before ``created_at`` existed (0.0) can't be checked
    for freshness — they must judge exactly as before, not start
    refusing."""
    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("legacy")
    _seed_transcript(sid, "Evidence from any era.")  # timestamp = now
    _seed_goal(sid, created_at=0.0)

    with _judge_patch(DONE_REPLY):
        result = rejudge_goal(sid)

    assert result["outcome"] == "done"


def test_goal_changed_during_judge_latency_is_not_overwritten(hermes_home):
    """If the goal is re-set while the judge call is in flight, the stale
    snapshot must NOT be written over the new goal via mark_done.
    Regression: rejudge loaded state, waited on the judge, then wrote
    done+old-goal-text, clobbering the concurrent edit."""
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("toctou")
    _seed_transcript(sid, "Deployed everything, all green.")
    _seed_goal(sid, paused_reason="judge API unreachable 5 turns in a row")

    def judge_and_reset(goal, evidence, **kwargs):
        # Simulate a concurrent /goal set landing DURING judge latency.
        GoalManager(session_id=sid).set("New unrelated goal set mid-judge")
        return ("done", "verified", False, None, False)

    with patch("hermes_cli.goals.judge_goal", side_effect=judge_and_reset):
        result = rejudge_goal(sid)

    assert result["outcome"] == "goal_changed"
    assert result["changed"] is False
    assert result["exit_code"] == 1
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.goal == "New unrelated goal set mid-judge"
    assert reloaded.status != "done", "stale mark_done must not have landed"


def test_goal_done_elsewhere_during_judge_latency_reports_already_done(hermes_home):
    """A concurrent completion during the judge window is reported, not
    overwritten."""
    from hermes_cli.goals import GoalManager, load_goal, rejudge_goal

    sid = _rand_sid("dupdone")
    _seed_transcript(sid, "Deployed everything, all green.")
    _seed_goal(sid)

    def judge_and_finish(goal, evidence, **kwargs):
        GoalManager(session_id=sid).mark_done("finished elsewhere")
        return ("done", "verified here too", False, None, False)

    with patch("hermes_cli.goals.judge_goal", side_effect=judge_and_finish):
        result = rejudge_goal(sid)

    assert result["outcome"] == "already_done"
    assert result["changed"] is False
    reloaded = load_goal(sid)
    assert reloaded is not None
    assert reloaded.status == "done"
    assert reloaded.last_reason == "finished elsewhere"


# ── judge inputs ─────────────────────────────────────────────────────


def test_judge_receives_original_evidence_and_goal_context(hermes_home):
    """The judge must see the stored goal, the stored evidence, and any
    subgoals/contract attached to the goal — the same inputs the loop
    feeds it."""
    from hermes_state import SessionDB

    from hermes_cli.goals import GoalContract, GoalManager, rejudge_goal

    sid = _rand_sid("inputs")
    evidence = (
        "Rollout finished: canary at 100%, error rate 0.00%, dashboard green."
    )
    # Session + goal first, transcript second: the loop only judges
    # responses that postdate the goal, and so does rejudge.
    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
    finally:
        db.close()
    mgr = GoalManager(session_id=sid)
    mgr.set(
        "Ship the canary rollout",
        contract=GoalContract(verification="error rate under 0.1%"),
    )
    mgr.pause(reason="judge API unreachable 5 turns in a row")
    db = SessionDB()
    try:
        db.append_message(sid, role="user", content="do it")
        db.append_message(sid, role="assistant", content=evidence)
    finally:
        db.close()

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "verified", False, None, False),
    ) as mock_judge:
        result = rejudge_goal(sid)

    assert result["outcome"] == "done"
    mock_judge.assert_called_once()
    args, kwargs = mock_judge.call_args
    assert args[0] == "Ship the canary rollout"
    assert args[1] == evidence
    assert kwargs.get("subgoals") is None
    contract = kwargs.get("contract")
    assert contract is not None and contract.verification == "error rate under 0.1%"


def test_evidence_skips_tool_rows_and_picks_last_assistant(hermes_home):
    """A trailing tool result must not shadow the assistant's response."""
    from hermes_state import SessionDB

    from hermes_cli.goals import rejudge_goal

    sid = _rand_sid("toolrow")
    db = SessionDB()
    try:
        db.create_session(session_id=sid, source="cli")
        db.append_message(sid, role="user", content="check health")
        db.append_message(
            sid, role="tool", content='{"status": "ok"}', tool_name="terminal"
        )
        db.append_message(sid, role="assistant", content="Health check passed.")
        db.append_message(
            sid, role="tool", content="exit 0", tool_name="terminal"
        )
    finally:
        db.close()
    _seed_goal(sid)

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "not yet", False, None, False),
    ) as mock_judge:
        rejudge_goal(sid)

    assert mock_judge.call_args.args[1] == "Health check passed."
