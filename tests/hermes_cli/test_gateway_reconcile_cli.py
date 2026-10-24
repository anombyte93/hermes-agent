"""CLI wiring for gateway ownership + reconcile (issue #9).

The pure classification/planning logic is covered in
``tests/gateway/test_gateway_ownership_reconcile.py``. These controls prove the
CLI actually reaches it: that ``collect_gateway_ownership`` maps the reported
live state (failed unit + live manual PID) to ``DEGRADED``, that
``hermes gateway reconcile`` is a dry run by default, and that even with
``--apply`` it refuses to start a second listener.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import gateway as gw


@pytest.fixture
def linux_systemd(monkeypatch):
    monkeypatch.setattr(gw, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gw, "is_macos", lambda: False)
    monkeypatch.setattr(gw, "is_termux", lambda: False)
    monkeypatch.setattr(gw, "_select_systemd_scope", lambda system=False: False)
    monkeypatch.setattr(gw, "_profile_suffix", lambda: "astra")


def _snapshot(**kw):
    base = dict(manager="systemd (user)", service_installed=True,
                service_running=False, gateway_pids=(), service_scope="user")
    base.update(kw)
    return gw.GatewayRuntimeSnapshot(**base)


class TestCollectGatewayOwnership:
    def test_reported_state_is_degraded(self, linux_systemd, monkeypatch):
        """Failed+disabled unit, outdated definition, live manual PID."""
        monkeypatch.setattr(
            gw, "get_gateway_runtime_snapshot",
            lambda system=False: _snapshot(gateway_pids=(1206662,)),
        )
        monkeypatch.setattr(
            gw, "_read_systemd_unit_properties",
            lambda system=False: {"ActiveState": "failed", "MainPID": "0",
                                  "UnitFileState": "disabled"},
        )
        monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: False)

        own = gw.collect_gateway_ownership()
        assert own.state == "DEGRADED"
        assert own.listener_pid == 1206662
        assert "unmanaged_listener" in own.reasons
        assert "service_failed" in own.reasons

    def test_managed_service_is_healthy(self, linux_systemd, monkeypatch):
        monkeypatch.setattr(
            gw, "get_gateway_runtime_snapshot",
            lambda system=False: _snapshot(service_running=True,
                                           gateway_pids=(4242,)),
        )
        monkeypatch.setattr(
            gw, "_read_systemd_unit_properties",
            lambda system=False: {"ActiveState": "active", "MainPID": "4242",
                                  "UnitFileState": "enabled"},
        )
        monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: True)

        assert gw.collect_gateway_ownership().state == "healthy"

    def test_nothing_running_is_stopped(self, linux_systemd, monkeypatch):
        monkeypatch.setattr(
            gw, "get_gateway_runtime_snapshot", lambda system=False: _snapshot(),
        )
        monkeypatch.setattr(
            gw, "_read_systemd_unit_properties",
            lambda system=False: {"ActiveState": "inactive", "MainPID": "0",
                                  "UnitFileState": "enabled"},
        )
        monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: True)

        assert gw.collect_gateway_ownership().state == "stopped"


class TestGatewayReconcileCommand:
    def _degraded(self, monkeypatch):
        monkeypatch.setattr(
            gw, "get_gateway_runtime_snapshot",
            lambda system=False: _snapshot(gateway_pids=(1206662,)),
        )
        monkeypatch.setattr(
            gw, "_read_systemd_unit_properties",
            lambda system=False: {"ActiveState": "failed", "MainPID": "0",
                                  "UnitFileState": "disabled"},
        )
        monkeypatch.setattr(gw, "systemd_unit_is_current", lambda system=False: False)
        monkeypatch.setattr(gw, "find_gateway_pids", lambda *a, **k: [1206662])

    def _forbid_lifecycle_calls(self, monkeypatch):
        def _boom(name):
            def _fn(*a, **k):
                raise AssertionError(f"{name} must not run on a dry run")
            return _fn

        for name in ("systemd_start", "systemd_stop", "systemd_restart",
                     "stop_profile_gateway", "refresh_systemd_unit_if_needed",
                     "systemd_install"):
            monkeypatch.setattr(gw, name, _boom(name))

    def test_dry_run_by_default_changes_nothing(
        self, linux_systemd, monkeypatch, capsys,
    ):
        self._degraded(monkeypatch)
        self._forbid_lifecycle_calls(monkeypatch)

        code = gw.gateway_reconcile()

        out = capsys.readouterr().out
        assert code == 0
        assert "DEGRADED" in out
        assert "Dry run" in out

    def test_dry_run_plan_never_offers_a_start(
        self, linux_systemd, monkeypatch, capsys,
    ):
        self._degraded(monkeypatch)
        self._forbid_lifecycle_calls(monkeypatch)

        gw.gateway_reconcile()

        out = capsys.readouterr().out
        assert "start_service" not in out
        assert "operator handoff" in out or "--takeover" in out

    def test_apply_refuses_to_start_beside_a_live_listener(
        self, linux_systemd, monkeypatch, capsys,
    ):
        """Even --apply --takeover re-probes: with the incumbent still alive
        after the stop attempt, the start is refused rather than raced."""
        self._degraded(monkeypatch)
        stops: list[int] = []
        monkeypatch.setattr(
            gw, "stop_profile_gateway",
            lambda *a, **k: stops.append(1) or True,
        )
        monkeypatch.setattr(gw, "refresh_systemd_unit_if_needed",
                            lambda system=False: True)
        monkeypatch.setattr(gw, "_run_systemctl", lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="", stderr="",
        ))

        def _no_start(*a, **k):
            raise AssertionError("start must not run while a listener is live")

        monkeypatch.setattr(gw, "systemd_start", _no_start)
        # find_gateway_pids keeps reporting the incumbent — the stop did not
        # take effect, so the re-probe must block the start.
        code = gw.gateway_reconcile(apply=True, takeover=True)

        out = capsys.readouterr().out
        assert code == 1
        assert "listener_already_live" in out
        assert "Refusing to start a second one" in out
