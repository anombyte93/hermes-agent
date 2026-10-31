"""The end of a ``--no-fallback`` run that failed on its pinned provider (#107).

``hermes chat --no-fallback`` (and a Kanban card created with ``--no-fallback``)
never switches to ``fallback_providers``. When the pinned route then fails on a
provider error (429, quota, auth, outage), the run must say so plainly: the
reason names that fallback was disabled, which route failed and why. A Kanban
worker also blocks its own card with that reason, so the run's final status
carries it instead of the card being silently requeued or retried elsewhere.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping, Optional

from hermes_cli.fallback_config import NO_FALLBACK_FLAG

# Block kind for the card: a hard wall the worker cannot clear by itself.
NO_FALLBACK_BLOCK_KIND = "capability"


def _reason_text(failure_reason: str) -> str:
    try:
        from agent.chat_completion_helpers import _fallback_reason_text
        from agent.error_classifier import FailoverReason

        return _fallback_reason_text(FailoverReason(failure_reason))
    except Exception:
        return failure_reason.replace("_", " ")


def no_fallback_failure_reason(
    result: Any, *, model: Optional[str], provider: Optional[str],
) -> Optional[str]:
    """The operator-facing reason for a provider failure, else None.

    Only a failed turn that carries a classified ``failure_reason`` (the
    provider-error paths in ``agent.turn_recovery``) qualifies; a successful,
    interrupted or non-provider failure is left to the normal exit handling.
    """
    if not isinstance(result, Mapping) or not result.get("failed"):
        return None
    failure_reason = str(result.get("failure_reason") or "").strip()
    if not failure_reason:
        return None
    route = f"{model or '?'} via {provider or '?'}"
    detail = " ".join(str(result.get("error") or "").split())[:300]
    reason = (f"fallback disabled ({NO_FALLBACK_FLAG}): {route} failed "
              f"({_reason_text(failure_reason)})")
    if detail:
        reason += f": {detail}"
    return reason + ". fallback_providers were not tried."


def _block_own_card(task_id: str, run_id: Optional[int], reason: str) -> bool:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        return kb.block_task(conn, task_id, reason=reason, kind=NO_FALLBACK_BLOCK_KIND,
                             expected_run_id=run_id)


def finish_no_fallback_failure(
    result: Any, *, model: Optional[str], provider: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Report a provider failure of a ``--no-fallback`` run; None if there was none.

    Prints the reason to stderr. Inside a dispatcher-spawned Kanban worker
    (``HERMES_KANBAN_TASK``) it also blocks the worker's own card with the
    reason, fenced to this worker's run. The caller exits non-zero.
    """
    reason = no_fallback_failure_reason(result, model=model, provider=provider)
    if reason is None:
        return None
    try:
        from agent.redact import redact_sensitive_text

        reason = redact_sensitive_text(reason, force=True)
    except Exception:
        pass
    print(f"Error: {reason}", file=sys.stderr)
    env = os.environ if environ is None else environ
    task_id = (env.get("HERMES_KANBAN_TASK") or "").strip()
    if task_id:
        raw_run = (env.get("HERMES_KANBAN_RUN_ID") or "").strip()
        run_id = int(raw_run) if raw_run.isdigit() else None
        try:
            if not _block_own_card(task_id, run_id, reason):
                print(f"kanban: could not block {task_id} (stale run or not running)", file=sys.stderr)
        except Exception as exc:
            print(f"kanban: could not block {task_id}: {exc}", file=sys.stderr)
    return reason
