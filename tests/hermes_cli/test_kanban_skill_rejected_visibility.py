"""Regression tests for issue #20: forced-skill dispatch refusals must be
visible at every operator surface.

A READY card that requests a forced skill its assignee profile cannot load
is rejected before claim/workspace/spawn (fail-closed — that behaviour is
deliberate and must stay). The bug was that the refusal was invisible:
``dispatch --json`` omitted ``rejected_skills``/``respawn_guarded``, the
text renderer said nothing, no durable event was appended, and diagnostics
returned ``[]``. These tests pin the loud behaviour in both directions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: visibility test skill\n---\n\nBODY\n",
        encoding="utf-8",
    )


@pytest.fixture()
def kb_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh HERMES_HOME with a coder profile and a clean kanban DB."""
    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / "coder"
    (profile_home / "skills").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    return kb, profile_home


def _rejection_events(kb, conn, task_id: str):
    return [ev for ev in kb.list_events(conn, task_id) if ev.kind == "skill_rejected"]


# ---------------------------------------------------------------------------
# Durable, rate-limited event
# ---------------------------------------------------------------------------

def test_missing_forced_skill_appends_durable_event(kb_env) -> None:
    kb, _ = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="bad skill", assignee="coder",
            skills=["missing-skill-xyz"],
        )
        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1234,
                                  reconcile_orphans=False)
        events = _rejection_events(kb, conn, task_id)
    finally:
        conn.close()

    assert result.rejected_skills == [(task_id, ["missing-skill-xyz"])]
    assert len(events) == 1
    # Payload carries only the refusal facts: assignee + missing skill names.
    # The task id is the event's own row identity, never duplicated inside.
    assert events[0].payload == {
        "assignee": "coder",
        "missing_skills": ["missing-skill-xyz"],
    }


def test_repeated_dispatch_does_not_spam_duplicate_events(kb_env) -> None:
    kb, _ = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="bad skill", assignee="coder",
            skills=["missing-skill-xyz"],
        )
        for _ in range(3):
            kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1234,
                             reconcile_orphans=False)
        events = _rejection_events(kb, conn, task_id)
    finally:
        conn.close()

    assert len(events) == 1, (
        f"unchanged refusal must be rate-limited to one event; got {len(events)}"
    )


def test_changed_refusal_appends_fresh_event(kb_env) -> None:
    kb, _ = kb_env
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="bad skill", assignee="coder",
            skills=["missing-skill-xyz"],
        )
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1234,
                         reconcile_orphans=False)
        # Operator swaps the requested skill for a different missing one —
        # that is a NEW refusal and must be recorded, not rate-limited away.
        conn.execute(
            "UPDATE tasks SET skills = ? WHERE id = ?",
            (json.dumps(["a-different-missing-skill"]), task_id),
        )
        conn.commit()
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 1234,
                         reconcile_orphans=False)
        events = _rejection_events(kb, conn, task_id)
    finally:
        conn.close()

    assert len(events) == 2
    assert events[0].payload["missing_skills"] == ["missing-skill-xyz"]
    assert events[1].payload["missing_skills"] == ["a-different-missing-skill"]


def test_loadable_forced_skill_still_spawns_with_no_rejection(kb_env) -> None:
    """Positive control: a skill the profile CAN load must dispatch normally
    and record no rejection anywhere."""
    kb, profile_home = kb_env
    _write_skill(profile_home / "skills", "loadable-skill")
    spawns = {"n": 0}

    def spawn_fn(*args, **kwargs):
        spawns["n"] += 1
        return 4321

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="good skill", assignee="coder",
            skills=["loadable-skill"],
        )
        result = kb.dispatch_once(conn, spawn_fn=spawn_fn,
                                  reconcile_orphans=False)
        events = _rejection_events(kb, conn, task_id)
    finally:
        conn.close()

    assert result.rejected_skills == []
    assert [t for (t, _who, _ws) in result.spawned] == [task_id]
    assert spawns["n"] == 1
    assert events == []


