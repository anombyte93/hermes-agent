"""Name-only credential scrubbing with synthetic, value-safe environments."""

import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from tools import env_passthrough
from tools.environments import local


_BUILDERS = ("sanitize", "foreground", "factory", "nonterminal")
_SHAPES = (
    "SERVICE_API_KEY",
    "SERVICE_AUTH_KEY",
    "SERVICE_SECRET",
    "SERVICE_SECRET_KEY",
    "SERVICE_TOKEN",
    "SERVICE_ACCESS_TOKEN",
    "SERVICE_REFRESH_TOKEN",
    "SERVICE_PASSWORD",
    "SERVICE_PASSWD",
    "SERVICE_PRIVATE_KEY",
    "API_KEY",
    "TOKEN",
    "PASSWORD",
)
_SENTINEL = "synthetic-value-must-never-appear-in-debug-output"


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch, tmp_path):
    # Do not snapshot an inherited environment, even for assertion diagnostics.
    monkeypatch.setattr(os, "environ", {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    })
    monkeypatch.setattr(env_passthrough, "_config_passthrough", frozenset())
    token = env_passthrough._allowed_env_vars_var.set(set())
    try:
        yield
    finally:
        env_passthrough._allowed_env_vars_var.reset(token)


def _build(builder, base, extra=None):
    if builder == "sanitize":
        return local._sanitize_subprocess_env(base, extra)
    if builder == "factory":
        return local.build_subprocess_env(base, extra=extra)
    with patch.object(os, "environ", dict(base)):
        if builder == "foreground":
            return local._make_run_env(extra or {})
        return local.hermes_subprocess_env()


def _assert_absent(result, names):
    __tracebackhide__ = True
    # Hide helper arguments too: pytest renders them even without --showlocals.
    leaked = sorted(set(result).intersection(names))
    assert not leaked, f"Credential-shaped names leaked: {leaked}"


def _assert_preserved(result, expected):
    __tracebackhide__ = True
    for key, value in expected.items():
        preserved = result.get(key) == value
        assert preserved, f"Explicit or non-secret variable not preserved: {key}"


@pytest.mark.parametrize("builder", _BUILDERS)
def test_incident_names_are_stripped(builder):
    names = ("ABUSEIPDB_API_KEY", "SHODAN_API_KEY", "VIRUSTOTAL_API_KEY", "ABUSECH_AUTH_KEY")
    base = {"HERMES_KANBAN_TASK": "t_synthetic", **dict.fromkeys(names, _SENTINEL)}
    result = _build(builder, base)
    _assert_absent(result, names)
    _assert_preserved(result, {"HERMES_KANBAN_TASK": "t_synthetic"})


@pytest.mark.parametrize("builder", _BUILDERS)
@pytest.mark.parametrize("name", _SHAPES)
@pytest.mark.parametrize("casing", ("upper", "lower", "mixed"))
def test_each_secret_shape_is_stripped_case_insensitively(builder, name, casing):
    if casing == "lower":
        name = name.lower()
    elif casing == "mixed":
        name = name.title()
    result = _build(builder, {name: _SENTINEL, "SAFE_MARKER": "present"})
    _assert_absent(result, (name,))
    _assert_preserved(result, {"SAFE_MARKER": "present"})


@pytest.mark.parametrize("builder", _BUILDERS)
def test_non_secret_names_and_required_runtime_names_survive(builder):
    base = {
        "HERMES_KANBAN_TASK": "t_synthetic",
        "HERMES_HOME": "/synthetic/hermes-home",
        "PATH": os.defpath,
        "HOME": "/synthetic/home",
        "TERM": "xterm",
        "GIT_AUTHOR_NAME": "Synthetic Author",
        "GIT_CONFIG_COUNT": "0",
        "TOKEN_COUNT": "10",
        "PASSWORD_FILE": "/synthetic/password-location",
        "SERVICE_API_KEY_PATH": "/synthetic/key-location",
        "NOTATOKEN": "not-a-secret-name",
        "AUTH_KEY": "bare-name-not-in-the-contract",
        "SECRET": "bare-name-not-in-the-contract",
    }
    result = _build(builder, base)
    # PATH may gain the Hermes console-script dir; it must retain its entries.
    path_entries = result.get("PATH", "").split(os.pathsep)
    assert set(os.defpath.split(os.pathsep)) <= set(path_entries), "PATH entries missing"
    _assert_preserved(result, {key: value for key, value in base.items() if key != "PATH"})


