"""Only an active OAuth credential may adopt a token from the OAuth resolver."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from anthropic._models import FinalRequestOptions

from agent.turn_recovery import _refresh_credentials_after_401
from agent.turn_retry_state import TurnRetryState
from run_agent import AIAgent


# Deliberately synthetic credentials; no auth store or external API is used.
_API_KEY = "sk-ant-" + "api03-synthetic-console-key"
_OAUTH_OLD = "sk-ant-" + "oat01-synthetic-old-token"
_OAUTH_NEW = "sk-ant-" + "oat01-synthetic-new-token"


@pytest.fixture(params=[False, True], ids=["console-api-key", "oauth"])
def credential_agent(request, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    is_oauth = request.param
    original_key = _OAUTH_OLD if is_oauth else _API_KEY
    resolver = Mock(return_value=_OAUTH_NEW)
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.anthropic_credentials.resolve_anthropic_token", resolver),
    ):
        agent = AIAgent(
            provider="anthropic",
            api_mode="anthropic_messages",
            api_key=original_key,
            base_url="https://api.anthropic.com",
            model="claude-haiku-4-5",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        try:
            assert agent._is_anthropic_oauth is is_oauth
            assert agent._anthropic_api_key == original_key
            resolver.assert_not_called()
            yield agent, resolver, original_key, is_oauth
        finally:
            agent._close_cached_request_anthropic_client(reason="test_cleanup")
            agent._anthropic_client.close()


def test_request_clients_keep_console_key_but_adopt_rotated_oauth(credential_agent):
    agent, resolver, original_key, is_oauth = credential_agent
    original_client = agent._anthropic_client
    expected_key = _OAUTH_NEW if is_oauth else original_key
    request_clients = []
    for _ in range(2):
        client = agent._create_request_anthropic_client(reason="credential_kind_test")
        request_clients.append(client)
        try:
            # Use the real SDK's request-header assembly, not a mocked client builder.
            headers = client._build_headers(FinalRequestOptions(method="post", url="/v1/messages", json_data={}))
            if is_oauth:
                assert headers["authorization"] == f"Bearer {expected_key}"
                assert "x-api-key" not in headers
            else:
                assert headers["x-api-key"] == expected_key
                assert "authorization" not in headers
            assert agent._anthropic_api_key == expected_key
            assert agent._is_anthropic_oauth is is_oauth
        finally:
            agent._close_request_anthropic_client(client, reason="request_complete")

    assert request_clients[0] is request_clients[1]
    if is_oauth:
        assert original_client.is_closed()
        assert resolver.call_count == 2
    else:
        assert agent._anthropic_client is original_client
        assert not original_client.is_closed()
        resolver.assert_not_called()


def test_401_recovery_only_refreshes_oauth(credential_agent):
    agent, resolver, original_key, is_oauth = credential_agent
    retry = TurnRetryState()

    refreshed = _refresh_credentials_after_401(agent, RuntimeError("synthetic 401"), retry, 401)

    assert refreshed is is_oauth
    assert retry.anthropic_auth_retry_attempted is True
    assert agent._anthropic_api_key == (_OAUTH_NEW if is_oauth else original_key)
    assert agent._is_anthropic_oauth is is_oauth
    if is_oauth:
        resolver.assert_called_once_with()
    else:
        resolver.assert_not_called()
