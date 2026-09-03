"""Known-answer controls for gateway lifecycle ownership (issue #9).

Reported live on 2026-09-02: ``hermes-gateway.service`` failed/disabled since
2026-08-29 (exit 78), an *unmanaged* same-profile gateway process still alive,
port 8642 reported only as "occupied", and ready Kanban cards sitting until an
operator ran an exact-board ``hermes kanban dispatch``.

The status path could not name that combination.  These controls pin the three
answers it must give — ``DEGRADED`` for a failed service beside a live
same-profile listener, ``stopped`` when nothing is running, ``healthy`` only
for a service that actually supervises the live listener — plus the two
properties that make a reconciliation safe to run against a live box:

* it never emits a start while a same-profile listener is alive (no second
  listener, ever), and re-running it is a no-op;
* a port collision names its owner from value-safe fields only: never argv,
  never environment, never a credential.
"""

from __future__ import annotations

import pytest

from gateway import reconcile as rc


# ---------------------------------------------------------------------------
# Ownership classification
# ---------------------------------------------------------------------------


def _service(**kw) -> rc.ServiceState:
    base = dict(installed=True, active=True, failed=False, enabled=True,
                unit_current=True, main_pid=1000)
    base.update(kw)
    return rc.ServiceState(**base)


def _listener(pid=1000, **kw) -> rc.ListenerState:
    base = dict(pid=pid, profile="astra", managed=True)
    base.update(kw)
    return rc.ListenerState(**base)


class TestClassifyOwnership:
    def test_failed_service_with_live_manual_process_is_degraded(self):
        """The exact reported state: failed+disabled unit, live manual PID."""
        own = rc.classify_gateway_ownership(
            _service(active=False, failed=True, enabled=False, unit_current=False,
                     main_pid=None),
            _listener(pid=1206662, managed=False),
        )
        assert own.state == rc.OWNERSHIP_DEGRADED
        assert own.state != rc.OWNERSHIP_HEALTHY
        assert own.state != rc.OWNERSHIP_STOPPED
        assert own.listener_pid == 1206662
        assert own.listener_managed is False
        assert "unmanaged_listener" in own.reasons
        assert "service_failed" in own.reasons
        assert "service_definition_outdated" in own.reasons

    def test_stopped_when_no_listener_and_service_inactive(self):
        own = rc.classify_gateway_ownership(
            _service(active=False, failed=False, main_pid=None), None,
        )
        assert own.state == rc.OWNERSHIP_STOPPED
        assert own.listener_pid is None

    def test_dead_recorded_pid_is_still_stopped(self):
        """A PID file left behind by a dead gateway is not a listener."""
        own = rc.classify_gateway_ownership(
            _service(active=False, main_pid=None),
            rc.ListenerState(pid=None, profile="astra", managed=False),
        )
        assert own.state == rc.OWNERSHIP_STOPPED

    def test_healthy_when_service_supervises_the_listener(self):
        own = rc.classify_gateway_ownership(_service(main_pid=4242),
                                            _listener(pid=4242, managed=True))
        assert own.state == rc.OWNERSHIP_HEALTHY
        assert own.reasons == ()

    def test_active_service_with_foreign_pid_is_degraded_not_healthy(self):
        """Service claims a MainPID, but the live listener is a different
        process — that is exactly the ownership ambiguity issue #9 is about."""
        own = rc.classify_gateway_ownership(_service(main_pid=1000),
                                            _listener(pid=2000, managed=False))
        assert own.state == rc.OWNERSHIP_DEGRADED
        assert "listener_pid_mismatch" in own.reasons

    def test_active_service_without_any_listener_is_degraded(self):
        own = rc.classify_gateway_ownership(_service(main_pid=1000), None)
        assert own.state == rc.OWNERSHIP_DEGRADED
        assert "service_active_without_listener" in own.reasons

    def test_outdated_unit_is_never_reported_healthy(self):
        own = rc.classify_gateway_ownership(
            _service(main_pid=4242, unit_current=False), _listener(pid=4242),
        )
        assert own.state == rc.OWNERSHIP_DEGRADED
        assert "service_definition_outdated" in own.reasons


# ---------------------------------------------------------------------------
# Reconciliation planning — idempotent, never two listeners
# ---------------------------------------------------------------------------


