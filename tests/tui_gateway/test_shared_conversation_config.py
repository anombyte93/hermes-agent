"""Config parsing + gate for the opt-in shared-conversation mode.

The central invariant here: with no ``shared_conversation`` block, or with it
disabled, the mode is completely inert — that is what keeps every ordinary
Hermes session on exactly the path it took before this module existed.
"""

import os

import pytest

from tui_gateway import shared_conversation as sc


@pytest.fixture(autouse=True)
def _clean():
    sc.reset_config_cache()
    yield
    sc.detach_all(reason="test-teardown")
    sc.reset_config_cache()


def test_absent_or_disabled_block_is_inert():
    assert sc.parse_config(None) is None
    assert sc.parse_config({}) is None
    assert sc.parse_config({"enabled": False, "socket_path": "/tmp/x.sock"}) is None


def test_unix_socket_config_parses():
    cfg = sc.parse_config({"enabled": True, "socket_path": "/tmp/astra.sock"})
    assert cfg is not None
    assert cfg.address == "/tmp/astra.sock"
    assert cfg.token is None


def test_enabled_without_address_is_refused_not_defaulted():
    with pytest.raises(sc.ConfigError, match="no socket_path or tcp_host"):
        sc.parse_config({"enabled": True})


def test_socket_and_tcp_together_are_refused():
    with pytest.raises(sc.ConfigError, match="not both"):
        sc.parse_config(
            {
                "enabled": True,
                "socket_path": "/tmp/a.sock",
                "tcp_host": "127.0.0.1",
                "tcp_port": 9000,
            }
        )


def test_non_loopback_tcp_requires_a_token():
    with pytest.raises(sc.ConfigError, match="requires token_ref"):
        sc.parse_config(
            {"enabled": True, "tcp_host": "10.0.0.5", "tcp_port": 9000}
        )


def test_loopback_tcp_without_token_is_allowed():
    cfg = sc.parse_config(
        {"enabled": True, "tcp_host": "127.0.0.1", "tcp_port": 9000}
    )
    assert cfg is not None
    assert cfg.address == ("127.0.0.1", 9000)


def test_inline_secret_is_refused():
    with pytest.raises(sc.ConfigError, match="refused"):
        sc.parse_config(
            {"enabled": True, "socket_path": "/tmp/a.sock", "token_ref": "hunter2"}
        )


def test_env_secret_resolves(monkeypatch):
    monkeypatch.setenv("ASTRA_TEST_TOKEN", "s3cret")
    cfg = sc.parse_config(
        {
            "enabled": True,
            "socket_path": "/tmp/a.sock",
            "token_ref": "env:ASTRA_TEST_TOKEN",
        }
    )
    assert cfg is not None and cfg.token == "s3cret"


def test_env_secret_missing_is_refused(monkeypatch):
    monkeypatch.delenv("ASTRA_TEST_TOKEN", raising=False)
    with pytest.raises(sc.ConfigError, match="is unset"):
        sc.parse_config(
            {
                "enabled": True,
                "socket_path": "/tmp/a.sock",
                "token_ref": "env:ASTRA_TEST_TOKEN",
            }
        )


def test_file_secret_must_not_be_group_or_world_readable(tmp_path):
    path = tmp_path / "token"
    path.write_text("s3cret", encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(sc.ConfigError, match="group/world readable"):
        sc.parse_config(
            {
                "enabled": True,
                "socket_path": "/tmp/a.sock",
                "token_ref": f"file:{path}",
            }
        )
    os.chmod(path, 0o600)
    cfg = sc.parse_config(
        {"enabled": True, "socket_path": "/tmp/a.sock", "token_ref": f"file:{path}"}
    )
    assert cfg is not None and cfg.token == "s3cret"


def test_transcript_limit_is_bounded_by_the_service_ceiling():
    with pytest.raises(sc.ConfigError, match="1..500"):
        sc.parse_config(
            {"enabled": True, "socket_path": "/tmp/a.sock", "transcript_limit": 501}
        )


def test_maybe_dispatch_is_none_when_mode_is_off(monkeypatch):
    monkeypatch.setattr(sc, "active_config", lambda force=False: None)
    monkeypatch.setattr(sc, "config_error", lambda: None)
    for method in ("session.create", "prompt.submit", "session.close"):
        assert sc.maybe_dispatch("1", method, {}, None) is None


def test_maybe_dispatch_ignores_unrelated_methods(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sc, "active_config", lambda force=False: calls.append(1) or None
    )
    assert sc.maybe_dispatch("1", "config.get", {}, None) is None
    # Unrelated methods must not even read config — that read is the only cost
    # this mode imposes on an ordinary session.
    assert calls == []


def test_broken_config_refuses_instead_of_falling_back_to_a_local_runner(monkeypatch):
    monkeypatch.setattr(sc, "active_config", lambda force=False: None)
    monkeypatch.setattr(sc, "config_error", lambda: "tcp_host requires token_ref")
    resp = sc.maybe_dispatch("1", "session.create", {}, None)
    assert resp is not None
    assert resp["error"]["code"] == sc.ERR_UNAVAILABLE
    assert "tcp_host requires token_ref" in resp["error"]["message"]


# ── activation via a real profile config.yaml on disk ──────────────────
#
# This is the mechanism every entrance actually uses. `hermes -p <name> --tui`
# and the dashboard's `/chat?profile=<name>` both scope a child process by
# pointing HERMES_HOME at the profile directory (see `_resolve_chat_argv` in
# hermes_cli/web_server.py, which sets env["HERMES_HOME"] and deliberately
# spawns that scoped chat its OWN gateway). So "does the mode turn on for
# profile X" reduces to "does the gateway's config read see X's config.yaml".


def _write_profile(tmp_path, body):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")
    return home


def test_mode_activates_from_a_real_profile_config_on_disk(tmp_path, monkeypatch):
    home = _write_profile(
        tmp_path,
        "shared_conversation:\n"
        "  enabled: true\n"
        "  socket_path: /tmp/astra-from-disk.sock\n"
        "  poll_interval: 1.5\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    from tui_gateway import server as gw_server

    monkeypatch.setattr(gw_server, "_hermes_home", str(home))
    gw_server._cfg_cache = None
    gw_server._cfg_mtime = None
    gw_server._cfg_path = None
    sc.reset_config_cache()

    cfg = sc.active_config(force=True)

    assert cfg is not None, sc.config_error()
    assert cfg.address == "/tmp/astra-from-disk.sock"
    assert cfg.poll_interval == 1.5


def test_a_profile_without_the_block_stays_an_ordinary_hermes_profile(
    tmp_path, monkeypatch
):
    home = _write_profile(tmp_path, "model: some-model\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from tui_gateway import server as gw_server

    monkeypatch.setattr(gw_server, "_hermes_home", str(home))
    gw_server._cfg_cache = None
    gw_server._cfg_mtime = None
    gw_server._cfg_path = None
    sc.reset_config_cache()

    assert sc.active_config(force=True) is None
    assert sc.config_error() is None
    # And dispatch really does fall through to the ordinary handlers.
    assert sc.maybe_dispatch("1", "session.create", {}, None) is None

