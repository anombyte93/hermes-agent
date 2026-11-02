"""Tests for live auto-decompose settings resolution (issue #49638).

The gateway dispatcher used to capture ``kanban.auto_decompose`` once at boot,
so a user who flipped it to ``false`` to STOP runaway auto-decompose (which had
created and launched tasks they didn't intend) found the flag had no effect
without a full gateway restart. ``_resolve_auto_decompose_settings`` is now
called every tick, reading the current config.
"""

from __future__ import annotations

import pytest

from gateway.kanban_watchers_common import _resolve_auto_decompose_settings


def test_enabled_by_default_when_key_absent():
    enabled, per_tick = _resolve_auto_decompose_settings(lambda: {"kanban": {}})
    assert enabled is True
    assert per_tick == 3


def test_disabled_when_flag_false():
    enabled, per_tick = _resolve_auto_decompose_settings(
        lambda: {"kanban": {"auto_decompose": False}}
    )
    assert enabled is False


def test_auto_decompose_skips_legacy_external_kind_without_spending_budget(tmp_path, monkeypatch):
    from pathlib import Path
    from unittest.mock import Mock
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_decompose as decomp

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    with dbc.connect_closing(board="default") as conn:
        ext = kb.create_task(conn, title="legacy external", execution_kind="external_session")
        worker = kb.create_task(conn, title="real triage", triage=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (ext,))
    # Real list includes old/imported external rows. Filter before the auxiliary
    # call and before charging the per-tick cap, leaving room for worker triage.
    assert ext in decomp.list_triage_ids()
    auxiliary = Mock(return_value=decomp.DecomposeOutcome(worker, ok=True))
    monkeypatch.setattr(decomp, "decompose_task", auxiliary)
    dispatcher = _KanbanDispatcher(kb, Mock())
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    for _ in range(2):
        assert dispatcher.auto_decompose_tick(1) == 1
    assert [call.args[0] for call in auxiliary.call_args_list] == [worker, worker]
