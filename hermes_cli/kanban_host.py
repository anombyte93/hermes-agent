"""Operator-selected worker host, independent of the model's inference host."""

import socket


def require_execution_host(config: dict, hostname: str | None = None) -> None:
    """Refuse a misplaced worker before any child process is created.

    No setting preserves upstream behaviour. A configured host is a placement
    requirement, never permission to silently fall back to the caller's PC.
    """
    kanban = config.get("kanban", {})
    if not isinstance(kanban, dict):
        raise ValueError("kanban configuration must be a mapping")
    expected = kanban.get("execution_host")
    if expected is None:
        return
    if not isinstance(expected, str) or not expected.strip():
        raise ValueError("kanban.execution_host must name a non-empty hostname")
    actual = (hostname if hostname is not None else socket.gethostname()).casefold()
    expected = expected.strip().casefold()
    if actual != expected:
        raise RuntimeError(
            f"Kanban worker execution requires host {expected!r}; this dispatcher "
            f"is on {actual!r}. Run this board's dispatcher and workspace on "
            f"{expected!r}. No local fallback was started. Model/provider "
            "selection does not change the execution host."
        )
