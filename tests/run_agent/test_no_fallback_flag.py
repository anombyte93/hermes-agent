"""``--no-fallback`` (#107): a 429 on a pinned model fails the run instead of
switching to ``fallback_providers``.

Every provider call is stubbed: the primary raises HTTP 429 on every attempt
and the fallback client is a spy, so no network is touched. The tests prove

(a) with fallback disabled no fallback provider client is ever built and the
    turn fails on the primary route with its rate-limit reason, and
(b) without the flag the same 429 still switches to the configured fallback
    (default behaviour unchanged).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

_PRIMARY_MODEL = "kimi-k3"
_PRIMARY_PROVIDER = "kimi-coding"
# A different family on an OpenAI-compatible route, so the stubbed response shape fits.
_FALLBACK = {"provider": "zai", "model": "glm-5.3"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if anything here tries to open a real connection."""
    import socket

    def _refuse(*_a, **_k):
        raise AssertionError("network access attempted in a no-network test")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


def _tool_defs():
    return [{
        "type": "function",
        "function": {"name": "web_search", "description": "search",
                     "parameters": {"type": "object", "properties": {}}},
    }]


class _RateLimitError(Exception):
    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - weekly (7-day) usage limit reached")
        self.response = SimpleNamespace(headers={})
        self.body = {"error": {"message": "weekly (7-day) usage limit reached"}}


def _ok_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model=_FALLBACK["model"], usage=None)


def _make_agent(fallback_chain):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-test-key",
            base_url="https://api.kimi.com/coding/v1",
            provider=_PRIMARY_PROVIDER,
            model=_PRIMARY_MODEL,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_chain,
        )
    agent.client = MagicMock()
    agent._credential_pool = None  # no same-provider key to rotate to
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    return agent


def _fallback_spy():
    client = MagicMock()
    client.api_key = "fallback-test-key"
    client.base_url = "https://api.z.ai/api/coding/paas/v4"
    client._custom_headers = None
    client.default_headers = None
    return MagicMock(return_value=(client, _FALLBACK["model"]))


def _drive(agent, spy):
    """Run one turn: the primary model always 429s; the fallback model answers."""
    calls = []

    def _fake_api_call(api_kwargs):
        calls.append(agent.model)
        if agent.model == _PRIMARY_MODEL:
            raise _RateLimitError()
        return _ok_response("answered on fallback")

    agent._interruptible_api_call = _fake_api_call
    # The fallback route may pick the streaming transport; stub it the same way.
    agent._interruptible_streaming_api_call = lambda api_kwargs, **_kw: _fake_api_call(api_kwargs)
    with (
        patch("agent.auxiliary_client.resolve_provider_client", spy),
        patch("hermes_cli.model_normalize.normalize_model_for_provider",
              side_effect=lambda m, p: m),
        patch("run_agent.time.sleep", return_value=None),
        patch("time.sleep", return_value=None),
    ):
        result = agent.run_conversation("review this diff")
    return result, calls


class TestNoFallbackFlag:
    def test_disabled_fallback_never_calls_fallback_provider(self):
        from hermes_cli.fallback_config import disable_fallback

        agent = _make_agent([dict(_FALLBACK)])
        disable_fallback(agent)
        spy = _fallback_spy()

        result, calls = _drive(agent, spy)

        spy.assert_not_called()
        assert set(calls) == {_PRIMARY_MODEL}, calls
        assert agent.model == _PRIMARY_MODEL
        assert agent.provider == _PRIMARY_PROVIDER
        assert result.get("failed") is True
        # A "weekly usage limit" 429 classifies as a quota wall (billing); either way
        # the turn ends on the primary route with its provider reason.
        assert result.get("failure_reason") in {"rate_limit", "billing"}

    def test_fallback_still_happens_without_the_flag(self):
        agent = _make_agent([dict(_FALLBACK)])
        spy = _fallback_spy()

        result, calls = _drive(agent, spy)

        assert spy.called, "default behaviour: the 429 must switch to fallback_providers"
        assert agent.model == _FALLBACK["model"]
        assert calls[0] == _PRIMARY_MODEL and calls[-1] == _FALLBACK["model"]
        assert result.get("final_response") == "answered on fallback"

    def test_guard_holds_when_a_chain_is_reloaded_after_disable(self):
        """A later chain reload (gateway hot reload) must not re-arm fallback."""
        from hermes_cli.fallback_config import disable_fallback

        agent = _make_agent(None)
        disable_fallback(agent)
        agent._fallback_chain = [dict(_FALLBACK)]
        agent._fallback_index = 0
        spy = _fallback_spy()
        with patch("agent.auxiliary_client.resolve_provider_client", spy):
            assert agent._try_activate_fallback() is False
        spy.assert_not_called()
        assert agent.model == _PRIMARY_MODEL

    def test_disable_fallback_empties_chain_and_marks_agent(self):
        from hermes_cli.fallback_config import disable_fallback, fallback_disabled

        agent = _make_agent([dict(_FALLBACK)])
        assert fallback_disabled(agent) is False
        disable_fallback(agent)
        assert agent._fallback_chain == []
        assert agent._fallback_model is None
        assert fallback_disabled(agent) is True

    def test_delegated_child_of_a_no_fallback_parent_gets_no_chain(self):
        from hermes_cli.fallback_config import disable_fallback
        from tools.delegate_tool_config import _resolve_child_fallback_chain

        parent = _make_agent([dict(_FALLBACK)])
        disable_fallback(parent)
        declared = {"fallback_providers": [dict(_FALLBACK)]}
        assert _resolve_child_fallback_chain(parent, declared, pinned=False) is None
        assert _resolve_child_fallback_chain(parent, {}, pinned=False) is None


@pytest.mark.parametrize("disabled", [False, True])
def test_same_provider_key_rotation_is_not_a_fallback(disabled):
    """A pool rotation keeps provider and model; --no-fallback leaves it alone."""
    from hermes_cli.fallback_config import disable_fallback

    agent = _make_agent([dict(_FALLBACK)])
    if disabled:
        disable_fallback(agent)
    entry = SimpleNamespace(id="kimi-2", runtime_api_key="rotated",
                            runtime_base_url="https://api.kimi.com/coding/v1")
    with patch("hermes_cli.anon_auth.route_can_serve_model", return_value=True), \
         patch.object(agent, "_replace_primary_openai_client", lambda **k: None):
        assert agent._swap_credential(entry) is True
    assert (agent.provider, agent.model) == (_PRIMARY_PROVIDER, _PRIMARY_MODEL)
