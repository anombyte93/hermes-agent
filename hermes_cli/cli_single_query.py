"""Shared automation status for rendered (-q) and quiet (-Q) one-shot turns."""
from __future__ import annotations

import os
from typing import Any

from agent.error_classifier import FailoverReason


def single_query_exit_code(result: Any) -> int:
    """Use the turn's classifier verdict, never classify rendered response text."""
    if not isinstance(result, dict) or not result.get("failed"):
        return 0
    if os.environ.get("HERMES_KANBAN_TASK") and result.get("failure_reason") in {
        FailoverReason.rate_limit.value, FailoverReason.billing.value,
        FailoverReason.upstream_rate_limit.value,
    }:
        from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE

        return KANBAN_RATE_LIMIT_EXIT_CODE
    return 1
