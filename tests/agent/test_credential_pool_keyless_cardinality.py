"""Keyless rows block inheritance without changing selectable-key cooldowns."""
from __future__ import annotations

import json
import os
import socket
from argparse import Namespace
from pathlib import Path

import pytest

from agent import anthropic_credentials as ac
from agent import credential_pool as cp
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_cli.auth_commands import auth_add_command, auth_reset_command


_ENV_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)
NOW = 1_800_000_000.0


@pytest.fixture
def contaminated_parent_env(monkeypatch):
    # Positive precondition: prove the fixture clears even an inherited key.
    for name in _ENV_NAMES:
        monkeypatch.setenv(name, "synthetic-parent-value")


@pytest.fixture
def fleet(tmp_path, monkeypatch, contaminated_parent_env):
    for name in _ENV_NAMES:
        assert os.environ[name] == "synthetic-parent-value"
        monkeypatch.delenv(name)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    # macOS must not discover the operator's Keychain. File I/O stays real.
    monkeypatch.setattr(ac, "_read_claude_code_credentials_from_keychain", lambda: None)

    def no_network(*args, **kwargs):
        pytest.fail("credential cardinality tests must not contact providers")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    clock = [NOW]
    monkeypatch.setattr(cp.time, "time", lambda: clock[0])
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    write_credential_pool("anthropic", [_row("root-key", token="synthetic-root-key")])

    def use(name):
        home = root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        return home

    return use, clock


def _row(ident, *, token="synthetic-own-key", **updates):
    return {
        "id": ident, "label": ident, "source": "manual", "auth_type": "api_key",
        "access_token": token, "priority": 0, **updates,
    }


def _placeholder(auth_type="api_key", **updates):
    return _row(
        "relay-excluded", token="", source="manual:atlas-relay-excluded",
        auth_type=auth_type, last_status="dead", **updates,
    )


def _snapshot(pool):
    selected = pool.select()
    return {
        "available": pool.has_available(),
        "selected": selected.label if selected else None,
        "next_at": pool.next_available_at(),
    }


@pytest.mark.parametrize("reset_first", [True, False], ids=["reset-add", "add-reset"])
@pytest.mark.parametrize("target", [None, "relay-excluded"], ids=["reset-all", "reset-one"])
@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
def test_keyless_reset_preserves_own_key_cooldown(fleet, reset_first, target, auth_type):
    """Real auth add/reset and persisted reloads, with and without a marker."""
    use, clock = fleet
    observations = []
    for blocked in (False, True):
        clock[0] = NOW
        use("blocked" if blocked else "control")
        write_credential_pool("anthropic", [_placeholder(auth_type)] if blocked else [])

        def reset():
            if blocked or target is None:
                auth_reset_command(Namespace(provider="anthropic", target=target))

        if reset_first:
            reset()
        auth_add_command(Namespace(
            provider="anthropic", auth_type="api-key", api_key="synthetic-own-key",
            label="own", priority=None,
        ))
        if not reset_first:
            reset()
        if blocked:
            marker = next(row for row in read_credential_pool()["anthropic"] if row["id"] == "relay-excluded")
            assert marker["last_status"] is None
            assert not marker["access_token"]
        assert not any(os.environ.get(name) for name in _ENV_NAMES)
        pool = cp.load_pool("anthropic")
        own = pool.select()
        assert own is not None and own.label == "own"
        assert pool.mark_exhausted_and_rotate(status_code=429, credential_id=own.id) is None
        throttled = _snapshot(cp.load_pool("anthropic"))
        clock[0] += 120
        recovered = _snapshot(cp.load_pool("anthropic"))
        observations.append((throttled, recovered))

    assert observations[0] == (
        {"available": False, "selected": None, "next_at": NOW + cp.EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS},
        {"available": True, "selected": "own", "next_at": None},
    )
    assert observations[1] == observations[0]


@pytest.mark.parametrize("initial", ["missing", "empty"])
@pytest.mark.parametrize("target", ["untouched", None, "relay-excluded"], ids=["dead", "reset-all", "reset-one"])
def test_placeholder_still_blocks_root_inheritance(fleet, initial, target):
    use, clock = fleet
    home = use("blocked")
    if initial == "empty":
        write_credential_pool("anthropic", [])
    else:
        assert not (home / "auth.json").exists()
    inherited = cp.load_pool("anthropic").select()
    assert inherited is not None and inherited.id == "root-key"
    write_credential_pool("anthropic", [_placeholder()])
    if target != "untouched":
        auth_reset_command(Namespace(provider="anthropic", target=target))
    for age in (0, 3 * 24 * 60 * 60):
        clock[0] = NOW + age
        pool = cp.load_pool("anthropic")
        assert [entry.id for entry in pool.entries()] == ["relay-excluded"]
        assert _snapshot(pool) == {"available": False, "selected": None, "next_at": None}
        assert [row["id"] for row in read_credential_pool()["anthropic"]] == ["relay-excluded"]


