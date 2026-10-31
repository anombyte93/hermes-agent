"""Synthetic, names-only coverage for the real Kanban worker spawn boundary."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import env_passthrough
from tools.environments import local


_SENTINEL = "synthetic-value-must-never-appear-in-worker-debug-output"


@pytest.fixture(autouse=True)
def isolated_systemd_bus(monkeypatch, tmp_path):
    # The release spawn path normalises a real lingering user's bus. Keep that
    # host-dependent fallback inside the synthetic fixture instead.
    monkeypatch.setattr(
        "tools.process_registry._default_user_runtime_dir", lambda: tmp_path / "runtime",
    )


@pytest.mark.parametrize("passthrough", (False, True))
def test_worker_spawn_scrubs_unknown_credentials(monkeypatch, tmp_path, caplog, passthrough):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    root = tmp_path / "hermes"
    root.mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    names = ("ABUSEIPDB_API_KEY", "ABUSECH_AUTH_KEY", "abusech_auth_key")
    force = "_HERMES_FORCE_SHODAN_API_KEY"
    monkeypatch.setattr(os, "environ", {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(root),
        "HERMES_INFERENCE_PROVIDER": "openai-api",
        "HERMES_RUNTIME_MARKER": "synthetic",
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/bus",
        "OPENAI_API_KEY": _SENTINEL,
        "CUSTOM_AUTH_KEY": _SENTINEL,
        force: _SENTINEL,
        **dict.fromkeys(names, _SENTINEL),
    })
    monkeypatch.setattr(env_passthrough, "_config_passthrough", {})
    token = env_passthrough._allowed_env_vars_var.set(set())
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False, raising=False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    captured_names = set()
    preserved = {}

    def capture_spawn(_cmd, **kwargs):
        __tracebackhide__ = True
        env = kwargs["env"]
        captured_names.update(env)
        for name, value in {
            "OPENAI_API_KEY": _SENTINEL,
            "CUSTOM_AUTH_KEY": _SENTINEL,
            "HOME": str(tmp_path),
            "HERMES_KANBAN_TASK": "t_synthetic_env",
            "HERMES_RUNTIME_MARKER": "synthetic",
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/bus",
        }.items():
            preserved[name] = env.get(name) == value
        preserved["PATH"] = set(os.defpath.split(os.pathsep)) <= set(env["PATH"].split(os.pathsep))
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(subprocess, "Popen", capture_spawn)
    task = kb.Task(
        id="t_synthetic_env", title="synthetic spawn", body=None,
        assignee="synthetic-worker", status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=str(workspace), claim_lock=None,
        claim_expires=None, tenant=None,
    )
    try:
        if passthrough:
            env_passthrough.register_env_passthrough(["CUSTOM_AUTH_KEY"])
        pid = kbd._default_spawn(task, str(workspace))
    finally:
        env_passthrough._allowed_env_vars_var.reset(token)

    assert pid == 4242
    leaked = set(names + (force, "SHODAN_API_KEY")) & captured_names
    assert not leaked, f"Credential-shaped names leaked: {sorted(leaked)}"
    for name in preserved.keys() - {"CUSTOM_AUTH_KEY"}:
        assert preserved[name], f"Required name not preserved: {name}"
    assert ("CUSTOM_AUTH_KEY" in captured_names) == passthrough
    if passthrough:
        assert preserved["CUSTOM_AUTH_KEY"]
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1, "Expected one names-only scrub diagnostic"
    stripped: set[str] = set(names + (force,))
    if not passthrough:
        stripped.add("CUSTOM_AUTH_KEY")
    assert records[0].args == (len(stripped), ", ".join(sorted(stripped)))
    values_absent = _SENTINEL not in caplog.text
    assert values_absent, "A credential value appeared in the debug log"


@pytest.fixture(params=("profile", "auto"))
def worker_runtime_environment(monkeypatch, tmp_path, request):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles
    import hermes_constants

    root = tmp_path / "dispatcher"
    root.mkdir()
    (root / "home").mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo_root = str(Path(local.__file__).resolve().parents[2])
    base = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(root),
        "TERMINAL_HOME_MODE": request.param,
        "VIRTUAL_ENV": str(tmp_path / "worker-venv"),
        "CONDA_PREFIX": str(tmp_path / "worker-conda"),
        "PYTHONHOME": str(tmp_path / "worker-python"),
        "PYTHONPATH": os.pathsep.join((str(tmp_path / "user-python"), repo_root, "")),
        "PYTHONUTF8": "0",
        "HERMES_REAL_HOME": str(tmp_path / "real-home"),
        "MSYS_NO_PATHCONV": "synthetic",
        "HERMES_RUNTIME_MARKER": "synthetic",
        "OPENAI_API_KEY": _SENTINEL,
        "ANTHROPIC_API_KEY": _SENTINEL,
        "COPILOT_GITHUB_TOKEN": _SENTINEL,
        "ABUSECH_AUTH_KEY": _SENTINEL,
    }
    monkeypatch.setattr(os, "environ", base)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: True)
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _name: str(tmp_path / "worker"))
    monkeypatch.setattr(env_passthrough, "_config_passthrough", {})
    token = env_passthrough._allowed_env_vars_var.set(set())
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: [sys.executable, "-m", "hermes_cli.main"])
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    captured = {}

    def capture_spawn(_cmd, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(pid=4242)

    task = kb.Task(
        id="t_synthetic_runtime", title="synthetic runtime", body=None,
        assignee="synthetic-worker", status="running", priority=0,
        created_by="test", created_at=1, started_at=None, completed_at=None,
        workspace_kind="dir", workspace_path=str(workspace), claim_lock=None,
        claim_expires=None, tenant=None,
    )
    try:
        with monkeypatch.context() as spawn_patch:
            spawn_patch.setattr(subprocess, "Popen", capture_spawn)
            assert kbd._default_spawn(task, str(workspace)) == 4242
        yield SimpleNamespace(
            inherited=lambda name: captured.get(name) == base[name],
            names=frozenset(captured),
        )
    finally:
        env_passthrough._allowed_env_vars_var.reset(token)


def _assert_inherited(snapshot, names):
    __tracebackhide__ = True
    for name in names:
        assert snapshot.inherited(name), f"Required name not preserved: {name}"


def test_worker_spawn_keeps_parent_home(worker_runtime_environment):
    _assert_inherited(worker_runtime_environment, ("HOME", "HERMES_REAL_HOME"))


def test_worker_spawn_keeps_python_environment(worker_runtime_environment):
    _assert_inherited(worker_runtime_environment, (
        "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH",
    ))


def test_worker_spawn_keeps_other_nonsecret_values(worker_runtime_environment):
    _assert_inherited(worker_runtime_environment, (
        "PATH", "TERMINAL_HOME_MODE", "PYTHONUTF8", "MSYS_NO_PATHCONV", "HERMES_RUNTIME_MARKER",
    ))


def test_worker_spawn_keeps_all_provider_routes(worker_runtime_environment):
    _assert_inherited(worker_runtime_environment, (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "COPILOT_GITHUB_TOKEN",
    ))
    assert "ABUSECH_AUTH_KEY" not in worker_runtime_environment.names
