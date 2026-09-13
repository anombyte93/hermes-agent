"""Model truth for kanban runs (#7): record who ACTUALLY answered.

A board's ``model_override`` is a request. The fact — which model/provider
served the worker's first successful API call, and whether a provider
fallback silently substituted another model — lives in the agent runtime and
was previously written nowhere durable. ``record_response_usage`` calls
:func:`record_model_truth_once` after every successful response; the function
no-ops until the first call inside a dispatcher-spawned worker
(``HERMES_KANBAN_TASK`` + ``HERMES_KANBAN_RUN_ID``), then stamps the fact on
the run row exactly once. Best-effort by contract: a board failure must never
raise into the conversation loop.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("agent.kanban_model_truth")

# Once-latch per process: the first successful API call is the fact; later
# calls (fallback restored, next turn, ...) must not rewrite it.
_recorded = False


def model_truth_snapshot(agent: Any) -> Dict[str, Optional[str]]:
    """Derive ``{model_used, provider_used, fallback_from}`` from agent state.

    Pure: reads only current route + the fallback markers the runtime already
    maintains. After a fallback, ``agent.model``/``agent.provider`` ARE the
    fallback's; the original request survives only in the per-turn
    ``_primary_runtime`` snapshot (or, failing that, the route pair).
    """
    model = str(getattr(agent, "model", "") or "")
    provider = getattr(agent, "provider", None)
    provider = str(provider) if provider else None
    fallback_from: Optional[str] = None
    route = getattr(agent, "_provider_fallback_route", None)
    if isinstance(route, (list, tuple)) and len(route) == 2 and str(route[0]) == model:
        rt = getattr(agent, "_primary_runtime", None)
        if isinstance(rt, dict) and rt.get("model"):
            fallback_from = f"{rt['model']} ({rt.get('provider') or 'unknown'})"
        else:
            fallback_from = f"{route[0]} ({route[1] or 'unknown'})"
    return {"model_used": model, "provider_used": provider, "fallback_from": fallback_from}


def _write_model_truth(task_id: str, run_id: int, truth: Dict[str, Optional[str]]) -> bool:
    """Open the board and stamp the run row (seams: tests monkeypatch this)."""
    import os

    from hermes_cli import kanban_db_connect as kbc

    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    with kbc.connect_closing(board=board) as conn:
        from hermes_cli import kanban_db as kb

        return kb.record_run_model_truth(
            conn, task_id, run_id,
            model_used=truth["model_used"] or "",
            provider_used=truth.get("provider_used"),
            fallback_from=truth.get("fallback_from"),
        )


def record_model_truth_once(agent: Any) -> bool:
    """Stamp the answering model on this worker's run, once; never raises.

    Returns True when the fact was written this call. Failures leave the
    latch open so the next successful call retries (a reclaim between spawn
    and first call moves the run row; one lost write is not worth a crash).
    """
    global _recorded
    if _recorded:
        return False
    import os

    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    raw_run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not task_id or not raw_run:
        return False
    truth = model_truth_snapshot(agent)
    if not truth.get("model_used"):
        return False
    try:
        run_id = int(raw_run)
    except ValueError:
        return False
    try:
        written = _write_model_truth(task_id, run_id, truth)
    except Exception:
        logger.debug("kanban model truth: board write failed", exc_info=True)
        return False
    if written:
        _recorded = True
    return written
