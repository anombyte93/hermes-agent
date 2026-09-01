"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    build_kanban_wrapup_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_no_nudge_after_kanban_request_review(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {
                        "name": "kanban_request_review",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_request_review",
            "tool_call_id": "1",
            "content": "review",
        },
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_wrapup_nudge_reserves_two_calls_for_report_and_lifecycle(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_late")

    assert build_kanban_wrapup_nudge(
        api_call_count=57, max_iterations=60, already_issued=False,
    ) is None

    nudge = build_kanban_wrapup_nudge(
        api_call_count=58, max_iterations=60, already_issued=False,
    )
    assert nudge is not None
    assert "2 calls remain" in nudge
    assert "final report" in nudge.lower()
    assert "kanban_complete" in nudge
    assert "kanban_request_review" in nudge
    assert "kanban_block" in nudge

    assert build_kanban_wrapup_nudge(
        api_call_count=58, max_iterations=60, already_issued=True,
    ) is None


def test_wrapup_nudge_skips_non_kanban_and_tiny_budgets(clear_kanban_env):
    assert build_kanban_wrapup_nudge(
        api_call_count=58, max_iterations=60, already_issued=False,
    ) is None

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_tiny")
    assert build_kanban_wrapup_nudge(
        api_call_count=0, max_iterations=2, already_issued=False,
    ) is None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




