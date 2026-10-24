"""Positive liveness signal for the embedded Kanban dispatcher (issue #9).

The reported failure was not only that ready cards went undispatched — it was
that nothing could tell the difference between "the dispatcher ticked and the
board was idle" and "no dispatcher is running at all".  Both looked like
silence, and silence read as healthy.

This module persists one small record beside the gateway's other identity files
(``{HERMES_HOME}/gateway_dispatcher.json``):

* ``updated_at`` — advances on EVERY dispatcher tick, idle or not, so the
  dispatcher's own liveness is observable independently of board traffic;
* ``last_dispatch_at`` — advances only on a tick that actually spawned work,
  so an operator can see when the gateway last released a card;
* ``pid``/``start_time`` — the writing process's identity, so a file left
  behind by a dead gateway is recognised as stale rather than vouching for a
  dispatcher that is not running.

Deliberately import-light (stdlib + ``gateway.status`` helpers): the CLI status
path reads it, and status must never fail because a heavy import failed.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger_name = __name__

# Max age of a heartbeat before its liveness claim is treated as suspect.  The
# dispatcher's default tick is 60s, so three missed ticks is a real outage and
# not a slow board.  Kept above ``dispatch_interval_seconds`` by a wide margin
# so a busy tick never reads as stale.
DISPATCHER_HEARTBEAT_STALE_TTL_S = 180

_HEARTBEAT_FILENAME = "gateway_dispatcher.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dispatcher_heartbeat_path() -> Path:
    """Path to this profile's dispatcher heartbeat file."""
    from gateway.status import _get_process_hermes_home

    return _get_process_hermes_home() / _HEARTBEAT_FILENAME


def read_dispatcher_heartbeat(
    path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Return the persisted heartbeat record, or ``None`` when absent/unreadable."""
    from gateway.status import _read_json_file

    return _read_json_file(path or dispatcher_heartbeat_path())


def record_dispatcher_tick(
    *,
    boards: Iterable[str] = (),
    spawned: int = 0,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Stamp one dispatcher tick and return the new record.

    ``spawned`` is the number of workers this tick actually started across all
    boards.  Only a non-zero count advances ``last_dispatch_at`` — the
    heartbeat proves the loop is alive, ``last_dispatch_at`` proves it is
    releasing work.  Best-effort: a write failure returns the in-memory record
    rather than raising, because a diagnostics field must never be able to
    crash the dispatch loop it observes.
    """
    from gateway.status import _get_process_start_time, _read_json_file, _write_json_file

    target = path or dispatcher_heartbeat_path()
    previous = _read_json_file(target) or {}

    try:
        ticks = int(previous.get("ticks", 0) or 0)
    except (TypeError, ValueError):
        ticks = 0

    pid = os.getpid()
    record: dict[str, Any] = {
        "kind": "hermes-gateway-dispatcher",
        "pid": pid,
        "start_time": _get_process_start_time(pid),
        "updated_at": _utc_now_iso(),
        "ticks": ticks + 1,
        "boards": sorted({str(b) for b in boards if b}),
        "last_spawned": max(0, int(spawned or 0)),
        "interval_ttl_s": DISPATCHER_HEARTBEAT_STALE_TTL_S,
    }

    previous_last = previous.get("last_dispatch_at")
    if record["last_spawned"] > 0:
        record["last_dispatch_at"] = record["updated_at"]
    else:
        record["last_dispatch_at"] = previous_last if previous_last else None

    try:
        _write_json_file(target, record)
    except Exception:  # pragma: no cover - diagnostics must never raise
        pass
    return record


def dispatcher_heartbeat_is_stale(
    record: Optional[dict[str, Any]],
    ttl_s: int = DISPATCHER_HEARTBEAT_STALE_TTL_S,
) -> bool:
    """Return True when the heartbeat cannot vouch for a running dispatcher.

    Stale when the record is missing, malformed, older than ``ttl_s``, or
    written by a process that is no longer alive.  The PID check is what stops
    a file left behind by a killed gateway from reading fresh for its final
    ``ttl_s`` seconds.
    """
    if not isinstance(record, dict):
        return True

    from gateway.status import _get_process_start_time, _marker_is_stale, _pid_exists

    if _marker_is_stale(str(record.get("updated_at") or ""), ttl_s):
        return True

    try:
        pid = int(record.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return True
    if pid <= 0 or not _pid_exists(pid):
        return True

    recorded_start = record.get("start_time")
    current_start = _get_process_start_time(pid)
    if (
        recorded_start is not None
        and current_start is not None
        and current_start != recorded_start
    ):
        # PID reuse: a different process now holds that number.
        return True
    return False


def describe_dispatcher_heartbeat(
    record: Optional[dict[str, Any]] = None,
) -> str:
    """One operator-facing line about dispatcher liveness (value-safe)."""
    if record is None:
        record = read_dispatcher_heartbeat()
    if dispatcher_heartbeat_is_stale(record):
        if not isinstance(record, dict):
            return (
                "⚠ Kanban dispatcher: no heartbeat — no gateway-owned dispatcher "
                "has ticked for this profile"
            )
        return (
            "⚠ Kanban dispatcher: heartbeat is stale (last tick "
            f"{record.get('updated_at') or 'unknown'}); ready cards will not "
            "auto-dispatch"
        )
    last = record.get("last_dispatch_at") or "never" if isinstance(record, dict) else "never"
    updated = record.get("updated_at") if isinstance(record, dict) else None
    return (
        f"✓ Kanban dispatcher: alive (last tick {updated}, "
        f"last dispatch {last})"
    )