@pytest.mark.parametrize("builder", _BUILDERS)
def test_legacy_credential_exemptions_are_narrow(builder):
    base = {
        "AWS_SESSION_TOKEN": _SENTINEL,
        "CLAUDE_CODE_OAUTH_TOKEN": _SENTINEL,
        "OTHER_SESSION_TOKEN": _SENTINEL,
        "GIT_SERVICE_TOKEN": _SENTINEL,
    }
    result = _build(builder, base)
    _assert_preserved(result, {key: _SENTINEL for key in (
        "AWS_SESSION_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    )})
    _assert_absent(result, ("OTHER_SESSION_TOKEN", "GIT_SERVICE_TOKEN"))


@pytest.mark.parametrize("builder", ("sanitize", "foreground", "factory"))
@pytest.mark.parametrize("source", ("base", "extra"))
def test_registered_passthrough_survives(builder, source):
    env_passthrough.register_env_passthrough(["CUSTOM_AUTH_KEY"])
    base = {"SAFE_MARKER": "present"}
    extra = {"CUSTOM_AUTH_KEY": _SENTINEL}
    if source == "base":
        base.update(extra)
        extra = None
    result = _build(builder, base, extra)
    _assert_preserved(result, {"CUSTOM_AUTH_KEY": _SENTINEL, "SAFE_MARKER": "present"})


@pytest.mark.parametrize("builder", ("sanitize", "foreground", "factory"))
def test_profile_config_passthrough_survives(builder, monkeypatch):
    monkeypatch.setattr(env_passthrough, "_config_passthrough", None)
    monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {
        "terminal": {"env_passthrough": ["CUSTOM_AUTH_KEY"]},
    })
    result = _build(builder, {"CUSTOM_AUTH_KEY": _SENTINEL})
    _assert_preserved(result, {"CUSTOM_AUTH_KEY": _SENTINEL})


@pytest.mark.parametrize("builder", ("sanitize", "foreground", "factory"))
def test_force_prefix_in_extra_overrides_stripped_base(builder):
    prefix = local._HERMES_PROVIDER_ENV_FORCE_PREFIX
    result = _build(builder, {"CUSTOM_AUTH_KEY": "synthetic-inherited"}, {
        f"{prefix}CUSTOM_AUTH_KEY": _SENTINEL,
        "UNAPPROVED_AUTH_KEY": _SENTINEL,
    })
    _assert_preserved(result, {"CUSTOM_AUTH_KEY": _SENTINEL})
    _assert_absent(result, (f"{prefix}CUSTOM_AUTH_KEY", "UNAPPROVED_AUTH_KEY"))


@pytest.mark.parametrize("builder", ("sanitize", "foreground", "factory"))
def test_internal_secrets_remain_blocked_under_passthrough_and_force(builder, monkeypatch):
    monkeypatch.setattr(env_passthrough, "is_env_passthrough", lambda name: True)
    name = "AUXILIARY_SYNTHETIC_API_KEY"
    result = _build(builder, {name: _SENTINEL}, {
        name: _SENTINEL,
        f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}{name}": _SENTINEL,
    })
    _assert_absent(result, (name, f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}{name}"))


@pytest.mark.parametrize("inherit_credentials", (False, True))
def test_nonterminal_unknown_credentials_require_passthrough(inherit_credentials):
    with patch.object(os, "environ", {"CUSTOM_AUTH_KEY": _SENTINEL}):
        result = local.hermes_subprocess_env(inherit_credentials=inherit_credentials)
    _assert_absent(result, ("CUSTOM_AUTH_KEY",))


@pytest.mark.parametrize("builder", _BUILDERS)
def test_inherited_force_prefix_never_restores_a_credential(builder):
    name = "SHODAN_API_KEY"
    force = f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}{name}"
    result = _build(builder, {name: _SENTINEL, force: _SENTINEL})
    _assert_absent(result, (name, force))


@pytest.mark.parametrize("inherit_credentials", (False, True))
@pytest.mark.parametrize("source", ("skill", "config"))
def test_nonterminal_passthrough_uses_existing_opt_ins(inherit_credentials, source, monkeypatch):
    if source == "skill":
        env_passthrough.register_env_passthrough(["CUSTOM_AUTH_KEY"])
    else:
        monkeypatch.setattr(env_passthrough, "_config_passthrough", None)
        monkeypatch.setattr("hermes_cli.config.read_raw_config", lambda: {
            "terminal": {"env_passthrough": ["CUSTOM_AUTH_KEY"]},
        })
    os.environ["CUSTOM_AUTH_KEY"] = _SENTINEL
    result = local.hermes_subprocess_env(inherit_credentials=inherit_credentials)
    _assert_preserved(result, {"CUSTOM_AUTH_KEY": _SENTINEL})


@pytest.mark.parametrize("inherit_credentials", (False, True))
def test_nonterminal_force_is_explicit_only(inherit_credentials):
    name = "SHODAN_API_KEY"
    force = f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}{name}"
    os.environ.update({name: "synthetic-inherited", force: "synthetic-inherited"})
    result = local.hermes_subprocess_env(
        inherit_credentials=inherit_credentials, extra_env={force: _SENTINEL},
    )
    _assert_absent(result, (force,))
    _assert_preserved(result, {name: _SENTINEL})


