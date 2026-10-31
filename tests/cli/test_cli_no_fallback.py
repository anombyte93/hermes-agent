"""``hermes chat --no-fallback`` (#107) at the CLI layer: the configured chain is
dropped before the agent is built, and the flag reaches ``cli.main``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests.cli.test_cli_init import _make_cli

_CHAIN = {"fallback_providers": [{"provider": "anthropic", "model": "claude-opus-5-5"}]}


def test_no_fallback_drops_configured_chain():
    cli = _make_cli(config_overrides=_CHAIN, no_fallback=True)
    assert cli._fallback_model == []
    assert cli._no_fallback is True


def test_default_keeps_configured_chain():
    cli = _make_cli(config_overrides=_CHAIN)
    assert cli._fallback_model == [{"provider": "anthropic", "model": "claude-opus-5-5"}]
    assert cli._no_fallback is False


def test_auth_failover_at_startup_is_refused_with_no_fallback():
    """The startup auth fallthrough walks the same chain; it must find nothing."""
    from hermes_cli.auth import AuthError

    cli = _make_cli(config_overrides=_CHAIN, no_fallback=True)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider") as resolve:
        assert cli._resolve_fallback_runtime(AuthError("expired")) is None
    resolve.assert_not_called()


def test_cmd_chat_passes_no_fallback_to_cli_main(monkeypatch):
    from hermes_cli import main as hmain

    captured = {}
    monkeypatch.setattr("cli.main", lambda **kw: captured.update(kw))
    for name in ("_apply_safe_mode", "_apply_user_config_bypass", "_guard_noninteractive_user_config",
                 "_resolve_chat_session_args", "_warn_retired_xai_models", "_start_chat_background_prefetch",
                 "_pin_kanban_board_env", "_confirm_startup_expensive_model_override", "_read_query_file"):
        monkeypatch.setattr(hmain, name, lambda *a, **k: None)
    monkeypatch.setattr(hmain, "_resolve_use_tui", lambda args: False)
    monkeypatch.setattr(hmain, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr("hermes_cli.free_tier_bootstrap.run_bootstrap", lambda **k: None)

    parser = __import__("hermes_cli._parser", fromlist=["x"]).build_top_level_parser()[0]
    args = parser.parse_args(["chat", "-q", "hi", "--no-fallback"])
    hmain.cmd_chat(args)
    assert captured.get("no_fallback") is True


def test_single_query_exit_names_disabled_fallback_and_exits_nonzero(monkeypatch, capsys):
    import pytest

    import cli as cli_mod

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    shell = SimpleNamespace(_no_fallback=True, session_id="s1",
                            agent=SimpleNamespace(model="kimi-k3", provider="kimi-coding"))
    result = {"failed": True, "failure_reason": "rate_limit", "error": "HTTP 429: rate limit"}
    with pytest.raises(SystemExit) as exc:
        cli_mod._exit_if_no_fallback_failed(shell, result)
    assert exc.value.code == 1
    assert "fallback disabled (--no-fallback): kimi-k3 via kimi-coding" in capsys.readouterr().err

    # Default runs and successful runs are untouched.
    cli_mod._exit_if_no_fallback_failed(SimpleNamespace(_no_fallback=False, agent=None), result)
    cli_mod._exit_if_no_fallback_failed(shell, {"completed": True, "final_response": "ok"})
