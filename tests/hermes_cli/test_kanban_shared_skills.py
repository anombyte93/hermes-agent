from __future__ import annotations

from pathlib import Path

import pytest


def _write_skill(root: Path, name: str, marker: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: shared test skill\n---\n\n{marker}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("profile", ["coder", "evo", "generalist"])
def test_preload_resolves_canonical_shared_skill_for_every_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    home = tmp_path / ".hermes"
    profile_home = home / "profiles" / profile
    (profile_home / "skills").mkdir(parents=True)
    shared = tmp_path / ".agents" / "skills"
    _write_skill(shared, "atlas-issue", "CANONICAL ATLAS ISSUE")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from agent.skill_commands import build_preloaded_skills_prompt
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(profile_home)
    try:
        prompt, loaded, missing = build_preloaded_skills_prompt(["atlas-issue"])
    finally:
        reset_hermes_home_override(token)

    assert loaded == ["atlas-issue"]
    assert missing == []
    assert "CANONICAL ATLAS ISSUE" in prompt
    assert not (profile_home / "skills" / "atlas-issue").exists()


def test_unknown_task_skill_is_rejected_before_dispatch_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    calls = {"claim": 0, "workspace": 0, "spawn": 0}
    real_claim = kb.claim_task

    def tracked_claim(*args, **kwargs):
        calls["claim"] += 1
        return real_claim(*args, **kwargs)

    def tracked_workspace(*args, **kwargs):
        calls["workspace"] += 1
        return tmp_path / "workspace"

    def tracked_spawn(*args, **kwargs):
        calls["spawn"] += 1
        return 1234

    monkeypatch.setattr(kb, "claim_task", tracked_claim)
    monkeypatch.setattr(kb, "resolve_workspace", tracked_workspace)

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="unknown skill",
            assignee="coder",
            skills=["fabricated-skill-that-does-not-exist"],
        )
        result = kb.dispatch_once(conn, spawn_fn=tracked_spawn, reconcile_orphans=False)
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()

    assert result.rejected_skills == [
        (task_id, ["fabricated-skill-that-does-not-exist"])
    ]
    assert calls == {"claim": 0, "workspace": 0, "spawn": 0}
    assert task is not None and task.status == "ready" and task.claim_lock is None