def test_credential_child_keeps_provider_names_not_tool_or_unknown_secrets(monkeypatch, caplog):
    names = ("ABUSEIPDB_API_KEY", "ABUSECH_AUTH_KEY", "abuseipdb_api_key", "FIRECRAWL_API_KEY")
    os.environ.update(dict.fromkeys(names, _SENTINEL))
    os.environ.update({"OPENAI_API_KEY": _SENTINEL, "COPILOT_GITHUB_TOKEN": _SENTINEL})
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    result = local.hermes_subprocess_env(inherit_credentials=True)
    _assert_absent(result, names)
    _assert_preserved(result, {
        "OPENAI_API_KEY": _SENTINEL, "COPILOT_GITHUB_TOKEN": _SENTINEL,
    })
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1
    assert records[0].args == (len(names), ", ".join(sorted(names)))
    values_absent = _SENTINEL not in caplog.text
    assert values_absent, "A credential value appeared in the debug log"


def test_credential_child_uses_provider_registry_names(monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import auth

    name = "SYNTHETIC_PROVIDER_API_KEY"
    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", {
        "synthetic": SimpleNamespace(
            api_key_env_vars=(name,), base_url_env_var="SYNTHETIC_PROVIDER_URL",
            auth_type="api_key",
        ),
    })
    os.environ[name] = _SENTINEL
    result = local.hermes_subprocess_env(inherit_credentials=True)
    _assert_preserved(result, {name: _SENTINEL})
    _assert_absent(local.hermes_subprocess_env(), (name,))


