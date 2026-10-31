"""``hermes chat --no-fallback`` (#107) at the CLI layer: the configured chain is
dropped before the agent is built, and the flag reaches ``cli.main``."""

from __future__ import annotations

import os
import sys
import types
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
    """The startup auth fallthrough walks the same chain; it must find nothing.

    main inlines that fallthrough in ``_ensure_runtime_credentials``: with the
    chain dropped, only the primary route is resolved and the run fails there.
    """
    from hermes_cli.auth import AuthError

    cli = _make_cli(config_overrides=_CHAIN, no_fallback=True)
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=AuthError("expired")) as resolve:
        assert cli._ensure_runtime_credentials() is False
    assert resolve.call_count == 1  # the primary only; no fallback entry was tried


def test_cmd_chat_passes_no_fallback_to_cli_main(monkeypatch):
    import hermes_cli.main as main_mod
    from hermes_cli._parser import build_top_level_parser

    for var in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        monkeypatch.delenv(var, raising=False)
    parser, _subparsers, chat_parser = build_top_level_parser()
    chat_parser.set_defaults(func=main_mod.cmd_chat)
    captured = {}
    fake_cli = types.ModuleType("cli")
    setattr(fake_cli, "main", lambda **kw: captured.update(kw))
    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)
    monkeypatch.setattr(main_mod, "_sync_bundled_skills_for_startup", lambda: None)
    monkeypatch.setattr(main_mod, "_termux_should_prefetch_update_check", lambda: False)

    main_mod.cmd_chat(parser.parse_args(["chat", "-q", "hi", "--no-fallback"]))
    assert captured.get("no_fallback") is True

    captured.clear()
    main_mod.cmd_chat(parser.parse_args(["chat", "-q", "hi"]))
    assert captured.get("no_fallback") is False
    for var in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        os.environ.pop(var, None)


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