# ---------------------------------------------------------------------------
# CLI rendering (JSON + text)
# ---------------------------------------------------------------------------

@pytest.fixture()
def cli_env(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_skill_reject_cli_")
    os.makedirs(os.path.join(test_home, "profiles", "default"), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


def _patched_dispatch(monkeypatch, result):
    from hermes_cli import kanban_db
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(kanban_db, "dispatch_once", lambda conn, **kw: result)


def test_dispatch_json_renders_rejected_skills_and_respawn_guarded(
    cli_env, monkeypatch, capsys
) -> None:
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    res = kanban_db.DispatchResult(
        rejected_skills=[("t_bad", ["missing-skill-xyz"])],
        respawn_guarded=[("t_guard", "rate_limit_cooldown")],
    )
    _patched_dispatch(monkeypatch, res)

    args = argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=True)
    assert kb_cli._cmd_dispatch(args) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["rejected_skills"] == [
        {"task_id": "t_bad", "missing_skills": ["missing-skill-xyz"]}
    ]
    assert out["respawn_guarded"] == [
        {"task_id": "t_guard", "reason": "rate_limit_cooldown"}
    ]


def test_dispatch_text_names_rejected_task_and_skills(
    cli_env, monkeypatch, capsys
) -> None:
    from hermes_cli import kanban as kb_cli
    from hermes_cli import kanban_db

    res = kanban_db.DispatchResult(
        rejected_skills=[("t_bad", ["missing-skill-xyz"])],
        respawn_guarded=[("t_guard", "rate_limit_cooldown")],
    )
    _patched_dispatch(monkeypatch, res)

    args = argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    assert kb_cli._cmd_dispatch(args) == 0
    out = capsys.readouterr().out

    assert "t_bad" in out and "missing-skill-xyz" in out
    assert "t_guard" in out and "rate_limit_cooldown" in out


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _task_row(status: str = "ready", assignee: str = "coder") -> dict:
    return {
        "status": status,
        "assignee": assignee,
        "claim_lock": None,
        "created_at": 1000,
    }


def _reject_event(ts: int = 2000) -> dict:
    return {
        "kind": "skill_rejected",
        "payload": {"assignee": "coder", "missing_skills": ["missing-skill-xyz"]},
        "created_at": ts,
    }


def test_diagnostics_flags_ready_card_with_active_rejection() -> None:
    from hermes_cli import kanban_diagnostics as kd

    diags = kd.compute_task_diagnostics(
        _task_row(), [_reject_event()], [], now=3000,
    )
    kinds = [d.kind for d in diags]
    assert "skill_rejected" in kinds
    d = next(d for d in diags if d.kind == "skill_rejected")
    assert d.data["missing_skills"] == ["missing-skill-xyz"]
    assert d.data["assignee"] == "coder"
    assert "missing-skill-xyz" in d.detail


def test_diagnostics_quiet_when_no_rejection_event() -> None:
    """A card with no missing skill must not gain a false diagnostic."""
    from hermes_cli import kanban_diagnostics as kd

    diags = kd.compute_task_diagnostics(
        _task_row(),
        [{"kind": "created", "payload": None, "created_at": 1000}],
        [],
        now=3000,
    )
    assert "skill_rejected" not in [d.kind for d in diags]


def test_diagnostics_cleared_once_spawn_supersedes_rejection() -> None:
    from hermes_cli import kanban_diagnostics as kd

    events = [
        _reject_event(ts=2000),
        {"kind": "spawned", "payload": {"pid": 1}, "created_at": 2500},
    ]
    diags = kd.compute_task_diagnostics(_task_row(), events, [], now=3000)
    assert "skill_rejected" not in [d.kind for d in diags]


def test_diagnostics_quiet_when_card_no_longer_ready() -> None:
    from hermes_cli import kanban_diagnostics as kd

    diags = kd.compute_task_diagnostics(
        _task_row(status="running"), [_reject_event()], [], now=3000,
    )
    assert "skill_rejected" not in [d.kind for d in diags]