@pytest.mark.parametrize("routes", (("selected",), ("selected", "second"), (), ("unknown",)))
@pytest.mark.parametrize("inherit_credentials", (False, True))
def test_credential_child_scopes_registry_names(routes, inherit_credentials, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import auth

    registry = {
        route: SimpleNamespace(
            api_key_env_vars=(f"SYNTHETIC_{route.upper()}_API_KEY",),
            base_url_env_var=f"SYNTHETIC_{route.upper()}_URL",
            auth_type="api_key",
        )
        for route in ("selected", "second", "unrelated")
    }
    monkeypatch.setattr(auth, "PROVIDER_REGISTRY", registry)
    provider_names = {
        name for provider in registry.values()
        for name in (*provider.api_key_env_vars, provider.base_url_env_var)
    }
    # The static blocklist normally captures registry endpoints at import time.
    monkeypatch.setattr(local, "_HERMES_PROVIDER_ENV_BLOCKLIST", (
        local._HERMES_PROVIDER_ENV_BLOCKLIST | provider_names
    ))
    os.environ.update(dict.fromkeys(provider_names | {"ANTHROPIC_API_KEY"}, _SENTINEL))
    result = local.hermes_subprocess_env(
        inherit_credentials=inherit_credentials, credential_routes=routes,
    )
    expected = {
        name for route, provider in registry.items() if route in routes and inherit_credentials
        for name in (*provider.api_key_env_vars, provider.base_url_env_var)
    }
    _assert_preserved(result, dict.fromkeys(expected, _SENTINEL))
    _assert_absent(result, (provider_names - expected) | {"ANTHROPIC_API_KEY"})


@pytest.mark.parametrize("inherit_credentials", (False, True))
def test_nonterminal_tier_one_and_internal_secrets_cannot_be_forced(inherit_credentials, monkeypatch):
    names = ("GH_TOKEN", "AUXILIARY_SYNTHETIC_API_KEY", "GATEWAY_RELAY_SECRET")
    monkeypatch.setattr(env_passthrough, "is_env_passthrough", lambda _name: True)
    os.environ.update(dict.fromkeys(names, _SENTINEL))
    result = local.hermes_subprocess_env(
        inherit_credentials=inherit_credentials,
        extra_env={f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}{name}": _SENTINEL for name in names},
    )
    _assert_absent(result, names)


def test_real_credential_child_emits_only_permitted_names():
    names = ("ABUSEIPDB_API_KEY", "ABUSECH_AUTH_KEY", "abusech_auth_key")
    os.environ.update(dict.fromkeys(names, _SENTINEL))
    os.environ["OPENAI_API_KEY"] = _SENTINEL
    os.environ["CUSTOM_AUTH_KEY"] = _SENTINEL
    env_passthrough.register_env_passthrough(["CUSTOM_AUTH_KEY"])
    env = local.hermes_subprocess_env(inherit_credentials=True)
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(*sorted(os.environ), sep=chr(10))"],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    child_names = set(child.stdout.splitlines())
    _assert_absent(child_names, names)
    expected = {"OPENAI_API_KEY", "CUSTOM_AUTH_KEY", "PATH", "HOME", "HERMES_HOME"}
    assert expected <= child_names, f"Missing required names: {sorted(expected - child_names)}"


@pytest.mark.parametrize("builder", _BUILDERS)
def test_debug_names_count_and_no_values_once_across_builders(builder, monkeypatch, caplog):
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False, raising=False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    base = {"Z_CUSTOM_AUTH_KEY": _SENTINEL, "A_CUSTOM_TOKEN": _SENTINEL}
    _build(builder, base)
    for sibling in _BUILDERS:
        _build(sibling, base)
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1, "Expected one names-only scrub diagnostic per process"
    assert records[0].levelno == logging.DEBUG
    assert records[0].args == (2, "A_CUSTOM_TOKEN, Z_CUSTOM_AUTH_KEY")
    values_absent = _SENTINEL not in caplog.text
    assert values_absent, "A credential value appeared in the debug log"


def test_empty_call_does_not_consume_debug_log(monkeypatch, caplog):
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False, raising=False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    local._sanitize_subprocess_env({"SAFE_MARKER": "present"})
    local._sanitize_subprocess_env({"CUSTOM_AUTH_KEY": _SENTINEL})
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1
    assert records[0].args == (1, "CUSTOM_AUTH_KEY")


def test_debug_log_excludes_names_restored_by_force(monkeypatch, caplog):
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False, raising=False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    result = local._sanitize_subprocess_env({"RESTORED_AUTH_KEY": _SENTINEL}, {
        f"{local._HERMES_PROVIDER_ENV_FORCE_PREFIX}RESTORED_AUTH_KEY": _SENTINEL,
        "DROPPED_AUTH_KEY": _SENTINEL,
    })
    _assert_preserved(result, {"RESTORED_AUTH_KEY": _SENTINEL})
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1
    assert records[0].args == (1, "DROPPED_AUTH_KEY")


def test_concurrent_sanitizers_log_only_once(monkeypatch, caplog):
    monkeypatch.setattr(local, "_subprocess_env_strip_logged", False, raising=False)
    caplog.set_level(logging.DEBUG, logger=local.__name__)
    base = {"CUSTOM_AUTH_KEY": _SENTINEL}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(local._sanitize_subprocess_env, [base] * 16))
    for result in results:
        _assert_absent(result, base)
    records = [record for record in caplog.records if record.name == local.__name__]
    assert len(records) == 1


def test_real_child_gets_safe_keys_and_explicit_passthrough(tmp_path):
    env_passthrough.register_env_passthrough(["CUSTOM_AUTH_KEY"])
    base = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "HERMES_HOME": str(tmp_path / "hermes-home"),
        "HERMES_KANBAN_TASK": "t_synthetic",
        "ABUSEIPDB_API_KEY": _SENTINEL,
        "CUSTOM_AUTH_KEY": _SENTINEL,
    }
    env = local.build_subprocess_env(base)
    # The real child emits key NAMES only, even when the scrubber is broken.
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(*sorted(os.environ), sep=chr(10))"],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    names = set(child.stdout.splitlines())
    _assert_absent(names, ("ABUSEIPDB_API_KEY",))
    expected = {"CUSTOM_AUTH_KEY", "HERMES_KANBAN_TASK", "PATH", "HOME", "HERMES_HOME"}
    assert expected <= names, f"Missing required names: {sorted(expected - names)}"
