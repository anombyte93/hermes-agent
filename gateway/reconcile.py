"""Gateway lifecycle ownership and idempotent reconciliation (issue #9).

Reported live on 2026-09-02: ``hermes gateway status`` printed a failed,
disabled ``hermes-gateway.service``, an outdated unit definition, a live
same-profile gateway process, and "port 8642 already in use" — all at once,
with no single answer to "who owns this gateway, and is it safe to restart?".
Ready Kanban cards sat undispatched behind the same ambiguity.

Three pure pieces live here, deliberately free of I/O so they are testable
against known answers and cannot themselves touch a live gateway:

``classify_gateway_ownership``
    Collapses (service state, listener state) into exactly one of
    ``healthy`` / ``degraded`` / ``stopped``, with machine-readable reasons.
    ``healthy`` is the narrow case: an installed, active, current-definition
    service whose ``MainPID`` IS the live listener.  Anything else that is
    running is ``degraded`` — never healthy, never stopped.

``plan_reconciliation`` / ``apply_reconciliation``
    Produce and execute the smallest ordered action list that moves the box
    toward managed-healthy.  The load-bearing invariant, enforced both when
    planning and again immediately before the effect: **a start is never
    emitted while a same-profile listener is alive.**  Taking ownership of an
    unmanaged process requires an explicit ``allow_takeover`` and always stops
    the incumbent first, so the estate can never end up with two listeners.
    Re-running against a reconciled box is a no-op.

``describe_port_owner``
    Names the process and profile holding a port, from value-safe fields only.
    The record has no argv/cmdline/environ field at all, so a port-conflict
    diagnostic cannot leak a token that happened to be on a command line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

# --- ownership states -------------------------------------------------------

OWNERSHIP_HEALTHY = "healthy"
OWNERSHIP_DEGRADED = "DEGRADED"
OWNERSHIP_STOPPED = "stopped"

# --- reconciliation actions -------------------------------------------------

ACTION_REFRESH_UNIT = "refresh_unit"
ACTION_INSTALL_SERVICE = "install_service"
ACTION_RESET_FAILED = "reset_failed"
ACTION_STOP_LISTENER = "stop_listener"
ACTION_START_SERVICE = "start_service"


@dataclass(frozen=True)
class ServiceState:
    """What the service manager says about this profile's gateway unit."""

    installed: bool
    active: bool
    failed: bool = False
    enabled: bool = True
    unit_current: bool = True
    main_pid: Optional[int] = None


@dataclass(frozen=True)
class ListenerState:
    """The live same-profile gateway process, if any.

    ``pid`` is ``None`` when no process is actually alive (a stale PID file is
    not a listener).  ``managed`` means the service manager supervises it.
    """

    pid: Optional[int]
    profile: Optional[str] = None
    managed: bool = False


@dataclass(frozen=True)
class GatewayOwnership:
    state: str
    listener_pid: Optional[int]
    listener_managed: bool
    service_installed: bool
    service_active: bool
    reasons: tuple[str, ...]

    @property
    def is_healthy(self) -> bool:
        return self.state == OWNERSHIP_HEALTHY

    def describe(self) -> str:
        if self.state == OWNERSHIP_HEALTHY:
            return f"✓ Gateway healthy: service supervises PID {self.listener_pid}"
        if self.state == OWNERSHIP_STOPPED:
            return "✗ Gateway stopped: no service and no live listener"
        detail = ", ".join(self.reasons) or "unknown"
        pid = self.listener_pid
        who = f"live listener PID {pid}" if pid else "no live listener"
        return f"⚠ Gateway DEGRADED: {who} ({detail})"


