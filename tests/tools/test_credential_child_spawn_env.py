"""Exercise credential-child callers without launching any provider binary."""

import os
from types import SimpleNamespace

import pytest

from tools import env_passthrough


_SENTINEL = "synthetic-child-secret-not-for-diagnostics"
_BAD_NAMES = ("ABUSEIPDB_API_KEY", "ABUSECH_AUTH_KEY", "abuseipdb_api_key")


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {
        "PATH": os.defpath, "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "OPENAI_API_KEY": _SENTINEL,
        "COPILOT_GITHUB_TOKEN": _SENTINEL,
        "_HERMES_FORCE_SHODAN_API_KEY": _SENTINEL,
        **dict.fromkeys(_BAD_NAMES, _SENTINEL),
    })
    monkeypatch.setattr(env_passthrough, "_config_passthrough", frozenset())
    token = env_passthrough._allowed_env_vars_var.set(set())
    try:
        yield
    finally:
        env_passthrough._allowed_env_vars_var.reset(token)


def _assert_safe(env, *, explicit_force=False):
    __tracebackhide__ = True
    leaked = set(_BAD_NAMES + ("_HERMES_FORCE_SHODAN_API_KEY", "EXTRA_AUTH_KEY")) & set(env)
    assert not leaked, f"Credential-shaped names leaked: {sorted(leaked)}"
    for name in ("OPENAI_API_KEY", "COPILOT_GITHUB_TOKEN"):
        preserved = env.get(name) == _SENTINEL
        assert preserved, f"Provider name not preserved: {name}"
    assert ("SHODAN_API_KEY" in env) == explicit_force


@pytest.mark.parametrize("explicit_force", (False, True))
def test_codex_app_server_filters_explicit_overlay(monkeypatch, explicit_force):
    from agent.transports import codex_app_server as cas

    captured = {}

    def capture_spawn(_cmd, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(stdin=None, stdout=None, stderr=None)

    monkeypatch.setattr(cas.subprocess, "Popen", capture_spawn)
    monkeypatch.setattr(cas.threading, "Thread", lambda **_kw: SimpleNamespace(start=lambda: None))
    extra = {"EXTRA_AUTH_KEY": _SENTINEL, "ABUSECH_AUTH_KEY": _SENTINEL}
    if explicit_force:
        extra["_HERMES_FORCE_SHODAN_API_KEY"] = _SENTINEL
    client = cas.CodexAppServerClient(env=extra)
    client._closed = True
    _assert_safe(captured, explicit_force=explicit_force)


@pytest.mark.parametrize("explicit_force", (False, True))
def test_tui_supervisor_does_not_restore_parent_environment(monkeypatch, tmp_path, explicit_force):
    from tui_gateway import host_supervisor as hs

    extra = {"EXTRA_AUTH_KEY": _SENTINEL, "ABUSECH_AUTH_KEY": _SENTINEL}
    if explicit_force:
        extra["_HERMES_FORCE_SHODAN_API_KEY"] = _SENTINEL
    monkeypatch.setattr(hs, "_build_sha", lambda: "synthetic-build")
    supervisor = hs.HostSupervisor(
        registry_path=tmp_path / "registry.json", argv=["synthetic-compute-host"],
        env=extra, autostart=False,
    )
    captured = {}

    def capture_spawn(_cmd, **kwargs):
        captured.update(kwargs["env"])
        supervisor._hello = {
            "hermes_home": supervisor.expected_hermes_home,
            "build_sha": supervisor.expected_build_sha,
        }
        supervisor._hello_event.set()
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(hs.subprocess, "Popen", capture_spawn)
    monkeypatch.setattr(hs, "_Thread", lambda **_kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(supervisor, "_persist_registry", lambda: None)
    supervisor._spawn_locked(reason="synthetic-test")
    _assert_safe(captured, explicit_force=explicit_force)


def test_copilot_route_keeps_dedicated_provider_token():
    from agent.copilot_acp_client import _build_subprocess_env

    _assert_safe(_build_subprocess_env())


def test_copilot_capability_probe_uses_the_same_environment(monkeypatch):
    from agent import copilot_acp_client as acp

    captured = {}

    def capture_probe(_cmd, **kwargs):
        captured.update(kwargs.get("env", {}))
        return SimpleNamespace(returncode=0, stdout="--acp", stderr="")

    monkeypatch.setattr(acp.subprocess, "run", capture_probe)
    monkeypatch.setattr(acp, "_ACP_PROBE_CACHE", {})
    assert acp._acp_supported("synthetic-copilot", ["--acp"]) is True
    _assert_safe(captured)