class FakeOps:
    """Records service-manager calls; models one machine-global listener."""

    def __init__(self, listener_pid=None):
        self.listener_pid = listener_pid
        self.calls: list[str] = []
        self._next_pid = 9000

    # --- probes -----------------------------------------------------------
    def listener_is_live(self) -> bool:
        return self.listener_pid is not None

    # --- effects ----------------------------------------------------------
    def refresh_unit(self) -> None:
        self.calls.append("refresh_unit")

    def install_service(self) -> None:
        self.calls.append("install_service")

    def reset_failed(self) -> None:
        self.calls.append("reset_failed")

    def stop_listener(self, pid: int) -> None:
        self.calls.append(f"stop_listener:{pid}")
        self.listener_pid = None

    def start_service(self) -> None:
        self.calls.append("start_service")
        if self.listener_pid is not None:
            raise AssertionError(
                "start_service called while a listener was already live — "
                "that is the duplicate listener issue #9 forbids"
            )
        self._next_pid += 1
        self.listener_pid = self._next_pid


def _degraded_live() -> rc.GatewayOwnership:
    return rc.classify_gateway_ownership(
        _service(active=False, failed=True, enabled=False, unit_current=False,
                 main_pid=None),
        _listener(pid=1206662, managed=False),
    )


class TestReconcilePlan:
    def test_degraded_live_listener_plan_never_starts_a_second_listener(self):
        plan = rc.plan_reconciliation(_degraded_live())
        assert rc.ACTION_START_SERVICE not in plan.actions
        assert plan.starts_listener is False
        assert plan.requires_operator_handoff is True

    def test_degraded_live_listener_may_refresh_the_stale_unit(self):
        """Rewriting the unit file starts nothing, so it is always safe."""
        plan = rc.plan_reconciliation(_degraded_live())
        assert rc.ACTION_REFRESH_UNIT in plan.actions

    def test_takeover_stops_the_listener_before_starting_the_service(self):
        plan = rc.plan_reconciliation(_degraded_live(), allow_takeover=True)
        assert rc.ACTION_STOP_LISTENER in plan.actions
        assert rc.ACTION_START_SERVICE in plan.actions
        assert plan.actions.index(rc.ACTION_STOP_LISTENER) < plan.actions.index(
            rc.ACTION_START_SERVICE
        )

    def test_healthy_plan_is_empty(self):
        own = rc.classify_gateway_ownership(_service(main_pid=4242),
                                            _listener(pid=4242))
        plan = rc.plan_reconciliation(own)
        assert plan.actions == ()
        assert plan.starts_listener is False

    def test_stopped_plan_starts_the_service(self):
        own = rc.classify_gateway_ownership(
            _service(active=False, main_pid=None), None,
        )
        plan = rc.plan_reconciliation(own)
        assert rc.ACTION_START_SERVICE in plan.actions
        assert plan.starts_listener is True

    def test_stopped_and_uninstalled_plan_installs_first(self):
        own = rc.classify_gateway_ownership(
            _service(installed=False, active=False, main_pid=None), None,
        )
        plan = rc.plan_reconciliation(own)
        assert plan.actions.index(rc.ACTION_INSTALL_SERVICE) < plan.actions.index(
            rc.ACTION_START_SERVICE
        )

    @pytest.mark.parametrize("allow_takeover", [False, True])
    def test_invariant_start_is_always_preceded_by_a_stop_when_live(
        self, allow_takeover
    ):
        """Structural invariant over every state, not just the reported one."""
        for service in (
            _service(),
            _service(active=False, failed=True, main_pid=None),
            _service(active=False, unit_current=False, main_pid=None),
            _service(installed=False, active=False, main_pid=None),
            _service(main_pid=1000),
        ):
            for listener in (None, _listener(pid=5555, managed=False),
                             _listener(pid=1000, managed=True)):
                own = rc.classify_gateway_ownership(service, listener)
                plan = rc.plan_reconciliation(own, allow_takeover=allow_takeover)
                if rc.ACTION_START_SERVICE in plan.actions and own.listener_pid:
                    assert rc.ACTION_STOP_LISTENER in plan.actions
                    assert plan.actions.index(
                        rc.ACTION_STOP_LISTENER
                    ) < plan.actions.index(rc.ACTION_START_SERVICE)