def classify_gateway_ownership(
    service: ServiceState,
    listener: Optional[ListenerState],
) -> GatewayOwnership:
    """Collapse service + listener facts into one honest ownership verdict."""
    listener_pid = listener.pid if listener else None
    listener_managed = bool(listener and listener.managed and listener.pid)

    reasons: list[str] = []
    if service.installed and not service.unit_current:
        reasons.append("service_definition_outdated")
    if service.failed:
        reasons.append("service_failed")
    if service.installed and not service.enabled:
        reasons.append("service_disabled")
    if not service.installed:
        reasons.append("service_not_installed")

    if listener_pid is None:
        if service.active:
            # systemd claims active but nothing is actually listening for this
            # profile: the unit is lying, or the process died without systemd
            # noticing yet. Never "healthy", never "stopped".
            reasons.append("service_active_without_listener")
            return GatewayOwnership(
                state=OWNERSHIP_DEGRADED,
                listener_pid=None,
                listener_managed=False,
                service_installed=service.installed,
                service_active=service.active,
                reasons=tuple(reasons),
            )
        return GatewayOwnership(
            state=OWNERSHIP_STOPPED,
            listener_pid=None,
            listener_managed=False,
            service_installed=service.installed,
            service_active=False,
            reasons=tuple(reasons),
        )

    # A listener is alive. It is healthy only when the service both claims to
    # be running and claims THIS pid.
    if not service.active:
        reasons.append("unmanaged_listener")
    elif service.main_pid is not None and service.main_pid != listener_pid:
        reasons.append("listener_pid_mismatch")
    elif not listener_managed:
        reasons.append("unmanaged_listener")

    if not reasons:
        return GatewayOwnership(
            state=OWNERSHIP_HEALTHY,
            listener_pid=listener_pid,
            listener_managed=True,
            service_installed=service.installed,
            service_active=service.active,
            reasons=(),
        )

    return GatewayOwnership(
        state=OWNERSHIP_DEGRADED,
        listener_pid=listener_pid,
        listener_managed=listener_managed,
        service_installed=service.installed,
        service_active=service.active,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ReconcilePlan:
    state: str
    actions: tuple[str, ...]
    listener_pid: Optional[int]
    requires_operator_handoff: bool
    reasons: tuple[str, ...]

    @property
    def starts_listener(self) -> bool:
        return ACTION_START_SERVICE in self.actions

    def describe(self) -> str:
        if not self.actions:
            return "Nothing to reconcile — the gateway is already managed-healthy."
        lines = [f"Reconciliation plan ({self.state}):"]
        lines.extend(f"  - {a}" for a in self.actions)
        if self.requires_operator_handoff:
            lines.append(
                "  ! A live unmanaged listener owns this profile. Ownership "
                "transfer needs an explicit operator handoff (--takeover); "
                "this plan will NOT start a second listener."
            )
        return "\n".join(lines)


def plan_reconciliation(
    ownership: GatewayOwnership,
    *,
    allow_takeover: bool = False,
) -> ReconcilePlan:
    """Smallest ordered action list toward managed-healthy.

    Never emits ``start_service`` while a listener is alive unless the plan
    also stops that listener first, under an explicit ``allow_takeover``.
    """
    actions: list[str] = []

    if not ownership.service_installed:
        actions.append(ACTION_INSTALL_SERVICE)
    elif "service_definition_outdated" in ownership.reasons:
        # Rewriting the unit file starts nothing, so it is always safe — even
        # beside a live unmanaged listener.
        actions.append(ACTION_REFRESH_UNIT)

    if "service_failed" in ownership.reasons:
        actions.append(ACTION_RESET_FAILED)

    if ownership.state == OWNERSHIP_HEALTHY:
        return ReconcilePlan(
            state=ownership.state,
            actions=tuple(actions),
            listener_pid=ownership.listener_pid,
            requires_operator_handoff=False,
            reasons=ownership.reasons,
        )

    live_listener = ownership.listener_pid is not None
    handoff_needed = live_listener and not ownership.listener_managed

    if live_listener:
        if allow_takeover:
            actions.append(ACTION_STOP_LISTENER)
            actions.append(ACTION_START_SERVICE)
        # Without takeover: no start. The incumbent keeps serving.
    else:
        actions.append(ACTION_START_SERVICE)

    return ReconcilePlan(
        state=ownership.state,
        actions=tuple(actions),
        listener_pid=ownership.listener_pid,
        requires_operator_handoff=handoff_needed and not allow_takeover,
        reasons=ownership.reasons,
    )


@dataclass(frozen=True)
class ReconcileResult:
    performed: tuple[str, ...]
    refused_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.refused_reason is None


def apply_reconciliation(plan: ReconcilePlan, ops: Any) -> ReconcileResult:
    """Execute ``plan`` through ``ops``, re-probing before any start.

    ``ops`` supplies ``listener_is_live()`` plus one method per action name.
    The re-probe is the second half of the no-duplicate-listener guarantee: a
    plan is built from a snapshot, and between snapshot and effect an operator
    (or a systemd auto-restart) can bring a listener up.  A start against a
    live listener is refused outright rather than racing it.
    """
    performed: list[str] = []
    for action in plan.actions:
        if action == ACTION_START_SERVICE:
            try:
                live = bool(ops.listener_is_live())
            except Exception:
                # Cannot prove the port is free — refuse. A duplicate listener
                # is worse than a deferred start.
                return ReconcileResult(tuple(performed), "listener_probe_failed")
            if live:
                return ReconcileResult(tuple(performed), "listener_already_live")
            ops.start_service()
        elif action == ACTION_STOP_LISTENER:
            if plan.listener_pid is None:
                continue
            ops.stop_listener(plan.listener_pid)
        else:
            getattr(ops, action)()
        performed.append(action)
    return ReconcileResult(tuple(performed))


# ---------------------------------------------------------------------------
# Port collision ownership — value safe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortBinding:
    """One listening socket, reduced to non-sensitive fields."""

    port: int
    pid: Optional[int]
    laddr_host: str = ""


#: Every field a :class:`PortOwner` is allowed to carry. Asserted by the test
#: suite so a future edit cannot quietly add ``argv`` / ``environ`` to a record
#: that is printed to operators and written into diagnostics.
PORT_OWNER_VALUE_SAFE_FIELDS = frozenset(
    {"port", "pid", "profile", "process_name", "laddr_host", "is_hermes_gateway"}
)


@dataclass(frozen=True)
class PortOwner:
    port: int
    pid: Optional[int]
    profile: Optional[str]
    process_name: Optional[str]
    laddr_host: str
    is_hermes_gateway: bool

    def describe(self) -> str:
        who = self.process_name or "an unidentified process"
        text = f"Port {self.port} is held by {who}"
        if self.pid:
            text += f" (PID {self.pid})"
        if self.profile:
            text += f", the '{self.profile}' profile gateway"
            text += (
                f". Stop that gateway first: hermes --profile {self.profile} "
                "gateway stop"
            )
        else:
            text += ". Stop it, or set a different port in config.yaml"
        return text


def _default_connections(port: int) -> list[PortBinding]:
    """Enumerate listening sockets on ``port`` using psutil, value-safely.

    Only (port, pid, bind host) is lifted out of psutil; argv and environment
    are never read, so they cannot leak into a diagnostic even by accident.
    """
    try:
        import psutil
    except Exception:
        return []
    out: list[PortBinding] = []
    try:
        for conn in psutil.net_connections(kind="inet"):
            laddr = getattr(conn, "laddr", None)
            if not laddr or getattr(laddr, "port", None) != port:
                continue
            if getattr(conn, "status", "") not in ("LISTEN", "NONE", ""):
                continue
            out.append(
                PortBinding(
                    port=port,
                    pid=getattr(conn, "pid", None),
                    laddr_host=str(getattr(laddr, "ip", "") or ""),
                )
            )
    except Exception:
        return []
    return out


def _default_pid_profile(pid: int) -> Optional[str]:
    """Profile label for a PID, via the gateway PID files (never argv)."""
    try:
        from hermes_cli.gateway import find_profile_gateway_processes
    except Exception:
        return None
    try:
        for proc in find_profile_gateway_processes():
            if proc.pid == pid:
                return proc.profile
    except Exception:
        return None
    return None


def _default_process_name(pid: int) -> Optional[str]:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:
        return None


def describe_port_owner(
    port: int,
    *,
    connections_fn: Optional[Callable[[], Iterable[PortBinding]]] = None,
    pid_profile_fn: Optional[Callable[[int], Optional[str]]] = None,
    process_name_fn: Optional[Callable[[int], Optional[str]]] = None,
) -> Optional[PortOwner]:
    """Name the owner of ``port``, or ``None`` when it cannot be determined.

    Every probe is best-effort: on a locked-down box ``psutil`` may refuse to
    map sockets to PIDs, and an unresolvable owner degrades to ``None`` rather
    than raising into the status path.
    """
    conn_fn = connections_fn or (lambda: _default_connections(port))
    profile_fn = pid_profile_fn or _default_pid_profile
    name_fn = process_name_fn or _default_process_name

    try:
        bindings: Sequence[PortBinding] = list(conn_fn())
    except Exception:
        return None
    if not bindings:
        return None

    binding = next((b for b in bindings if b.pid), bindings[0])
    pid = binding.pid

    profile = None
    process_name = None
    if pid:
        try:
            profile = profile_fn(pid)
        except Exception:
            profile = None
        try:
            process_name = name_fn(pid)
        except Exception:
            process_name = None

    return PortOwner(
        port=port,
        pid=pid,
        profile=profile,
        process_name=process_name,
        laddr_host=binding.laddr_host,
        is_hermes_gateway=bool(profile),
    )
