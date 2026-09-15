"""Permanent controls for typed iteration-vs-clock timeout rendering (#113).

RED-first: every case here FAILED against the old inline
``max_runtime={int(_payload(ev, 'limit_seconds') or 0)}s`` formatter.
"""

from __future__ import annotations

from gateway.kanban_timeout_format import format_timeout_budget


def _fmt(payload):
    return format_timeout_budget(payload)


def test_iteration_exhaustion_names_iterations_not_zero_clock():
    # The measured run98 case: 150/150 iterations, 7200s limit never reached.
    out = _fmt({"iterations": 150, "max_iterations": 150, "limit_seconds": 7200})
    assert "150/150" in out
    assert "max_runtime=0s" not in out
    assert "7200" in out  # clock limit named, not coerced away


def test_clock_timeout_renders_limit():
    out = _fmt({"limit_seconds": 3600})
    assert out == "max_runtime=3600s"


def test_missing_limit_renders_unknown_never_zero():
    out = _fmt({})
    assert "unknown" in out
    assert "0s" not in out


def test_iterations_without_limit_still_typed():
    out = _fmt({"iterations": 150, "max_iterations": 150})
    assert "150/150" in out
    assert "0s" not in out


def test_none_payload_is_unknown():
    out = _fmt(None)
    assert "unknown" in out


def test_partial_iteration_counts_render_gracefully():
    out = _fmt({"iterations": 150})
    assert "150" in out
    assert "0s" not in out
