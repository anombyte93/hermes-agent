from __future__ import annotations

import subprocess


def _make_task(kb):
    return kb.Task(
        id="t_terminal_failure",
        title="terminal failure exits",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=1,
        goal_mode=False,
    )


def test_normal_kanban_worker_uses_fully_quiet_failure_propagation(monkeypatch, tmp_path):
    """A non-goal worker must propagate structured terminal failures to its exit code."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb._default_spawn(_make_task(kb), str(workspace))

    chat_index = captured["cmd"].index("chat")
    assert captured["cmd"][chat_index:] == [
        "chat",
        "-q",
        "work kanban task t_terminal_failure",
        "-Q",
    ]