class TestApplyReconciliation:
    def test_apply_refuses_to_start_beside_a_live_listener(self):
        ops = FakeOps(listener_pid=1206662)
        # A plan built from a stale probe still cannot start a duplicate: apply
        # re-probes immediately before the start.
        stale_plan = rc.ReconcilePlan(
            state=rc.OWNERSHIP_STOPPED,
            actions=(rc.ACTION_START_SERVICE,),
            listener_pid=None,
            requires_operator_handoff=False,
            reasons=(),
        )
        result = rc.apply_reconciliation(stale_plan, ops)
        assert "start_service" not in ops.calls
        assert result.refused_reason == "listener_already_live"

    def test_apply_is_idempotent(self):
        """Second run against the reconciled box changes nothing."""
        ops = FakeOps(listener_pid=None)
        own = rc.classify_gateway_ownership(
            _service(active=False, main_pid=None), None,
        )
        rc.apply_reconciliation(rc.plan_reconciliation(own), ops)
        assert ops.listener_is_live()
        first_pid = ops.listener_pid
        calls_after_first = list(ops.calls)

        # Re-derive from the now-healthy box and apply again.
        own2 = rc.classify_gateway_ownership(
            _service(main_pid=first_pid), _listener(pid=first_pid, managed=True),
        )
        rc.apply_reconciliation(rc.plan_reconciliation(own2), ops)
        assert ops.calls == calls_after_first
        assert ops.listener_pid == first_pid

    def test_takeover_apply_yields_exactly_one_listener(self):
        ops = FakeOps(listener_pid=1206662)
        plan = rc.plan_reconciliation(_degraded_live(), allow_takeover=True)
        rc.apply_reconciliation(plan, ops)
        assert ops.calls.count("start_service") == 1
        assert ops.listener_is_live()
        assert ops.listener_pid != 1206662


# ---------------------------------------------------------------------------
# Port collision ownership — value safe
# ---------------------------------------------------------------------------


class TestPortOwner:
    def _owner(self, **kw):
        conns = [rc.PortBinding(port=8642, pid=1206662, laddr_host="0.0.0.0")]
        return rc.describe_port_owner(
            8642,
            connections_fn=lambda: conns,
            pid_profile_fn=lambda pid: "astra" if pid == 1206662 else None,
            process_name_fn=lambda pid: "python3",
            **kw,
        )

    def test_names_pid_and_profile(self):
        owner = self._owner()
        assert owner is not None
        assert owner.pid == 1206662
        assert owner.profile == "astra"
        assert owner.process_name == "python3"
        assert owner.is_hermes_gateway is True

    def test_description_is_actionable_and_value_safe(self):
        owner = self._owner()
        text = owner.describe()
        assert "8642" in text
        assert "1206662" in text
        assert "astra" in text
        # No argv, no environment, no secret material — the description is
        # built from the value-safe fields only. (`--profile` in the remedy is
        # the suggested CLI command, not the owner's own command line.)
        lowered = text.lower()
        for banned in ("token", "secret", "api_key", "password", "environ", "argv"):
            assert banned not in lowered

    def test_owner_record_exposes_no_argv_or_environment_fields(self):
        owner = self._owner()
        fields = set(owner.__dataclass_fields__)
        assert fields <= rc.PORT_OWNER_VALUE_SAFE_FIELDS
        assert not (fields & {"argv", "cmdline", "environ", "env", "command"})

    def test_unowned_port_returns_none(self):
        assert rc.describe_port_owner(
            8642, connections_fn=lambda: [],
            pid_profile_fn=lambda pid: None,
            process_name_fn=lambda pid: None,
        ) is None

    def test_probe_failure_degrades_to_none_not_an_exception(self):
        def boom():
            raise PermissionError("no ptrace")

        assert rc.describe_port_owner(
            8642, connections_fn=boom,
            pid_profile_fn=lambda pid: None,
            process_name_fn=lambda pid: None,
        ) is None

    def test_non_hermes_owner_is_reported_without_a_profile(self):
        conns = [rc.PortBinding(port=8642, pid=777, laddr_host="127.0.0.1")]
        owner = rc.describe_port_owner(
            8642,
            connections_fn=lambda: conns,
            pid_profile_fn=lambda pid: None,
            process_name_fn=lambda pid: "nginx",
        )
        assert owner.pid == 777
        assert owner.profile is None
        assert owner.is_hermes_gateway is False
        assert "nginx" in owner.describe()
