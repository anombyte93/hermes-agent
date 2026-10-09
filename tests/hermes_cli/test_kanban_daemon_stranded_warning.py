"""The standalone daemon must say so when it skips cards it can never dispatch.

Upstream treats a non-profile assignee as a control-plane lane pulled via ``claim_task``
and deliberately suppresses the "stuck" warning for it. On an estate that has no such
lanes, ``kanban.require_known_assignee`` says so -- and then a ``skipped_nonspawnable``
card is an error the operator must hear about, not correctly-idle silence.

Measured 2026-09-23: 17 cards assigned to ``developer``/``frontend-dev``/``designer`` sat
in ``ready`` for 21 days beside a daemon whose verbose log printed nothing about them,
because its tick line only prints when ``did_work`` and its health warning keys off
``has_spawnable_ready()``, which is False for exactly these cards.
"""
from __future__ import annotations

from hermes_cli import kanban_ops


def test_first_sighting_names_the_cards_it_can_never_dispatch():
    state = {"last_stranded_warn_at": 0}
    line = kanban_ops.stranded_warning(["t_aaa", "t_bbb"], now=1_000_000, state=state, enabled=True)
    assert line is not None
    assert "t_aaa" in line and "t_bbb" in line
    assert "not a Hermes profile" in line or "never be dispatched" in line
    assert state["last_stranded_warn_at"] == 1_000_000


def test_rate_limited_to_once_per_five_minutes():
    state = {"last_stranded_warn_at": 1_000_000}
    assert kanban_ops.stranded_warning(["t_aaa"], now=1_000_000 + 299, state=state, enabled=True) is None
    assert kanban_ops.stranded_warning(["t_aaa"], now=1_000_000 + 300, state=state, enabled=True) is not None


def test_silent_when_the_operator_has_not_opted_in():
    state = {"last_stranded_warn_at": 0}
    assert kanban_ops.stranded_warning(["t_aaa"], now=1_000_000, state=state, enabled=False) is None
    assert state["last_stranded_warn_at"] == 0


def test_silent_when_nothing_was_skipped():
    state = {"last_stranded_warn_at": 0}
    assert kanban_ops.stranded_warning([], now=1_000_000, state=state, enabled=True) is None


def test_the_flag_is_readable_by_name_from_the_library(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert kb.require_known_assignee_enabled() is False
    (home / "config.yaml").write_text("kanban:\n  require_known_assignee: true\n")
    assert kb.require_known_assignee_enabled() is True
