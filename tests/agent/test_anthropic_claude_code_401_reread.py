"""Regression: a revoked-token 401 re-reads the shared Claude Code login once.

hermes-agent #100.  A profile with no ``claude_code`` pool entry resolves its
Anthropic token straight from ``~/.claude/.credentials.json`` — a file the
usage layer repoints between accounts.  When that login rotates mid-run the
in-flight token is revoked upstream (401 ``OAuth access token has been
revoked``).  The agent must re-read the credential source once, retry once
with the new token, and fail with exactly one attempt when nothing changed.
"""

import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent  # noqa: E402
from agent import anthropic_credentials  # noqa: E402

# Synthetic, non-secret token shapes (OAuth-looking prefix so the adapter
# treats them as Bearer tokens).
OLD_TOKEN = "sk-ant-oat01-synthetic-old-account"
NEW_TOKEN = "sk-ant-oat01-synthetic-new-account"


class _RevokedError(Exception):
    def __init__(self):
        super().__init__(
            "Error code: 401 - {'type': 'error', 'error': {'type': "
            "'authentication_error', 'message': 'OAuth access token has been revoked.'}}"
        )
        self.status_code = 401
        self.body = {
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": "OAuth access token has been revoked.",
            },
        }


class _FakeAnthropicClient:
    def __init__(self, token):
        self.token = token

    def close(self):
        pass


def _write_creds(path, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": token,
                    "refreshToken": "synthetic-refresh-" + token[-11:],
                    # Valid for an hour: no refresh POST is ever attempted.
                    "expiresAt": int(time.time() * 1000) + 3_600_000,
                }
            }
        ),
        encoding="utf-8",
    )


def _ok_response():
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="recovered")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        model="claude-test",
    )


@pytest.fixture(autouse=True)
def _reset_runtime_main():
    # run_conversation records the turn's main provider process-globally in
    # agent.auxiliary_client; clear it so later tests in the same process do
    # not inherit an "anthropic" main.
    from agent.auxiliary_client import clear_runtime_main

    clear_runtime_main()
    yield
    clear_runtime_main()


@pytest.fixture()
def creds_path(tmp_path, monkeypatch):
    path = tmp_path / "claude" / ".credentials.json"
    monkeypatch.setattr(anthropic_credentials, "claude_code_credentials_path", lambda: path)
    monkeypatch.setattr(
        anthropic_credentials, "_read_claude_code_credentials_from_keychain", lambda: None
    )
    for var in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # No pool entry: the coder profile resolves the shared login directly.
    monkeypatch.setattr(anthropic_credentials, "_resolve_anthropic_pool_token", lambda: None)
    _write_creds(path, OLD_TOKEN)
    return path


def _make_agent(monkeypatch, api_call):
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kw: [{
        "type": "function",
        "function": {"name": "t", "description": "t", "parameters": {"type": "object", "properties": {}}},
    }])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    monkeypatch.setattr(
        "agent.anthropic_adapter.build_anthropic_client",
        lambda key, base=None, **kw: _FakeAnthropicClient(key),
    )
    # Startup resolves the token exactly like a real coder run.
    token = anthropic_credentials.resolve_anthropic_token()
    assert token == OLD_TOKEN

    agent = run_agent.AIAgent(
        model="claude-test",
        api_key=token,
        base_url="https://api.anthropic.com",
        provider="anthropic",
        api_mode="anthropic_messages",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        max_iterations=4,
    )
    agent._cleanup_task_resources = lambda *a, **k: None
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    agent._disable_streaming = True
    agent._credential_pool = None
    agent._fallback_chain = []
    agent._interruptible_api_call = lambda kw: api_call(agent)
    return agent


def test_revoked_401_rereads_rotated_login_and_retries_once(creds_path, monkeypatch):
    calls = []

    def api_call(agent):
        calls.append(agent._anthropic_client.token)
        if len(calls) == 1:
            # The usage layer repoints the login while the request is in flight.
            _write_creds(creds_path, NEW_TOKEN)
            raise _RevokedError()
        return _ok_response()

    agent = _make_agent(monkeypatch, api_call)
    result = agent.run_conversation("hello")

    assert calls == [OLD_TOKEN, NEW_TOKEN]
    assert result["final_response"] == "recovered"
    assert agent._anthropic_api_key == NEW_TOKEN