@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
@pytest.mark.parametrize("reset_delay", [10, 7200], ids=["earlier", "later"])
def test_keyless_stale_reset_does_not_supply_recovery_time(fleet, auth_type, reset_delay):
    use, clock = fleet
    use("stale-marker")
    marker = _row(
        "relay-excluded", token="", auth_type=auth_type,
        source="manual:atlas-relay-excluded", last_status="exhausted",
        last_status_at=NOW, last_error_code=429, last_error_reset_at=NOW + reset_delay,
    )
    write_credential_pool("anthropic", [marker])
    assert cp.load_pool("anthropic").next_available_at() is None
    own = _row("own", last_status="exhausted", last_status_at=NOW, last_error_code=429)
    write_credential_pool("anthropic", [marker, own])
    pool = cp.load_pool("anthropic")
    assert not pool.has_available()
    assert pool.next_available_at() == NOW + cp.EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS
    # Timing probes must not delete the marker or clear its stale metadata.
    saved = next(row for row in read_credential_pool()["anthropic"] if row["id"] == marker["id"])
    assert saved["last_status"] == "exhausted"
    assert saved["last_error_reset_at"] == marker["last_error_reset_at"]
    clock[0] += 120
    selected = cp.load_pool("anthropic").select()
    assert selected is not None and selected.id == "own"


@pytest.mark.parametrize("kind", ["exhausted-key", "oauth-access", "dead-key", "refresh-only", "refresh-not-due"])
def test_real_rotation_candidates_preserve_multi_key_ttl(fleet, monkeypatch, kind):
    use, clock = fleet
    use("candidates")
    own = _row("own", priority=1, last_status="exhausted", last_status_at=NOW, last_error_code=429)
    second = _row("second", token="synthetic-second-key")
    counted = kind in {"exhausted-key", "oauth-access", "refresh-only"}
    if kind == "exhausted-key":
        second.update(last_status="exhausted", last_status_at=NOW, last_error_code=429)
    elif kind == "dead-key":
        second.update(last_status="dead", last_status_at=NOW)
    else:
        second.update(auth_type="oauth", expires_at_ms=int((NOW + 3600) * 1000))
        if kind.startswith("refresh"):
            second.update(access_token="", refresh_token="synthetic-refresh")
            if kind == "refresh-only":
                second["expires_at_ms"] = int((NOW - 1) * 1000)
        else:
            second.update(last_status="exhausted", last_status_at=NOW, last_error_code=429)

    refresh_calls = []

    def refresh(token, **kwargs):
        refresh_calls.append(token)
        return {
            "access_token": "synthetic-refreshed", "refresh_token": "synthetic-rotated",
            "expires_at_ms": int((NOW + 3600) * 1000),
        }

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", refresh)
    write_credential_pool("anthropic", [second, own])
    pool = cp.load_pool("anthropic")
    ttl = cp.EXHAUSTED_TTL_429_SECONDS if counted else cp.EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS
    assert not pool.has_available()
    assert pool.next_available_at() == NOW + ttl
    clock[0] += 120
    available, _ = pool._available_entries()
    assert ("own" in [entry.id for entry in available]) is not counted
    selected = pool.select()
    if kind == "refresh-only":
        # Selection refreshes before its empty-access-token guard, so this is
        # a real candidate even though the non-refreshing probe rejected it.
        assert selected is not None and selected.id == "second"
        assert selected.access_token == "synthetic-refreshed"
        assert refresh_calls == ["synthetic-refresh"]
    else:
        assert (selected.id if selected else None) == (None if counted else "own")
        assert refresh_calls == []


@pytest.mark.parametrize("source", ["claude_code", "hermes_pkce"])
def test_singleton_hydration_counts_the_selected_oauth_row(fleet, tmp_path, source):
    use, clock = fleet
    home = use("hydrated")
    second = _row("second", token="", auth_type="oauth", source=source)
    own = _row("own", priority=1, last_status="exhausted", last_status_at=NOW, last_error_code=429)
    write_credential_pool("anthropic", [second, own])
    creds = {
        "accessToken": "synthetic-singleton-access", "refreshToken": "synthetic-singleton-refresh",
        "expiresAt": int((NOW + 3600) * 1000),
    }
    if source == "claude_code":
        path = tmp_path / ".claude" / ".credentials.json"
        payload = {"claudeAiOauth": creds}
    else:
        path = home / ".anthropic_oauth.json"
        payload = creds
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    pool = cp.load_pool("anthropic")
    hydrated = next(entry for entry in pool.entries() if entry.id == "second")
    assert hydrated.access_token == creds["accessToken"]
    assert pool.has_available()
    assert pool.next_available_at() is None
    clock[0] += 120
    selected = pool.select()
    assert selected is not None and selected.id == "second"
    assert selected.runtime_api_key == creds["accessToken"]
    assert next(entry for entry in pool.entries() if entry.id == "own").last_status == "exhausted"


@pytest.mark.parametrize("status,reason,reset_delay", [
    (402, None, None), (403, "billing", None), (429, None, 7200),
])
@pytest.mark.parametrize("blocked", [False, True])
def test_billing_and_provider_reset_remain_authoritative(fleet, status, reason, reset_delay, blocked):
    use, clock = fleet
    use("authoritative")
    own = _row("own")
    write_credential_pool("anthropic", [own, _placeholder()] if blocked else [own])
    pool = cp.load_pool("anthropic")
    selected = pool.select()
    assert selected is not None and selected.id == "own"
    reset_at = NOW + reset_delay if reset_delay else None
    assert pool.mark_exhausted_and_rotate(
        status_code=status, credential_id="own", failure_reason=reason,
        error_context={"reset_at": reset_at} if reset_at else None,
    ) is None
    expected = reset_at or NOW + cp.EXHAUSTED_TTL_DEFAULT_SECONDS
    clock[0] += 120
    pool = cp.load_pool("anthropic")
    assert _snapshot(pool) == {"available": False, "selected": None, "next_at": expected}
