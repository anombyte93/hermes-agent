"""Regression tests for iteration-limit exit normalization (#61631)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.turn_finalizer import finalize_turn


class _LimitAgent:
    def __init__(
        self,
        *,
        max_iterations=60,
        budget_remaining=0,
        completion_explainer=False,
    ):
        self.max_iterations = max_iterations
        self.iteration_budget = SimpleNamespace(
            remaining=budget_remaining, used=max_iterations, max_total=max_iterations
        )
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None
        self._handle_max_iterations_called = False
        self._completion_explainer = completion_explainer

    def _handle_max_iterations(self, messages, api_call_count):
        self._handle_max_iterations_called = True
        return "summary from extra call"

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._completion_explainer

    def _format_turn_completion_explanation(self, _reason):
        return "iteration-limit explanation"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def _finalize(
    agent,
    *,
    final_response,
    exit_reason,
    api_call_count=60,
    pending_verification_response=None,
    pending_verification_response_source=None,
):
    return finalize_turn(
        agent,
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response=pending_verification_response,
        _pending_verification_response_source=pending_verification_response_source,
    )
















@pytest.mark.parametrize(
    ("exit_reason", "interrupted", "failed"),
    [
        ("interrupted_by_user", True, False),
        ("all_retries_exhausted_no_response", False, False),
        ("provider_failure", False, True),
    ],
)
def test_pending_response_does_not_mask_later_terminal_exit(
    monkeypatch, exit_reason, interrupted, failed
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=interrupted,
        failed=failed,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
        _pending_verification_response="stale premature report",
    )

    assert result["final_response"] is None
    assert result["turn_exit_reason"] == exit_reason
    assert result["completed"] is False
    assert agent._handle_max_iterations_called is False


def test_pending_response_records_kanban_timeout(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    result = _finalize(
        agent,
        final_response=None,
        exit_reason="unknown",
        pending_verification_response="composed report",
    )

    assert result["turn_exit_reason"] == "max_iterations_reached(60/60)"
    record.assert_called_once_with(
        conn,
        "task-123",
        error=(
            "Iteration budget exhausted (60/60) — task could not complete "
            "within the allowed iterations"
        ),
        outcome="timed_out",
        release_claim=True,
        end_run=True,
        event_payload_extra={"budget_used": 60, "budget_max": 60},
    )


def test_published_pending_candidate_is_not_duplicated_by_finalizer(monkeypatch):
    """When budget exhaustion preserves a verification candidate that is
    already the tail assistant message, the finalizer must NOT append a
    duplicate. The content-comparison guard prevents this. (#65919 §7)
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _LimitAgent()
    report = "the composed report"

    result = finalize_turn(
        agent,
        final_response=report,
        api_call_count=60,
        interrupted=False,
        failed=False,
        # The candidate is already in messages as the tail assistant.
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": report},
        ],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="unknown",
        _pending_verification_response=report,
    )

    # The tail assistant already matches final_response — no duplicate appended.
    roles = [m["role"] for m in result["messages"]]
    assert roles == ["user", "assistant"]
    # Persisted messages should also have no duplicate.
    assert agent.persisted_messages is not None
    persisted_roles = [m["role"] for m in agent.persisted_messages]
    assert persisted_roles == ["user", "assistant"]


def test_bounded_fallback_records_kanban_failure_when_interrupted(monkeypatch):
    """When budget is exhausted and the turn was interrupted,
    ``finalize_turn`` must still record a terminal kanban failure via
    the bounded fallback path (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-456")
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    # Budget exhausted (60/60), interrupted, no fallback-eligible exit_reason
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    # The bounded fallback must fire even though interrupted=True
    # makes budget_fallback_eligible=False.
    record.assert_called_once()
    args, kwargs = record.call_args
    assert args[1] == "task-456"
    assert kwargs["outcome"] == "timed_out"
    assert kwargs["release_claim"] is True
    assert kwargs["end_run"] is True
    assert kwargs["event_payload_extra"]["budget_used"] == 60
    assert kwargs["event_payload_extra"]["budget_max"] == 60


def test_bounded_fallback_records_kanban_failure_when_failed(monkeypatch):
    """When budget is exhausted and the turn failed,
    the bounded fallback must still record a terminal kanban failure (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-789")
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=False,
        failed=True,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="provider_failure",
    )

    record.assert_called_once()
    args, kwargs = record.call_args
    assert args[1] == "task-789"
    assert kwargs["outcome"] == "timed_out"


def test_bounded_fallback_does_not_fire_without_kanban_task(monkeypatch):
    """When budget is exhausted and interrupted but no kanban task is
    active, the bounded fallback must NOT fire (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent()

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=60,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    record.assert_not_called()


def test_bounded_fallback_does_not_fire_when_budget_not_exhausted(monkeypatch):
    """When budget is NOT exhausted but turn is interrupted and a kanban
    task is active, the bounded fallback must NOT fire (#87096).
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-999")
    record = MagicMock(name="record_task_failure")
    conn = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_db.connect", lambda: conn)
    monkeypatch.setattr("hermes_cli.kanban_db._record_task_failure", record)
    agent = _LimitAgent(budget_remaining=60)

    # api_call_count=10, max_iterations=60 — budget NOT exhausted
    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=10,
        interrupted=True,
        failed=False,
        messages=[{"role": "user", "content": "task"}],
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="task",
        original_user_message="task",
        _should_review_memory=False,
        _turn_exit_reason="interrupted_by_user",
    )

    record.assert_not_called()


def _running_task(tmp_path, monkeypatch, *, max_retries=2):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(
        conn,
        title="late report",
        assignee="default",
        max_retries=max_retries,
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    return kb, task_id


def test_late_terminal_report_is_preserved_for_review_not_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    kb, task_id = _running_task(tmp_path, monkeypatch)
    secret = "ghp_" + "Z" * 40
    report = "final report\n" + ("A" * 5000) + f"\ntoken: {secret}\nPASS"

    result = _finalize(
        _LimitAgent(),
        final_response=None,
        exit_reason="unknown",
        pending_verification_response=report,
        pending_verification_response_source="kanban_stop",
    )

    assert result["final_response"] == report
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        events = kb.list_events(conn, task_id)
        assert task.status == "review"
        assert run.outcome == "timed_out_with_output"
        assert run.status == "review"
        assert kb.goal_run_status(conn, task_id, run.id) == "review"
        assert run.summary is not None
        assert "Final output tail" in run.summary
        assert secret not in run.summary
        assert len(run.summary) <= 4600
        assert any(event.kind == "timed_out_with_output" for event in events)
        assert kb.has_spawnable_ready(conn) is False
        assert kb.has_spawnable_review(conn) is True

        review_run = kb.claim_review_task(conn, task_id)
        assert review_run is not None
        changed, implementer = kb.request_changes(
            conn,
            task_id,
            reason="one assertion still fails",
            expected_run_id=review_run.current_run_id,
        )
        assert changed is True
        assert implementer == "default"
        assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


def test_no_terminal_report_keeps_ordinary_timeout_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    kb, task_id = _running_task(tmp_path, monkeypatch)

    _finalize(
        _LimitAgent(),
        final_response=None,
        exit_reason="unknown",
    )

    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert task.status == "ready"
        assert run.outcome == "timed_out"
        assert kb.has_spawnable_ready(conn) is True
        assert kb.has_spawnable_review(conn) is False
    finally:
        conn.close()


