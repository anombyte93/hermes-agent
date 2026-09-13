"""Regression tests: a READY card whose assignee is not a live Hermes profile
must announce itself.

The dispatcher deliberately refuses to spawn for an assignee that does not
resolve to a profile (``skipped_nonspawnable``) — that fail-closed behaviour is
correct and must stay, because ``hermes -p <assignee>`` would crash on startup
and the card would loop back to ``ready`` forever.

The bug was that the refusal was SILENT: no event, no log line. Combined with
``has_spawnable_ready()`` — which counts only assignees that DO resolve — such a
card could never raise the dispatcher's "stuck" alarm either, so it sat ``ready``
indefinitely and every unattended surface reported a healthy, idle board.

These tests pin the loud behaviour in both directions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def kb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh HERMES_HOME and a clean kanban DB, with no profile resolving."""
    home = tmp_path / ".hermes"
    (home / "profiles").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    # Only "evo" is a real profile; "developer" is not.
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists", lambda name: name == "evo"
    )
    return kb


def _skip_events(kb, conn, task_id: str):
    return [
        ev for ev in kb.list_events(conn, task_id)
        if ev.kind == "dispatch_skipped"
    ]


# ---------------------------------------------------------------------------
# Durable, rate-limited event
# ---------------------------------------------------------------------------

def test_unresolvable_assignee_appends_durable_event(kb_env) -> None:
    kb = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ghost lane", assignee="developer")
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1234, reconcile_orphans=False
        )
        events = _skip_events(kb, conn, task_id)
    finally:
        conn.close()

    assert result.skipped_nonspawnable == [task_id]
    assert result.spawned == []
    assert len(events) == 1, "the skip must leave a durable trace"
    assert events[0].payload == {
        "reason": "assignee_not_a_profile",
        "assignee": "developer",
    }


def test_unresolvable_assignee_logs_a_warning(kb_env, caplog) -> None:
    kb = kb_env
    conn = kb.connect()
    try:
        kb.create_task(conn, title="ghost lane", assignee="developer")
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
            kb.dispatch_once(
                conn, spawn_fn=lambda *a, **k: 1234, reconcile_orphans=False
            )
    finally:
        conn.close()

    assert any(
        "not a live Hermes profile" in r.getMessage()
        and "developer" in r.getMessage()
        for r in caplog.records
    ), "the operator-facing log line must name the assignee"


def test_repeated_ticks_do_not_spam_events(kb_env) -> None:
    """A 30s dispatch interval must not write 120 rows an hour."""
    kb = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ghost lane", assignee="developer")
        for _ in range(5):
            kb.dispatch_once(
                conn, spawn_fn=lambda *a, **k: 1234, reconcile_orphans=False
            )
        events = _skip_events(kb, conn, task_id)
    finally:
        conn.close()

    assert len(events) == 1, (
        f"unchanged skip must be rate-limited to one notice; got {len(events)}"
    )


def test_stranded_card_renotifies_after_the_window(kb_env) -> None:
    """The card must not go quiet while it is still stranded — this is the
    half of the fix that stops it sitting READY-and-invisible forever."""
    kb = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ghost lane", assignee="developer")
        kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1234, reconcile_orphans=False
        )
        # Age the existing notice past the renotify window.
        conn.execute(
            "UPDATE task_events SET created_at = created_at - ? "
            "WHERE task_id = ? AND kind = 'dispatch_skipped'",
            (kb.NONSPAWNABLE_RENOTIFY_SECONDS + 60, task_id),
        )
        conn.commit()
        kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1234, reconcile_orphans=False
        )
        events = _skip_events(kb, conn, task_id)
    finally:
        conn.close()

    assert len(events) == 2, "a still-stranded card must re-announce itself"


def test_dry_run_writes_no_event(kb_env) -> None:
    kb = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="ghost lane", assignee="developer")
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 1234, dry_run=True,
            reconcile_orphans=False,
        )
        events = _skip_events(kb, conn, task_id)
    finally:
        conn.close()

    assert result.skipped_nonspawnable == [task_id]
    assert events == [], "a dry run must leave the board byte-for-byte alone"


def test_resolvable_assignee_still_spawns_silently(kb_env) -> None:
    """Positive control: a real profile must dispatch normally and record no
    skip notice — the fix must not make every card noisy."""
    kb = kb_env
    spawns = {"n": 0}

    def spawn_fn(*args, **kwargs):
        spawns["n"] += 1
        return 4321

    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="real lane", assignee="evo")
        result = kb.dispatch_once(
            conn, spawn_fn=spawn_fn, reconcile_orphans=False
        )
        events = _skip_events(kb, conn, task_id)
    finally:
        conn.close()

    assert result.skipped_nonspawnable == []
    assert [t for (t, _who, _ws) in result.spawned] == [task_id]
    assert spawns["n"] == 1
    assert events == []


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------

@pytest.fixture()
def cli_env(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_nonspawnable_cli_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def test_dispatch_text_does_not_call_a_stranded_card_ok(
    cli_env, monkeypatch, capsys
) -> None:
    """The old renderer asserted 'terminal lane, OK' — which is exactly how a
    typo'd assignee read as healthy."""
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    res = kanban_db.DispatchResult(skipped_nonspawnable=["t_ghost"])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda conn, **kw: res)

    args = argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    assert kb_cli._cmd_dispatch(args) == 0
    out = capsys.readouterr().out

    assert "t_ghost" in out
    assert "OK" not in out
    assert "typo" in out


def test_dispatch_json_still_reports_the_bucket(
    cli_env, monkeypatch, capsys
) -> None:
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    res = kanban_db.DispatchResult(skipped_nonspawnable=["t_ghost"])
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda conn, **kw: res)

    args = argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=True)
    assert kb_cli._cmd_dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["skipped_nonspawnable"] == ["t_ghost"]
