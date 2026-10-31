"""Exercise credential-child callers without launching any provider binary."""

import os
from types import SimpleNamespace

import pytest

from tools import env_passthrough


_SENTINEL = "synthetic-child-secret-not-for-diagnostics"
_BAD_NAMES = ("ABUSEIPDB_API_KEY", "ABUSECH_AUTH_KEY", "abuseipdb_api_key")
_OPENAI_COMPAT_NAMES = frozenset({
    "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_API_BASE", "CODEX_API_KEY",
})


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {
        "PATH": os.defpath, "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "OPENAI_API_KEY": _SENTINEL,
        "OPENAI_BASE_URL": "https://synthetic-openai.invalid/v1",
        "ANTHROPIC_API_KEY": _SENTINEL,
        "ANTHROPIC_BASE_URL": "https://synthetic-anthropic.invalid",
        "COPILOT_GITHUB_TOKEN": _SENTINEL,
        "COPILOT_API_BASE_URL": "https://synthetic-copilot.invalid",
        "COPILOT_ACP_BASE_URL": "acp://synthetic-copilot",
        "GH_TOKEN": _SENTINEL,
        "GITHUB_TOKEN": _SENTINEL,
        "_HERMES_FORCE_SHODAN_API_KEY": _SENTINEL,
        **dict.fromkeys(_BAD_NAMES, _SENTINEL),
        **dict.fromkeys(_OPENAI_COMPAT_NAMES, _SENTINEL),
    })
    monkeypatch.setattr(env_passthrough, "_config_passthrough", {})
    token = env_passthrough._allowed_env_vars_var.set(set())
    try:
        yield
    finally:
        env_passthrough._allowed_env_vars_var.reset(token)


def _assert_safe(env, *, routes, explicit_force=False):
    __tracebackhide__ = True
    from hermes_cli.auth import PROVIDER_REGISTRY

    allowed = set()
    providers = (
        PROVIDER_REGISTRY.values() if routes is None else
        (PROVIDER_REGISTRY[route] for route in routes)
    )
    for provider in providers:
        allowed.update(provider.api_key_env_vars)
        if provider.base_url_env_var:
            allowed.add(provider.base_url_env_var)
    if routes is None:
        # Route-agnostic children keep the existing legacy names, not CODEX_API_KEY.
        allowed.update(_OPENAI_COMPAT_NAMES - {"CODEX_API_KEY"})
    elif {"openai-codex", "openai-api"}.intersection(routes):
        allowed.update(_OPENAI_COMPAT_NAMES)
    # Generic GitHub auth remains Tier-1 even for Copilot.
    allowed.difference_update({"GH_TOKEN", "GITHUB_TOKEN"})
    provider_names = {
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
        "COPILOT_GITHUB_TOKEN", "COPILOT_API_BASE_URL", "COPILOT_ACP_BASE_URL",
    } | _OPENAI_COMPAT_NAMES
    blocked = set(_BAD_NAMES) | {
        "_HERMES_FORCE_SHODAN_API_KEY", "EXTRA_AUTH_KEY", "GH_TOKEN", "GITHUB_TOKEN",
    } | (provider_names - allowed)
    leaked = blocked & set(env)
    assert not leaked, f"Credential-shaped names leaked: {sorted(leaked)}"
    for name in allowed & set(os.environ):
        preserved = env.get(name) == os.environ[name]
        assert preserved, f"Provider name not preserved: {name}"
    assert ("SHODAN_API_KEY" in env) == explicit_force


@pytest.mark.parametrize("routes", (
    ("openai-codex",), ("openai-api",), ("copilot", "copilot-acp"), ("anthropic",), (),
))
@pytest.mark.parametrize("source", ("inherited", "extra"))
def test_openai_compatibility_names_are_route_scoped(routes, source):
    from tools.environments.local import _credential_child_env_names, hermes_subprocess_env

    extra = None
    if source == "extra":
        extra = {name: os.environ.pop(name) for name in _OPENAI_COMPAT_NAMES}
    result = hermes_subprocess_env(
        inherit_credentials=True, credential_routes=routes, extra_env=extra,
    )
    _assert_safe(result, routes=routes)
    if {"openai-codex", "openai-api"}.intersection(routes):
        for name in _OPENAI_COMPAT_NAMES:
            preserved = result.get(name) == _SENTINEL
            assert preserved, f"OpenAI compatibility name not preserved: {name}"
    elif not routes:
        assert _credential_child_env_names(()) == frozenset()


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
    _assert_safe(captured, routes=("openai-codex", "openai-api"), explicit_force=explicit_force)


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
    monkeypatch.setattr(hs.threading, "Thread", lambda **_kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(supervisor, "_persist_registry", lambda: None)
    supervisor._spawn_locked(reason="synthetic-test")
    _assert_safe(captured, routes=None, explicit_force=explicit_force)


def test_copilot_route_keeps_dedicated_provider_token():
    from agent.copilot_acp_client import _build_subprocess_env

    _assert_safe(_build_subprocess_env(), routes=("copilot", "copilot-acp"))


def test_copilot_capability_probe_uses_the_same_environment(monkeypatch):
    from agent import copilot_acp_client as acp

    captured = {}

    def capture_probe(_cmd, **kwargs):
        captured.update(kwargs.get("env", {}))
        return SimpleNamespace(returncode=0, stdout="--acp", stderr="")

    monkeypatch.setattr(acp.subprocess, "run", capture_probe)
    monkeypatch.setattr(acp, "_ACP_PROBE_CACHE", {})
    assert acp._acp_supported("synthetic-copilot", ["--acp"]) is True
    _assert_safe(captured, routes=("copilot", "copilot-acp"))