def test_revoked_401_unchanged_login_fails_after_one_attempt(creds_path, monkeypatch):
    calls = []

    def api_call(agent):
        calls.append(agent._anthropic_client.token)
        raise _RevokedError()

    agent = _make_agent(monkeypatch, api_call)
    result = agent.run_conversation("hello")

    assert calls == [OLD_TOKEN]
    assert result.get("completed") is not True
    assert agent._anthropic_api_key == OLD_TOKEN


def _arm_swap_gap(creds_path, monkeypatch):
    """Put the login mid-swap (link removed, not yet recreated) and finish the
    swap on the reader's first settle pause.  Returns the swap-state dict."""
    target_new = creds_path.parent / "account-b.json"
    _write_creds(target_new, NEW_TOKEN)
    creds_path.unlink()
    state = {"pauses": 0}

    def fake_pause(seconds):
        state["pauses"] += 1
        if not creds_path.is_symlink():
            creds_path.symlink_to(target_new)

    # raising=False: on unmodified code the hook does not exist and the read
    # simply reports "no credentials" — that is the known-red failure.
    monkeypatch.setattr(anthropic_credentials, "_settle_pause", fake_pause, raising=False)
    return state


def test_resolver_survives_non_atomic_symlink_swap(creds_path, monkeypatch):
    """A startup read inside an ``rm`` + ``ln -s`` repoint must not report
    "No Anthropic credentials found": the reader settles briefly."""
    state = _arm_swap_gap(creds_path, monkeypatch)
    assert anthropic_credentials.resolve_anthropic_token() == NEW_TOKEN
    assert state["pauses"] >= 1


def test_revoked_401_reread_during_swap_gap_retries_once(creds_path, monkeypatch):
    """The 401 re-read itself can land in the swap gap (the 13:11 shape)."""
    calls = []

    def api_call(agent):
        calls.append(agent._anthropic_client.token)
        if len(calls) == 1:
            _arm_swap_gap(creds_path, monkeypatch)
            raise _RevokedError()
        return _ok_response()

    agent = _make_agent(monkeypatch, api_call)
    result = agent.run_conversation("hello")

    assert calls == [OLD_TOKEN, NEW_TOKEN]
    assert result["final_response"] == "recovered"


def test_missing_login_does_not_settle_when_no_claude_dir(tmp_path, monkeypatch):
    """No ~/.claude directory at all (Claude Code never installed): fail fast,
    no settle pauses."""
    path = tmp_path / "absent" / ".credentials.json"
    monkeypatch.setattr(anthropic_credentials, "claude_code_credentials_path", lambda: path)
    pauses = []
    monkeypatch.setattr(anthropic_credentials, "_settle_pause", pauses.append, raising=False)
    assert anthropic_credentials._read_claude_code_credentials_from_file() is None
    assert pauses == []


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("initial_state", ["truncated", "dangling"])
def test_settle_reresolves_borrower_owner(tmp_path, monkeypatch, scoped, initial_state):
    """A settled read follows a repointed borrower, not its stale owner path."""
    home = tmp_path / "home"
    monkeypatch.setattr(
        anthropic_credentials.Path, "home", classmethod(lambda cls: home)
    )
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    selected_dir = home / ".claude"
    if scoped:
        selected_dir = tmp_path / "selected-account"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected_dir))
        _write_creds(home / ".claude" / ".credentials.json", OLD_TOKEN)
    selected_dir.mkdir(parents=True, exist_ok=True)
    old_owner = tmp_path / "old-owner.json"
    if initial_state == "truncated":
        old_owner.write_text("{", encoding="utf-8")
    new_owner = tmp_path / "new-owner.json"
    _write_creds(new_owner, NEW_TOKEN)
    borrower = selected_dir / ".credentials.json"
    borrower.symlink_to(old_owner)
    assert anthropic_credentials.claude_code_credentials_path() == old_owner
    pauses = []

    def finish_swap(seconds):
        pauses.append(seconds)
        borrower.unlink()
        borrower.symlink_to(new_owner)

    monkeypatch.setattr(anthropic_credentials, "_settle_pause", finish_swap)
    creds = anthropic_credentials._read_claude_code_credentials_from_file()

    assert creds is not None
    assert creds["accessToken"] == NEW_TOKEN
    assert pauses == [anthropic_credentials._CREDENTIALS_SETTLE_DELAYS[0]]
    assert borrower.is_symlink()
    assert anthropic_credentials.claude_code_credentials_path() == new_owner
