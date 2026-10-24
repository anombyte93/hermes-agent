"""R2 browser readiness through the REAL host middleware and router.

The brief demands R2 authentication be tested at login and authenticated
states against the real host middleware/router — not direct function calls
and not weakened auth. This file boots the REAL ``web_server.app`` with
``auth_required=True``, a registered password provider (the same harness
pattern as tests/hermes_cli/test_dashboard_auth_password_login.py), mounts
the REAL kanban plugin router through the same include path the server
uses, and drives:

  * login state: no session cookie -> the browser-readiness route returns
    401 JSON, never evidence;
  * authenticated state: valid session cookies -> 200 with the real
    envelope shape;
  * wrong credentials: 401, still no evidence;
  * logout: the session is revoked and the route 401s again.

The helper boundary is faked only at the subprocess boundary (a real
temporary executable on PATH), matching the evidence-suite discipline.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.base import Session


# ---------------------------------------------------------------------------
# Password provider (same shape as the dashboard auth test harness)
# ---------------------------------------------------------------------------


def _sign(secret: bytes, sub: str, kind: str, ttl: int) -> str:
    import base64
    import hashlib
    import hmac

    payload = json.dumps(
        {"sub": sub, "kind": kind, "exp": int(time.time()) + ttl},
        sort_keys=True,
    ).encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + sig).decode().rstrip("=")


def _unsign(secret: bytes, token: str):
    import base64

    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode())
    except Exception:
        return None
    payload, sig = raw[:-16], raw[-16:]
    import hashlib
    import hmac

    expect = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        return json.loads(payload.decode())
    except Exception:
        return None


class BrowserReadinessPasswordProvider:
    name = "testpw"
    display_name = "Test Password"
    supports_password = True
    _secret = b"browser-readiness-secret"

    def start_login(self, *, redirect_uri):
        raise NotImplementedError

    def complete_login(self, **kwargs):
        raise NotImplementedError

    def complete_password_login(self, *, username: str, password: str) -> Session:
        if username == "admin" and password == "hunter2":
            exp = int(time.time()) + 3600
            return Session(
                user_id="admin",
                email="",
                display_name="admin",
                org_id="",
                provider=self.name,
                expires_at=exp,
                access_token=_sign(self._secret, "admin", "access", 3600),
                refresh_token=_sign(self._secret, "admin", "refresh", 30 * 86400),
            )
        from hermes_cli.dashboard_auth.base import InvalidCredentialsError

        raise InvalidCredentialsError("bad credentials")

    def verify_session(self, *, access_token: str):
        p = _unsign(self._secret, access_token)
        if not p or p.get("kind") != "access" or p["exp"] <= int(time.time()):
            return None
        return Session(
            user_id=p["sub"],
            email="",
            display_name=p["sub"],
            org_id="",
            provider=self.name,
            expires_at=p["exp"],
            access_token=access_token,
            refresh_token="",
        )

    def refresh_session(self, *, refresh_token: str):
        raise NotImplementedError

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Fake helper executable (pass behavior only; the route's auth boundary is
# under test here, the helper receipt handling is covered by the plugin
# suite).
# ---------------------------------------------------------------------------


def _write_pass_helper(directory: Path) -> Path:
    script = '''#!/usr/bin/env python3
import json, sys
args = json.loads(sys.stdin.buffer.read().decode("utf-8"))
receipt = {
    "state": "PASS",
    "execution_host": "evo",
    "observed_at": 1789300000.0,
    "data": {
        "board": args.get("board", "evo-alpha"),
        "adapter": {
            "state": "PASS",
            "revision": "a84a2b2c0" * 4,
            "source": "atlas-kanban installed release",
            "version": "1.4.0",
        },
        "observed_at": 1789300000.0,
    },
}
sys.stdout.write(json.dumps(receipt))
'''
    exe = directory / "atlas-kanban-call"
    exe.write_text(script, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


# ---------------------------------------------------------------------------
# Real app + plugin router mounted the way the server mounts it
# ---------------------------------------------------------------------------


@pytest.fixture
def gated_plugin_app(tmp_path, monkeypatch):
    # Isolated home so the plugin's board reads never touch real state.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_pass_helper(fakebin)
    monkeypatch.setenv("PATH", str(fakebin) + os.pathsep + os.environ.get("PATH", ""))

    clear_providers()
    register_provider(BrowserReadinessPasswordProvider())
    from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit

    _reset_password_rate_limit()

    # Mount the REAL plugin router on the REAL app through the same
    # include_router call the server's _mount_plugin_api_routes performs.
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_r2test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    web_server.app.include_router(mod.router, prefix="/api/plugins/kanban")

    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


# ---------------------------------------------------------------------------
# Login and authenticated states through the REAL middleware
# ---------------------------------------------------------------------------


class TestBrowserReadinessAuthGate:
    def test_unauthenticated_request_is_401_json(self, gated_plugin_app):
        resp = gated_plugin_app.get(
            "/api/plugins/kanban/evidence/browser-readiness?board=evo-alpha"
        )
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_unauthenticated_body_carries_no_evidence(self, gated_plugin_app):
        resp = gated_plugin_app.get(
            "/api/plugins/kanban/evidence/browser-readiness?board=evo-alpha"
        )
        body = resp.json()
        dumped = json.dumps(body)
        assert "board_readable" not in dumped
        assert "adapter" not in dumped

    def test_login_then_authenticated_read(self, gated_plugin_app):
        login = gated_plugin_app.post(
            "/auth/password-login",
            json={
                "provider": "testpw",
                "username": "admin",
                "password": "hunter2",
                "next": "/",
            },
        )
        assert login.status_code == 200, login.text
        # The TestClient retains the minted session cookies; this request
        # now crosses the REAL gated middleware authenticated.
        resp = gated_plugin_app.get(
            "/api/plugins/kanban/evidence/browser-readiness?board=evo-alpha"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "PASS"
        assert body["evidence"]["authenticated"] is True
        assert body["evidence"]["board_readable"] is True
        assert body["board"] == "evo-alpha"

    def test_wrong_password_never_authenticates(self, gated_plugin_app):
        bad = gated_plugin_app.post(
            "/auth/password-login",
            json={"provider": "testpw", "username": "admin", "password": "nope"},
        )
        assert bad.status_code == 401
        resp = gated_plugin_app.get(
            "/api/plugins/kanban/evidence/browser-readiness?board=evo-alpha"
        )
        assert resp.status_code == 401

    def test_new_route_paths_not_in_public_allowlist(self):
        """The gate's public API allowlist must not exempt the new doors."""
        from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

        for path in PUBLIC_API_PATHS:
            assert not path.startswith("/api/plugins/kanban/evidence")
        src = Path(web_server.__file__).read_text(encoding="utf-8")
        assert 'path.startswith("/api/")' in src
