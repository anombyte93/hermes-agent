"""Typed iteration-vs-clock formatting for kanban watcher notifications.

Extracted from the inline f-string so it is unit-testable. A timed-out run
whose budget was consumed by ITERATIONS is not a clock timeout: rendering it
as max_runtime=0s (the `or 0` coercion of an absent limit_seconds) misstates
both the budget kind and its size (#113). A run that exhausted iterations
carries iterations/ max_iterations in the payload; a clock timeout carries
limit_seconds. Both may be present (whichever fired first wins), and a
missing limit is stated as missing, never as zero.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


def format_timeout_budget(payload: Optional[Mapping[str, Any]]) -> str:
    """Render the budget clause for a ``timed_out`` event.

    Rules (issue #113):
    - iterations exhausted (iterations/max_iterations present) renders the
      iteration budget, and names the clock limit only when present;
    - clock timeout renders ``max_runtime=<N>s`` from limit_seconds;
    - an absent limit renders ``max_runtime=unknown`` — never 0.
    """
    payload = payload or {}
    iterations = payload.get("iterations")
    max_iterations = payload.get("max_iterations")
    limit_seconds = payload.get("limit_seconds")

    parts: list[str] = []
    if iterations is not None or max_iterations is not None:
        it = f"{int(iterations)}/{int(max_iterations)}" if (
            iterations is not None and max_iterations is not None
        ) else str(int(iterations or max_iterations))  # type: ignore[arg-type]
        parts.append(f"iteration budget exhausted ({it} iterations)")
        if limit_seconds is not None:
            parts.append(f"clock limit {int(limit_seconds)}s not reached")
    if not parts:
        if limit_seconds is not None:
            parts.append(f"max_runtime={int(limit_seconds)}s")
        else:
            parts.append("max_runtime=unknown (no limit recorded)")
    return "; ".join(parts)


def format_budget_facts(payload: Optional[Mapping[str, Any]]) -> str:
    """One line from a budget event: turns used, this run's commits and its last activity.

    ``new_commits`` of None is unprovable and says so, because "no commits" is a claim.
    """
    payload = payload or {}
    turns = f"{payload.get('iterations', '?')}/{payload.get('max_iterations', '?')} turns"
    commits = payload.get("new_commits")
    if commits is None:
        said = "commits unknown"
    else:
        said = "no commits this run" if not commits else f"{int(commits)} commit(s) this run"
    activity = str(payload.get("last_activity") or "").strip()
    return f"{turns}, {said}" + (f", last: {activity}" if activity else "")
