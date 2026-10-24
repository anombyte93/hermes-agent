"""``record_success()`` must clear an exhausted credential's cooldown state.

Regression for issue #104: a credential marked exhausted by a transient 402
stays ``exhausted ... (ready to retry)`` in ``hermes auth list`` even after
subsequent requests with it succeed, because nothing ever cleared the
exhausted state on a successful request.  ``record_success()`` is the
symmetric counterpart to ``mark_exhausted_and_rotate``.
"""

from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _exhausted_entry(cred_id: str = "cred-1") -> dict:
    return {
        "id": cred_id,
        "label": "deepseek",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-ds-test",
        "base_url": "https://api.deepseek.com",
        "last_status": "exhausted",
        "last_status_at": time.time(),
        "last_error_code": 402,
        "last_error_reason": "invalid_request_error",
    }


def _load(tmp_path, monkeypatch, entries: list[dict]):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _write_auth_store(
        tmp_path,
        {"version": 1, "credential_pool": {"openrouter": entries}},
    )
    from agent.credential_pool import load_pool

    return load_pool("openrouter")


def test_record_success_clears_exhausted_state(tmp_path, monkeypatch):
    pool = _load(tmp_path, monkeypatch, [_exhausted_entry()])

    from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK

    assert pool.entries()[0].last_status == STATUS_EXHAUSTED

    cleared = pool.record_success(credential_id="cred-1")

    assert cleared is not None
    assert cleared.last_status == STATUS_OK
    assert cleared.last_error_code is None
    assert cleared.last_error_reason is None

    # The cleared state must be persisted so a fresh load also renders ok.
    from agent.credential_pool import load_pool

    reloaded = load_pool("openrouter")
    assert reloaded.entries()[0].last_status == STATUS_OK


def test_record_success_matches_api_key_hint(tmp_path, monkeypatch):
    pool = _load(tmp_path, monkeypatch, [_exhausted_entry()])

    from agent.credential_pool import STATUS_OK

    cleared = pool.record_success(api_key_hint="sk-ds-test")

    assert cleared is not None
    assert cleared.id == "cred-1"
    assert cleared.last_status == STATUS_OK


def test_record_success_is_no_op_when_not_exhausted(tmp_path, monkeypatch):
    entry = _exhausted_entry()
    entry.pop("last_status", None)
    entry.pop("last_status_at", None)
    entry.pop("last_error_code", None)
    entry.pop("last_error_reason", None)
    pool = _load(tmp_path, monkeypatch, [entry])

    cleared = pool.record_success(credential_id="cred-1")

    assert cleared is not None
    assert cleared.last_status is None
    assert cleared.last_error_code is None


def test_record_success_stale_id_prefers_key_match(tmp_path, monkeypatch):
    """#79156: a stale credential_id must not clear the non-serving entry.

    When per-turn env refresh rewrites ``api_key`` without rebinding
    ``_credential_pool_entry_id``, recovery passes a stale id together with
    the key that actually served.  ``record_success`` must attribute the
    clear to the key-matched entry, not the stale-id entry (mirroring
    ``mark_exhausted_and_rotate``).
    """
    cred_a = _exhausted_entry("cred-a")
    cred_a["access_token"] = "sk-keyA"
    cred_a["priority"] = 1
    cred_b = _exhausted_entry("cred-b")
    cred_b["access_token"] = "sk-keyB"
    cred_b["priority"] = 0
    pool = _load(tmp_path, monkeypatch, [cred_a, cred_b])

    from agent.credential_pool import STATUS_EXHAUSTED, STATUS_OK

    cleared = pool.record_success(credential_id="cred-a", api_key_hint="sk-keyB")

    by_id = {e.id: e for e in pool.entries()}
    # The entry that actually served (key-matched) is cleared to ok…
    assert cleared is not None
    assert cleared.id == "cred-b"
    assert cleared.last_status == STATUS_OK
    assert cleared.last_error_code is None
    assert by_id["cred-b"].last_status == STATUS_OK
    # …while the stale-id entry (which did NOT serve) stays exhausted.
    assert by_id["cred-a"].last_status == STATUS_EXHAUSTED
    assert by_id["cred-a"].last_error_code == 402


def test_record_success_unmatched_identity_clears_nothing(tmp_path, monkeypatch):
    """A supplied id + hint matching nothing must NOT fall through to current.

    ``mark_exhausted_and_rotate`` deliberately refuses to guess when the
    supplied identity matches no entry; ``record_success`` must do the same,
    otherwise it clears whatever happens to be current.
    """
    pool = _load(tmp_path, monkeypatch, [_exhausted_entry("cred-a")])
    # Establish a current entry so the buggy fall-through has something to clear.
    pool.acquire_lease("cred-a")

    from agent.credential_pool import STATUS_EXHAUSTED

    cleared = pool.record_success(credential_id="does-not-exist", api_key_hint="sk-nope")

    assert cleared is None
    by_id = {e.id: e for e in pool.entries()}
    assert by_id["cred-a"].last_status == STATUS_EXHAUSTED
    assert by_id["cred-a"].last_error_code == 402


def test_record_success_no_identity_clears_current_entry(tmp_path, monkeypatch):
    """With no id and no hint, fall through to the current entry.

    Mirrors ``mark_exhausted_and_rotate``'s identity-less resolution: when no
    identity is supplied, the serving entry is the current selection and
    ``record_success`` should clear it.
    """
    pool = _load(tmp_path, monkeypatch, [_exhausted_entry("cred-1")])
    pool.acquire_lease("cred-1")

    from agent.credential_pool import STATUS_OK

    cleared = pool.record_success()

    assert cleared is not None
    assert cleared.id == "cred-1"
    assert cleared.last_status == STATUS_OK
    assert cleared.last_error_code is None
