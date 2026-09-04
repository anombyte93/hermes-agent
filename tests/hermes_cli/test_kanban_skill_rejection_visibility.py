from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / "coder"
    (profile_home / "skills").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: None)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb.init_db()
    return profile_home


def _write_skill(profile_home: Path, name: str) -> None:
    skill_dir = profile_home / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n",
        encoding="utf-8",
    )


def test_dispatch_rejection_is_durable_rate_limited_and_loadable_skill_spawns(
    isolated_home: Path,
) -> None:
    _write_skill(isolated_home, "loadable-skill")
    spawned: list[str] = []

    with kb.connect_closing() as conn:
        rejected_id = kb.create_task(
            conn,
            title="missing skill",
            assignee="coder",
            skills=["missing-one", "missing-two"],
        )
        loadable_id = kb.create_task(
            conn,
            title="loadable skill",
            assignee="coder",
            skills=["loadable-skill"],
        )

        first = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4242,
            reconcile_orphans=False,
        )
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id) or 4243,
            reconcile_orphans=False,
        )

        rejection_events = [
            event
            for event in kb.list_events(conn, rejected_id)
            if event.kind == "skill_rejected"
        ]
        rejected_task = kb.get_task(conn, rejected_id)

    assert first.rejected_skills == [
        (rejected_id, ["missing-one", "missing-two"])
    ]
    assert second.rejected_skills == first.rejected_skills
    assert spawned == [loadable_id]
    assert len(rejection_events) == 1
    assert rejection_events[0].payload == {
        "assignee": "coder",
        "missing_skills": ["missing-one", "missing-two"],
    }
    assert rejected_task is not None
    assert rejected_task.status == "ready"
    assert rejected_task.claim_lock is None


def test_skill_rejection_event_drives_ready_diagnostic() -> None:
    task = {"id": "t_rejected", "status": "ready", "assignee": "coder"}
    event = {
        "kind": "skill_rejected",
        "created_at": 123,
        "payload": {
            "assignee": "coder",
            "missing_skills": ["missing-one", "missing-two"],
        },
    }

    diagnostics = kd.compute_task_diagnostics(task, [event], [], now=200)

    rejection = next(d for d in diagnostics if d.kind == "skill_rejected")
    assert rejection.severity == "error"
    assert rejection.data == {
        "assignee": "coder",
        "missing_skills": ["missing-one", "missing-two"],
    }
    assert "missing-one" in rejection.detail
    assert "missing-two" in rejection.detail
    assert not any(
        d.kind == "skill_rejected"
        for d in kd.compute_task_diagnostics(
            {**task, "status": "running"}, [event], [], now=200
        )
    )


@contextmanager
def _unused_connection():
    yield object()


@pytest.mark.parametrize("json_output", [True, False])
def test_dispatch_output_names_skill_rejections_and_respawn_guards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_output: bool,
) -> None:
    result = kb.DispatchResult(
        rejected_skills=[("t_rejected", ["missing-one", "missing-two"])],
        respawn_guarded=[("t_guarded", "active_pr")],
    )
    monkeypatch.setattr(kb, "connect_closing", _unused_connection)
    monkeypatch.setattr(kb, "dispatch_once", lambda _conn, **_kwargs: result)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})

    exit_code = kanban_cli._cmd_dispatch(
        argparse.Namespace(dry_run=False, max=1, failure_limit=2, json=json_output)
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    if json_output:
        payload = json.loads(output)
        assert payload["rejected_skills"] == [
            {
                "task_id": "t_rejected",
                "missing_skills": ["missing-one", "missing-two"],
            }
        ]
        assert payload["respawn_guarded"] == [
            {"task_id": "t_guarded", "reason": "active_pr"}
        ]
    else:
        assert "t_rejected" in output
        assert "missing-one, missing-two" in output
        assert "t_guarded" in output
        assert "active_pr" in output
