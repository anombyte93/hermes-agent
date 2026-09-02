"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete",
    "kanban_request_review",
    "kanban_block",
})

_DEFAULT_MAX_ATTEMPTS = 2

# Reserve one call for a bounded final report and one for the terminal board
# tool. Smaller budgets keep their historical behaviour rather than spending
# the whole turn wrapping up.
_KANBAN_WRAPUP_RESERVE_CALLS = 2
_KANBAN_WRAPUP_MIN_BUDGET = 4


def build_kanban_wrapup_nudge(
    *,
    api_call_count: int,
    max_iterations: int,
    already_issued: bool,
) -> Optional[str]:
    """Return the one-shot Kanban wrap-up prompt before the hard ceiling.

    The prompt is issued with two provider calls remaining: one to compose the
    final deliverable and one to invoke the lifecycle tool. It is Kanban-only so
    ordinary interactive turns retain the no-pressure-warning behaviour.
    """
    if already_issued or not kanban_stop_nudge_enabled():
        return None
    try:
        used = int(api_call_count)
        maximum = int(max_iterations)
    except (TypeError, ValueError):
        return None
    if maximum < _KANBAN_WRAPUP_MIN_BUDGET:
        return None
    remaining = maximum - used
    if remaining != _KANBAN_WRAPUP_RESERVE_CALLS:
        return None
    return (
        "[System: Kanban iteration reserve reached. Exactly 2 calls remain. "
        "Stop starting new work. Use this response to finish and state the "
        "bounded final report, then use the remaining call to invoke exactly "
        "one terminal board tool: `kanban_complete`, `kanban_request_review`, "
        "or `kanban_block`. Do not end with narration or a promise.]"
    )


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, `kanban_request_review(summary=...)` if implementation is "
        "finished but needs same-card review, OR `kanban_block(reason=...)` if "
        "you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "build_kanban_wrapup_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
