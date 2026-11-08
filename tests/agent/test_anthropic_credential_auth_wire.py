"""Synthetic profile-to-SDK auth contracts for Anthropic credentials (#205)."""

import json
from pathlib import Path
from run_agent import AIAgent

import anthropic
import httpx
import pytest

from agent import anthropic_adapter, anthropic_credentials
from agent.agent_init import _init_anthropic_client
from agent.anthropic_adapter import build_anthropic_kwargs
from hermes_cli.runtime_provider import resolve_runtime_provider


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    """Exercise real files and resolution, isolating even borrowed Claude Code paths."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".hermes" / "profiles" / "auth-wire"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    # Only external boundaries are replaced, not the resolver, pool or SDK.
    monkeypatch.setattr(
        anthropic_credentials, "_read_claude_code_credentials_from_keychain", lambda: None
    )
    monkeypatch.setattr(anthropic_adapter, "_anthropic_sdk", anthropic)
    monkeypatch.setattr(anthropic_adapter, "_claude_code_version_cache", "2.1.74")
    monkeypatch.setattr(
        anthropic_credentials, "_post_oauth_token",
        lambda *a, **kw: pytest.fail("Unexpected OAuth network request"),
    )
    return home


@pytest.fixture
def wire_requests(monkeypatch):
    requests = []

    def handle_request(transport, request):
        requests.append(request)
        return httpx.Response(200, request=request, json={
            "id": "synthetic-message", "type": "message", "role": "assistant",
            "model": "claude-fable-5-1", "content": [{"type": "text", "text": "fixture"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return requests


def _native_agent(token, base_url, provider="anthropic"):
    agent = AIAgent.__new__(AIAgent)
    agent.provider = provider
    agent.model = "claude-fable-5-1"
    agent.api_mode = "anthropic_messages"
    agent.base_url = base_url
    agent.quiet_mode = True
    agent._disable_streaming = True
    _init_anthropic_client(agent, token, base_url, None)
    return agent


def _send_through_preflight(agent, kwargs, request_path):
    """Neither the shared nor request-local lifecycle is replaced by a mock."""
    try:
        if request_path == "shared":
            response = agent._anthropic_messages_create(kwargs)
        else:
            client = agent._create_request_anthropic_client(reason="auth_wire_test")
            try:
                with client.with_options(timeout=10) as cloned:
                    response = agent._anthropic_messages_create(kwargs, client=cloned)
            finally:
                agent._close_request_anthropic_client(client, reason="auth_wire_test_complete")
        assert response.content[0].text == "fixture"
    finally:
        agent._anthropic_client.close()


@pytest.mark.parametrize("request_path", ["shared", "request-clone"])
@pytest.mark.parametrize("discovery", ["borrowed-oauth", "sdk-defaults", "oauth-refresh"])
@pytest.mark.parametrize("token,oauth,pool_kind", [
    pytest.param("sk-ant-" + "scoped-synthetic", False, "api_key", id="generic-api"),
    pytest.param("sk-ant-api03-" + "synthetic", False, "api_key", id="console-api"),
    pytest.param("sk-ant-oat01-" + "synthetic", True, "oauth", id="oauth-access"),
    pytest.param("sk-ant-oat-" + "synthetic", True, "oauth", id="setup-token"),
    pytest.param("sk-ant-oat01-" + "synthetic", True, "api_key", id="mislabelled-oauth"),
    pytest.param("eyJ" + "synthetic.jwt.fixture", True, "oauth", id="oauth-jwt"),
    pytest.param("cc-" + "synthetic", True, "oauth", id="claude-code"),
])
def test_profile_credential_keeps_its_auth_on_the_sdk_wire(
    isolated_profile, wire_requests, monkeypatch, token, oauth, pool_kind, request_path, discovery
):
    isolated_profile.joinpath("config.yaml").write_text(
        "model:\n  provider: anthropic\n  default: claude-fable-5-1\n"
        "fallback_providers: []\n"
    )
    isolated_profile.joinpath("auth.json").write_text(json.dumps({
        "version": 1, "credential_pool": {"anthropic": [{
            "id": "synthetic", "label": "Synthetic credential", "source": "manual",
            "auth_type": pool_kind, "priority": 0, "access_token": token,
        }]},
    }))
    runtime = resolve_runtime_provider(requested="anthropic", target_model="claude-fable-5-1")
    assert runtime["provider"] == "anthropic"
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["api_key"] == token
    pool = runtime["credential_pool"]
    assert len(pool.entries()) == 1
    assert pool.entries()[0].auth_type == ("oauth" if oauth else "api_key")
    agent = _native_agent(runtime["api_key"], runtime["base_url"], runtime["provider"])
    # Make an unrelated borrowed subscription available AFTER profile selection.
    borrowed = anthropic_credentials.claude_code_credentials_path()
    borrowed.parent.mkdir(parents=True)
    borrowed.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-" + "oat01-competing-synthetic", "expiresAt": 0,
    }}))
    assert anthropic_credentials.read_claude_code_credentials()["accessToken"] != token
    expected_token = token
    refresh_calls = []
    if discovery == "sdk-defaults":
        # Both SDK defaults compete; discovery returns the incidental paid API key.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-synthetic-api-key")
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-synthetic-bearer")
    elif discovery == "oauth-refresh" and oauth:
        # Follow the actual expired-grant read/refresh/commit path, intercepting
        # only the token-endpoint POST, not resolution or client lifecycle.
        expected_token = "sk-ant-" + "oat01-refreshed-synthetic"
        monkeypatch.setenv("ANTHROPIC_TOKEN", token)
        borrowed.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": "synthetic-refresh", "expiresAt": 1,
        }}))

        def refresh_post(*args, **kwargs):
            refresh_calls.append(kwargs["what"])
            return {"access_token": expected_token, "refresh_token": "synthetic-rotated", "expires_in": 3600}

        monkeypatch.setattr(anthropic_credentials, "_post_oauth_token", refresh_post)
    kwargs = build_anthropic_kwargs(
        model="claude-fable-5-1",
        messages=[{"role": "system", "content": "Hermes Agent"}, {"role": "user", "content": "fixture"}],
        tools=None, max_tokens=128000, reasoning_config={"enabled": True, "effort": "xhigh"},
        is_oauth=agent._is_anthropic_oauth,
    )
    _send_through_preflight(agent, kwargs, request_path)
    assert refresh_calls == (["refresh"] if discovery == "oauth-refresh" and oauth else [])
    if refresh_calls:
        assert anthropic_credentials.read_claude_code_credentials()["accessToken"] == expected_token
    assert agent._anthropic_api_key == expected_token
    assert len(wire_requests) == 1
    request = wire_requests[0]
    expected = {"authorization": f"Bearer {expected_token}"} if oauth else {"x-api-key": expected_token}
    actual = {name: request.headers[name] for name in ("authorization", "x-api-key") if name in request.headers}
    assert actual == expected
    assert request.url.path == "/v1/messages"
    assert ("oauth-2025-04-20" in request.headers["anthropic-beta"]) is oauth
    assert request.headers.get("x-app") == ("cli" if oauth else None)
    assert ("claude-code/" in request.headers["user-agent"]) is oauth
    assert agent._is_anthropic_oauth is oauth
    body = json.loads(request.content)
    assert body["model"] == "claude-fable-5-1"
    assert body["max_tokens"] == 128000
    assert body["thinking"]["type"] == "adaptive"
    assert body["output_config"]["effort"] == "xhigh"
    assert ("You are Claude Code" in json.dumps(body["system"])) is oauth


@pytest.mark.parametrize("base_url,auth_header", [
    ("https://proxy.example.test/anthropic/v1", "x-api-key"),
    ("https://api.kimi.com/coding", "x-api-key"),
    ("https://api.minimax.io/anthropic", "authorization"),
    ("https://api.minimaxi.com/anthropic", "authorization"),
    ("https://example.openai.azure.com/anthropic", "authorization"),
    ("https://example.palantirfoundry.com/api/v2/llm/proxy/anthropic", "authorization"),
])
@pytest.mark.parametrize("token", ["sk-ant-" + "scoped-synthetic", "sk-ant-oat01-" + "synthetic"])
def test_endpoint_auth_precedence_survives_sdk_cloning(
    isolated_profile, wire_requests, monkeypatch, base_url, auth_header, token
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-synthetic-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unrelated-synthetic-bearer")
    agent = _native_agent(token, base_url)
    _send_through_preflight(agent, {
        "model": "synthetic-model", "max_tokens": 1,
        "messages": [{"role": "user", "content": "fixture"}],
    }, "request-clone")
    assert len(wire_requests) == 1
    request = wire_requests[0]
    expected_value = f"Bearer {token}" if auth_header == "authorization" else token
    assert {name: request.headers[name] for name in ("authorization", "x-api-key") if name in request.headers} == {auth_header: expected_value}
    assert "oauth-2025-04-20" not in request.headers.get("anthropic-beta", "")
    assert "x-app" not in request.headers
    assert "claude-code/" not in request.headers["user-agent"]
