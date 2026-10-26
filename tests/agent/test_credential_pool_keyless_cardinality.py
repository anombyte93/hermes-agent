"""Keyless pool rows must block inheritance without changing usable-key cooldowns."""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from agent import credential_pool as cp
from hermes_cli.auth import read_credential_pool, write_credential_pool
from hermes_cli.auth_commands import auth_add_command, auth_reset_command


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    # Source discovery and root fallback must stay inside this synthetic estate.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    write_credential_pool("anthropic", [{
        "id": "root-key", "label": "root", "source": "manual",
        "auth_type": "api_key", "access_token": "synthetic-root-key", "priority": 0,
    }])

    def use(name):
        home = root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        return home

    return use


def _placeholder(auth_type="api_key"):
    return {
        "id": "relay-excluded", "label": "inheritance blocked",
        "source": "manual:atlas-relay-excluded", "auth_type": auth_type,
        "access_token": "", "priority": 0, "last_status": "dead",
    }


def _snapshot(pool, model):
    selected = pool.select(model=model)
    return {
        "available": pool.has_available(model=model),
        "selected": selected.label if selected else None,
        "next_at": pool.next_available_at(model=model),
        "limits": pool.limit_state([model or "claude-test"]),
    }


@pytest.mark.parametrize("reset_first", [True, False], ids=["reset-add", "add-reset"])
@pytest.mark.parametrize("target", [None, "relay-excluded"], ids=["reset-all", "reset-one"])
@pytest.mark.parametrize("model", [None, "claude-test"], ids=["account-429", "model-429"])
@pytest.mark.parametrize("auth_type", ["api_key", "oauth"])
def test_keyless_reset_preserves_own_key_cooldown(fleet, monkeypatch, reset_first, target, model, auth_type):
    """Real auth add/reset and persisted pool reloads, with and without a marker."""
    now = 1_800_000_000.0
    clock = [now]
    monkeypatch.setattr(cp.time, "time", lambda: clock[0])
    observations = []
    for blocked in (False, True):
        clock[0] = now
        fleet("blocked" if blocked else "control")
        write_credential_pool("anthropic", [_placeholder(auth_type)] if blocked else [])

        def reset():
            # A targeted marker reset has no target in the control profile.
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

        pool = cp.load_pool("anthropic")
        own = pool.select(model=model)
        assert own is not None and own.label == "own"
        assert pool.mark_exhausted_and_rotate(
            status_code=429, credential_id=own.id, model=model,
        ) is None
        # Exercise cooldown persistence, not just the in-memory view.
        pool = cp.load_pool("anthropic")
        throttled = _snapshot(pool, model)
        clock[0] += 120
        recovered = _snapshot(cp.load_pool("anthropic"), model)
        observations.append((throttled, recovered))

    throttled, recovered = observations[0]
    assert throttled["available"] is False and throttled["selected"] is None
    assert throttled["next_at"] == now + cp.EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS
    assert throttled["limits"] == (
        {"scope": "models", "models": {model: throttled["next_at"]}} if model
        else {"scope": "account", "resets_at": throttled["next_at"]}
    )
    assert recovered == {"available": True, "selected": "own", "next_at": None, "limits": None}
    assert observations[1] == observations[0]


@pytest.mark.parametrize("initial", ["missing", "empty"])
@pytest.mark.parametrize("target", ["untouched", None, "relay-excluded"], ids=["dead", "reset-all", "reset-one"])
def test_placeholder_still_blocks_root_inheritance(fleet, monkeypatch, initial, target):
    home = fleet("blocked")
    if initial == "empty":
        write_credential_pool("anthropic", [])
    else:
        assert not (home / "auth.json").exists()
    # Positive control: without the local row Hermes really can inherit the root key.
    inherited = cp.load_pool("anthropic").select()
    assert inherited is not None and inherited.id == "root-key"

    write_credential_pool("anthropic", [_placeholder()])
    if target != "untouched":
        auth_reset_command(Namespace(provider="anthropic", target=target))
    clock = [1_800_000_000.0]
    monkeypatch.setattr(cp.time, "time", lambda: clock[0])
    for age in (0, 3 * 24 * 60 * 60):
        clock[0] = 1_800_000_000.0 + age
        pool = cp.load_pool("anthropic")
        assert [entry.id for entry in pool.entries()] == ["relay-excluded"]
        assert _snapshot(pool, None) == {
            "available": False, "selected": None, "next_at": None, "limits": None,
        }
        assert [row["id"] for row in read_credential_pool()["anthropic"]] == ["relay-excluded"]
