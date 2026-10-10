"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2


def auto_block_reason(outcome: str, failures: int, error: Optional[str]) -> str:
    """Reason stamped by every dispatcher auto-block, as one pure string build.

    ``auto-blocked after N <outcome> failure(s): <error excerpt>`` — always
    non-empty, at most 500 chars (``error`` contributes at most its first 200),
    so it satisfies the trg_tasks_block_reason_* guards on every board.
    """
    outcome = (outcome or "failure").strip() or "failure"
    excerpt = " ".join(str(error or "").split())[:200]
    reason = f"auto-blocked after {int(failures)} {outcome} failure(s)"
    if excerpt:
        reason += f": {excerpt}"
    return reason[:500]

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_assignee_not_allowed: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` refused because the board's ``board.json``
    ``allowed_assignees`` does not list the assignee. The card is blocked with the
    reason (operator-actionable); a review card is held with ``source_status=review``
    so it leaves the lane (no reserved review slot) and unblock restores review."""
    skipped_board_unreadable: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, assignee)`` not spawned this tick because the board's
    ``allowed_assignees`` policy could not be read (see ``board_allowlist_state``).
    Transient: the card stays in its lane and is retried next tick."""
    board_allowlist_unreadable: Optional[str] = None
    """Why the board's ``allowed_assignees`` policy could not be read this tick."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck".
    For an assignee-scope deferral ``current`` is that assignee's host-wide
    running count; for a group-scope deferral it is the group's running count
    (the number that hit the cap). ``capped_details`` carries the scope."""
    capped_details: list[dict] = field(default_factory=list)
    """One dict per ``skipped_per_profile_capped`` entry, same order:
    ``{"task_id", "assignee", "running", "cap", "scope"}`` where ``scope`` is
    ``"assignee"`` (its own cap) or ``"group:<name>"`` (``kanban.assignee_groups``)."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    rate_limited: list[str] = field(default_factory=list)
    """``Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    output_limit: list[str] = field(default_factory=list)
    """Task ids whose workers hit the output length limit after doing work
    (end-of-session truncation banner in the worker log) and were released to
    ``ready`` WITHOUT counting a failure — the work is likely already done;
    the retry is briefed to commit, verify, and complete tersely."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap.
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
                return ("rate_limited", code)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children without blocking; returns reaped PIDs. No-op on Windows."""
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Uses ``gateway.status._pid_exists`` (OpenProcess on Windows, ``os.kill(pid, 0)``
    on POSIX). **DO NOT** call ``os.kill(pid, 0)`` directly on Windows — there
    ``sig=0`` is ``CTRL_C_EVENT`` broadcast to the console group, potentially
    killing unrelated processes.

    Zombies (exited, not yet reaped) still pass the existence check, so a
    worker would look "alive" forever between exit and reap. Linux: peek at
    ``/proc/<pid>/status`` and treat ``State: Z`` as dead; macOS: ask ``ps``
    for the BSD ``stat`` field and treat ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _kb._pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    conn: Optional[sqlite3.Connection] = None,
    operator: bool = False,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths.

    Authorization (see :func:`kanban_db.claim_authorizes_host_action`): the
    hostname prefix alone never authorizes a signal. When ``conn`` is given,
    a claim minted by a live foreign dispatcher — or for another board — is
    never signalled (``authorized: False, reason`` in the result dict); the
    caller must then leave the row alone. ``operator=True`` marks explicit
    human force-paths (``reclaim_task``, ``archive_task``) which keep the
    plain same-host rule. Without ``conn`` (legacy call sites) behaviour is
    unchanged: hostname + prefix rule only.
    """
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
        "authorized": True,
        "defer_reason": None,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True
    if conn is not None:
        ok, why = _kb.claim_authorizes_host_action(
            conn, claim_lock, operator=operator,
        )
        if not ok:
            info["authorized"] = False
            info["defer_reason"] = why
            return info
    elif operator:
        # No conn to authorize against: an operator force-path without a
        # connection cannot verify scope; keep the old hostname rule.
        pass

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if _poll_worker_exit(pid):
        info["terminated"] = True
        return info
    if _kb._pid_alive(pid):
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = not _kb._pid_alive(pid)
    return info


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    An UNAUTHORIZED termination (foreign live claimer / other board) also counts:
    the row is not ours to release either.
    """
    if termination.get("authorized") is False and not termination.get(
        "termination_attempted"
    ):
        return True
    return bool(
        termination.get("termination_attempted")
        and termination.get("host_local")
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def notify_model_fallbacks(conn: sqlite3.Connection) -> list[str]:
    """Comment ``model_fallback:`` on cards whose run answered on a different
    model than the task's override (once per run).

    The worker stamps ``task_runs.model_used`` at its first successful API
    call; this pass (every dispatcher tick) turns a mismatch into a visible
    card comment so a human or the next worker sees it without any UI.
    Idempotence: the ``model_fallback`` event on the run scopes "already
    notified" — ticks repeat, comments must not. Runs without model truth
    (pre-#7, never-started) and tasks without an override are skipped.
    Returns the task ids commented.
    """
    rows = conn.execute(
        """
        SELECT r.id AS run_id, r.task_id, r.model_used, r.provider_used, r.fallback_from,
               t.model_override, t.provider_override
          FROM task_runs r
          JOIN tasks t ON t.id = r.task_id
         WHERE r.model_used IS NOT NULL
           AND t.model_override IS NOT NULL
           AND t.model_override != ''
           AND r.model_used != t.model_override
        """
    ).fetchall()
    commented: list[str] = []
    for row in rows:
        run_id = int(row["run_id"])
        already = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'model_fallback' LIMIT 1",
            (row["task_id"], run_id),
        ).fetchone()
        if already:
            continue
        requested = row["model_override"] + (f" ({row['provider_override']})" if row["provider_override"] else "")
        answered = row["model_used"] + (f" ({row['provider_used']})" if row["provider_used"] else "")
        body = (
            f"model_fallback: run #{run_id} was dispatched on {requested} but answered on "
            f"{answered} (provider fallback)."
        )
        if row["fallback_from"]:
            body += f" Worker-recorded fallback origin: {row['fallback_from']}."
        with _kb.write_txn(conn, allow_nested=True):
            _kb.add_comment(conn, row["task_id"], "dispatcher", body)
            _kb._append_event(conn, row["task_id"], "model_fallback", {
                "run_id": run_id, "requested": requested, "answered": answered,
            }, run_id=run_id)
        commented.append(row["task_id"])
    return commented


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Authorization gate: hostname prefix alone must never authorize a
        # timeout kill — a live foreign dispatcher's (or another board's)
        # row is deferred to its owner's tick.
        ok, why = _kb.claim_authorizes_host_action(conn, lock)
        if not ok:
            _kb._log.debug(
                "kanban enforce_max_runtime: deferring task %s (claim %r: %s)",
                row["id"], lock, why,
            )
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL after 5 s grace; workers wanting a cleaner
        # shutdown install their own SIGTERM handler.
        killed = False
        kill = _kill_fn(signal_fn)
        if kill is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                kill(pid, signal.SIGTERM)
            # Short polling wait — no time.sleep on the write txn.
            _poll_worker_exit(pid)
            if _kb._pid_alive(pid):
                killed = _sigkill(kill, pid)

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                _write_resume_note(
                    conn, tid, run_id,
                    outcome="timed_out", error=error,
                )
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
            )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Authorization gate: never signal/release a row whose claim was
        # minted by a live foreign dispatcher or for another board.
        ok, why = _kb.claim_authorizes_host_action(conn, lock)
        if not ok:
            _kb._log.debug(
                "kanban detect_stale_running: deferring task %s (claim %r: %s)",
                tid, lock, why,
            )
            continue

        termination = _kb._terminate_reclaimed_worker(pid, lock, signal_fn=signal_fn)

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            _write_resume_note(
                conn, tid, run_id,
                outcome="stale",
                error=str(payload.get("heartbeat_age_seconds") or "no heartbeat"),
            )
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        if pid and _kb._pid_alive(pid):
            # Never requeue beside a live process. Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is alive on this host — deferring", tid, pid,
            )
            continue
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome in ("rate_limited", "output_limit_reached"):
            # Failure-free requeues (quota wall / output-limit respawn) are
            # neutral: they say nothing about the task.
            continue
        if outcome == "protocol_violation" or (
            outcome == "crashed" and (
                _kb._json_dict(row["metadata"]).get("protocol_violation")
                or "protocol violation" in (row["error"] or "")
            )
        ):
            streak += 1
            continue
        break
    return streak


_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    "worker exited cleanly (rc=0) without calling "
    "kanban_complete or kanban_block — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report the result via kanban_complete; a run that ends "
    "without a terminal kanban call counts as failed no "
    "matter what it did."
)


# The Hermes end-of-session banner for an output-length-limited run
# (agent/turn_truncation.py; the sibling ``First response truncated`` variant
# shares the distinctive suffix). The worker log captures the TUI render —
# padded and CRLF-terminated — so match on the stable phrase, not the layout.
_OUTPUT_LIMIT_MARKER = "truncated due to output length limit"

# How much of the worker log tail to scan: the banner must belong to THIS run's
# dying breath, not an earlier attempt buried in the same append-mode file.
_OUTPUT_LIMIT_TAIL_BYTES = 64 * 1024


def _worker_log_tail_contains_output_limit(task_id: str, board: Optional[str] = None) -> bool:
    """True when the worker's log tail carries the output-length-limit banner.

    Deliberately failure-proof: a missing/unreadable/oversized log never turns
    a crash into an output-limit respawn — only the banner does. When the file
    is larger than the tail window the marker must sit inside the window;
    history from earlier attempts must not reclassify a genuine crash.
    """
    try:
        path = _kb.worker_logs_dir(board=board) / f"{task_id}.log"
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > _OUTPUT_LIMIT_TAIL_BYTES:
                fh.seek(-_OUTPUT_LIMIT_TAIL_BYTES, os.SEEK_END)
            data = fh.read(_OUTPUT_LIMIT_TAIL_BYTES)
        return _OUTPUT_LIMIT_MARKER.encode() in data
    except (OSError, ValueError):
        return False


_OUTPUT_LIMIT_ERROR = (
    "worker hit the output length limit before it could report — "
    "work on disk is likely complete; retry asked to commit, verify, "
    "and complete without re-deriving. No failure counted."
)


def _output_limit_dead_worker(pid: int, claimer: Optional[str], base: _DeadWorker) -> _DeadWorker:
    """Rebook ``base`` as an output-limit death: failure-free requeue.

    Keeps the underlying exit facts (kind/code — the worker WAS killed, often
    SIGKILL from the runtime's own truncation path) but retargets outcome, run
    error, and event kind, and flips the flags so the reclaim/accounting loops
    treat it like a quota wall: released to ``ready``, no failure counted.
    """
    payload = dict(base.event_payload)
    payload["output_limit"] = True
    return _DeadWorker(
        base.kind, base.code, _OUTPUT_LIMIT_ERROR, "output_limit_reached", payload,
        output_limit=True,
    )


_OUTPUT_LIMIT_RESUME_NOTE = (
    "[resume note from dispatcher] The previous run ended on the Hermes banner "
    "`Response truncated due to output length limit` after doing real work — do NOT "
    "re-derive it. First run `git log --oneline <base>..HEAD` and "
    "`git status --short` in the workspace to see what the prior run committed and "
    "left dirty; commit anything finished, run the task's documented checks once "
    "quietly, then call kanban_complete immediately. Keep output minimal: do not "
    "narrate, do not re-read files, do not re-run green tests."
)


def _output_limit_resume_note_prompt(base: str = "") -> str:
    """Terse-complete brief prefixed to the respawn prompt (``-q`` payload)."""
    return _OUTPUT_LIMIT_RESUME_NOTE + (f"\n{base}" if base else "")


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    # Output-limit rebook (banner in the worker log tail): failure-free
    # requeue with a terse-complete resume note, like a quota wall.
    output_limit: bool = False
    # Precise ``task_runs.outcome``; None falls back to the legacy buckets
    # (``rate_limited`` / ``crashed``) so ad-hoc callers keep old behaviour.
    outcome: Optional[str] = None

    @property
    def run_outcome(self) -> str:
        if self.outcome is not None:
            return self.outcome
        # Failure-free requeues are recorded as their own outcomes so board
        # history doesn't show a phantom crash for a quota wall / truncation.
        if self.rate_limited:
            return "rate_limited"
        if self.output_limit:
            return "output_limit_reached"
        return "crashed"


def _classify_dead_worker(pid: int, claimer: Optional[str]) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    Distinct causes stay distinct: rc=0-without-complete is
    ``protocol_violation`` (bounded violation-only budget), a signal death is
    ``signaled:<n>`` (a real crash — counts against the breaker), anything
    else is ``crashed``.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
            outcome="protocol_violation",
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    outcome: Optional[str] = None
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
        if kind == "signaled":
            # SIGBUS/SIGKILL/... — the board must show WHICH signal killed the
            # worker, not a generic "crashed".
            outcome = f"signaled:{code}"
    event_kind = "signaled" if kind == "signaled" else "crashed"
    return _DeadWorker(kind, code, error_text, event_kind, event_payload, outcome=outcome)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # Output-limit requeues: failure-free, terse-complete respawn (like
    # ``rate_limited`` — never in ``crashed``).
    output_limit: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, protocol_violation, error_text)``: accounted
    # after the txn via ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, bool, str]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)


def _reclaim_dead_workers(
    conn: sqlite3.Connection, board: Optional[str] = None,
) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead."""
    sweep = _CrashSweep()
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock, started_at, assignee "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Authorization gate: a claim minted by a live foreign dispatcher
            # or for another board is not ours to reclaim, even when the
            # worker pid is dead on this host.
            ok, why = _kb.claim_authorizes_host_action(conn, lock)
            if not ok:
                _kb._log.debug(
                    "kanban detect_crashed_workers: deferring task %s (claim %r: %s)",
                    row["id"], lock, why,
                )
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _kb._pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            dead = _classify_dead_worker(pid, row["claim_lock"])
            if (
                not dead.rate_limited
                and not dead.protocol_violation
                and dead.outcome != "output_limit_reached"
                and _worker_log_tail_contains_output_limit(row["id"], board=board)
            ):
                # The worker's own log says it died on the output length limit
                # AFTER doing work (the end-of-session banner) — failure-free
                # requeue with a terse-complete resume note, not a crash.
                dead = _output_limit_dead_worker(pid, row["claim_lock"], dead)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            dead.event_payload["retry_status"] = retry_status
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=dead.error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            _write_resume_note(
                conn, row["id"], run_id,
                outcome=dead.run_outcome, error=dead.error_text,
            )
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.protocol_violation or dead.output_limit:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message. An
                # output-limit requeue is the same shape: no failure, but the
                # retry worker must see the terse-complete instruction, and
                # ``_worker_argv`` prefixes it onto the respawn prompt.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (dead.error_text[:500], row["id"]),
                )
            if dead.output_limit:
                # Durable resume note on the card itself: the retry worker's
                # context (``build_worker_context``) reads comments, so the
                # instruction survives even if the prompt prefix is missed.
                try:
                    _kb.add_comment(
                        conn, row["id"], "dispatcher",
                        _OUTPUT_LIMIT_RESUME_NOTE,
                    )
                except Exception:  # pragma: no cover - best-effort
                    _kb._log.debug(
                        "kanban dispatch: failed to append output-limit resume note to %s",
                        row["id"], exc_info=True,
                    )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.output_limit:
                # Failure-free: NOT a crash — must not count against the breaker
                # (below) nor land in ``crashed`` (the dispatcher's return).
                sweep.output_limit.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append(
                    (row["id"], pid, row["claim_lock"], dead.protocol_violation, dead.error_text)
                )
    return sweep


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) trip
    immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, _, err_text in crash_details:
        fp = _error_fingerprint(err_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, protocol_violation, error_text in crash_details:
        if protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(
    conn: sqlite3.Connection, board: Optional[str] = None,
) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall, released WITHOUT counting a failure and surfaced via the
    ``_last_rate_limited`` attribute (the return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # and output-limit requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    detect_crashed_workers._last_output_limit = sweep.output_limit  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + 1

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error,
                    metadata={"failures": failures, "retry_status": retry_status},
                )
                _kb._append_event(
                    conn, task_id, outcome,
                    {"error": error, "failures": failures, "retry_status": retry_status},
                    run_id=run_id,
                )
                if run_id is not None:
                    _write_resume_note(
                        conn, task_id, run_id, outcome=outcome, error=error,
                    )
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did. Both stamp the current
        # reason as durable task state — a reasonless blocked row would abort
        # against the trg_tasks_block_reason_update guard on guarded boards
        # (the 2026-09-13 dispatcher wedge).
        block_reason = auto_block_reason(outcome, failures, error)
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
               if release_claim else "")
            + "block_reason = ?, "
            "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (block_reason, failures, error, task_id),
        )
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
            "block_reason": block_reason,
        }
        run_id = None
        if end_run:
            # Only the spawn path has an open run to close.
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
            if run_id is not None:
                _write_resume_note(
                    conn, task_id, run_id, outcome="gave_up", error=error,
                )
        if event_payload_extra:
            payload.update(event_payload_extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + emit a ``spawned`` event carrying it."""
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (int(pid), task_id))
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (int(pid), run_id))
        _kb._append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)


# --- Automatic dispatcher resume note ---------------------------------------
#
# Every path that closes a run with a non-completed outcome writes one
# self-contained ``author='dispatcher'`` comment so the retry worker resumes
# from the previous attempt's real state instead of restarting blind
# (hand-written precedent: the seven "Retry note from the parent" comments on
# the align-apple-feel wave, 2026-09-13).
#
# ``_kb.RESUME_NOTE_AUTHOR`` / ``_kb.RESUME_NOTE_MARKER`` live in
# ``kanban_db`` (single source) because ``build_worker_context`` hoists the
# newest note above the attempt history; this module imports ``kanban_db``
# late (bottom of file), so they are referenced through ``_kb`` at call time.


def _worker_log_path(task_id: str) -> Optional[str]:
    """Per-task worker log path if it exists on disk (TUI-rendered stdout)."""
    try:
        p = _kb.worker_logs_dir() / f"{task_id}.log"
    except Exception:
        return None
    return str(p) if p.exists() else None


def _write_resume_note(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], *,
    outcome: str, error: Optional[str] = None,
) -> None:
    """Insert the idempotent resume-note comment for ``run_id``.

    Idempotency: one note per run. A re-sweep finds no live ``running`` row so
    never gets here twice for the same run; the marker-scan guard makes that
    structural instead of relying on the caller's discipline. Never raises —
    the note is evidence, not bookkeeping that must succeed.
    """
    try:
        if run_id is not None and conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND body LIKE ?",
            (task_id, _kb.RESUME_NOTE_AUTHOR, f"%{_kb.RESUME_NOTE_MARKER}%run {run_id}%"),
        ).fetchone():
            return
        row = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        workspace_path = _kb._row_get(row, "workspace_path") if row is not None else None
        # Base SHA, opportunistic: the just-closed run's metadata (written by
        # ``_end_run`` immediately before this call), else the ``tasks`` row
        # when a future schema adds it (``_row_get`` is column-tolerant).
        # Neither present -> the note builder derives it from
        # ``git merge-base HEAD <default branch>``.
        base_sha = None
        meta = None
        if run_id is not None:
            run_row = conn.execute(
                "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
            ).fetchone()
            meta = _kb._json_or(run_row["metadata"]) if run_row is not None else None
        if not isinstance(meta, dict) and row is not None:
            meta = _kb._json_or(_kb._row_get(row, "metadata"))
        if isinstance(meta, dict):
            candidate = meta.get("base_sha") or meta.get("merge_base")
            if isinstance(candidate, str) and candidate.strip():
                base_sha = candidate.strip()
        note_body = _kb.build_resume_note(
            workspace_path, base_sha=base_sha,
            run={"outcome": outcome, "error": error},
            log_path=_worker_log_path(task_id),
        )
        body = (
            f"{_kb.RESUME_NOTE_MARKER} — run {run_id} closed as ``{outcome}``. "
            f"Resume from this state; do not redo committed work.\n\n{note_body}"
        )
        # Plain INSERT (not add_comment): caller holds the reclaim txn and
        # add_comment would open a nested write_txn + emit a duplicate
        # ``commented`` event per reclaim. No event append either: consumers
        # (tests, ``watch``) treat the closing outcome event as the last
        # transition event; the note is a comment, not a transition.
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, _kb.RESUME_NOTE_AUTHOR, body, int(time.time())),
        )
    except Exception:
        # Best-effort by contract; a note failure must never abort a reclaim.
        pass


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (PR URL in a recent comment; re-spawning risks a duplicate PR). The review
    lane skips the last two: they are the *inputs* to a review handoff. Stale /
    dead claim locks are NOT a guard reason — the reclaim passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        if c["body"] and _RESPAWN_GUARD_PR_URL_RE.search(c["body"]):
            return "active_pr"

    return None


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee)."""
    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        return None
    return profile_exists


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def spawnable_ready_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of ready+assigned+unclaimed tasks whose assignee is a real profile
    (all assigned ones when ``profile_exists`` is unimportable). Health
    telemetry compares this with a tick's cap deferrals: a queue that is
    entirely cap-deferred is waiting, not stuck."""
    rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL AND assignee != '' "
        "AND claim_lock IS NULL"
    ).fetchall()
    profile_exists = _profile_exists_fn()
    return {
        row["id"] for row in rows
        if profile_exists is None or profile_exists(row["assignee"])
    }


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def parse_assignee_caps(raw: Any) -> dict[str, int]:
    """Normalise per-assignee caps to ``{assignee: positive int}``.

    Accepts the ``kanban.max_in_progress_by_assignee`` mapping (``{evo: 2}``)
    or the CLI's repeated ``NAME=N`` strings (later pairs win). Invalid entries
    (empty name, non-integer, bool, below 1) are dropped rather than raising:
    a typo must not brick dispatch, and a dropped entry falls back to the
    scalar ``max_in_progress_per_profile``.
    """
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw, Mapping):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and "=" in item:
                name, _, value = item.partition("=")
                pairs.append((name, value))
    caps: dict[str, int] = {}
    for name, value in pairs:
        name = str(name).strip() if name is not None else ""
        if not name or isinstance(value, bool):
            continue
        try:
            ival = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if ival >= 1:
            caps[name] = ival
    return caps


def _strict_int(value: Any, *, minimum: int) -> Optional[int]:
    """``int(value)`` when it is an integer (or integer string) >= ``minimum``;
    None otherwise. Bools are rejected (``True`` is not a cap of 1)."""
    if isinstance(value, bool):
        return None
    try:
        ival = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ival if ival >= minimum else None


def parse_assignee_groups(raw: Any) -> dict[str, dict]:
    """Normalise ``kanban.assignee_groups`` to ``{group: {"members": [..], "max": N}}``.

    Profiles that share one GPU / local model server are capped together: the
    group's running count is the host-wide sum over its members. ``members``
    is a list of profile names (a comma-separated string is accepted);
    duplicates and blanks are dropped, order is kept. A group with no valid
    member or no positive integer ``max`` is dropped — a config typo must
    never brick dispatch.
    """
    if not isinstance(raw, Mapping):
        return {}
    groups: dict[str, dict] = {}
    for name, spec in raw.items():
        name = str(name).strip() if name is not None else ""
        if not name or not isinstance(spec, Mapping):
            continue
        cap = _strict_int(spec.get("max"), minimum=1)
        raw_members = spec.get("members")
        if isinstance(raw_members, str):
            raw_members = raw_members.split(",")
        if cap is None or not isinstance(raw_members, (list, tuple)):
            continue
        members: list[str] = []
        for member in raw_members:
            if not isinstance(member, str):
                continue
            member = member.strip()
            if member and member not in members:
                members.append(member)
        if members:
            groups[name] = {"members": members, "max": cap}
    return groups


def parse_capped_alert(raw: Any) -> Optional[tuple[int, list[str]]]:
    """Normalise ``kanban.capped_alert`` to ``(after_minutes, argv)`` or None (off).

    ``after_minutes`` is a non-negative integer; ``command`` is an argv list of
    strings (a plain string is split with :func:`shlex.split`). Anything else
    disables the alert rather than raising.
    """
    if not isinstance(raw, Mapping):
        return None
    after = _strict_int(raw.get("after_minutes"), minimum=0)
    command = raw.get("command")
    if isinstance(command, str):
        import shlex
        try:
            command = shlex.split(command)
        except ValueError:
            return None
    if after is None or not isinstance(command, (list, tuple)) or not command:
        return None
    if not all(isinstance(part, str) and part for part in command):
        return None
    return after, list(command)


def _kanban_config_readonly() -> Optional[dict]:
    """The ``kanban:`` config section (mtime-cached load), or None when the
    config cannot be loaded."""
    try:
        from hermes_cli.config import load_config_readonly
        return (load_config_readonly() or {}).get("kanban", {}) or {}
    except Exception:
        return None


def configured_per_assignee_caps() -> tuple[Optional[int], dict[str, int]]:
    """``(kanban.max_in_progress_per_profile, kanban.max_in_progress_by_assignee)``
    from config. Read per tick by the standalone daemon (config load is
    mtime-cached) so operators can retune a saturated local model without a
    restart (#83)."""
    kanban_cfg = _kanban_config_readonly()
    if kanban_cfg is None:
        return None, {}
    scalar = _positive_int(kanban_cfg.get("max_in_progress_per_profile"), None)
    return scalar, parse_assignee_caps(kanban_cfg.get("max_in_progress_by_assignee"))


def configured_assignee_groups() -> dict[str, dict]:
    """``kanban.assignee_groups`` from config, normalised; re-read every daemon tick."""
    kanban_cfg = _kanban_config_readonly()
    return parse_assignee_groups(kanban_cfg.get("assignee_groups")) if kanban_cfg else {}


def configured_capped_alert() -> Optional[tuple[int, list[str]]]:
    """``kanban.capped_alert`` from config, normalised; None when off/invalid."""
    kanban_cfg = _kanban_config_readonly()
    return parse_capped_alert(kanban_cfg.get("capped_alert")) if kanban_cfg else None


def configured_max_runtime_by_assignee() -> dict[str, int]:
    """``kanban.max_runtime_by_assignee`` as ``{canonical assignee: seconds}``.

    Normalised with :func:`parse_assignee_caps` (invalid or < 1 entries dropped)
    and keyed by the same lowercase canonical name ``create_task`` stores, so
    ``Kimi: 600`` matches a card assigned to ``kimi``. Empty when unset or the
    config cannot be read: no config means creation is unchanged.
    """
    # Board-level: profile value when set, else the root config (see kanban_board_setting).
    raw = parse_assignee_caps(_kb.kanban_board_setting("max_runtime_by_assignee", {}))
    out: dict[str, int] = {}
    for name, seconds in raw.items():
        try:
            key = _kb._canonical_assignee(name)
        except Exception:
            key = name
        if key:
            out[key] = seconds
    return out


def default_max_runtime_for(assignee: Optional[str]) -> Optional[int]:
    """The configured default max runtime (seconds) for a card assigned to
    ``assignee`` (already canonical), or ``None`` when it has none."""
    if not assignee:
        return None
    return configured_max_runtime_by_assignee().get(assignee)


def configured_max_spawn() -> Optional[int]:
    """``kanban.max_spawn`` from config, or None when unset/invalid."""
    try:
        from hermes_cli.config import load_config_readonly
        raw = ((load_config_readonly() or {}).get("kanban", {}) or {}).get("max_spawn")
    except Exception:
        return None
    return _positive_int(raw, None)


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def _for_each_other_board(board: Optional[str], fn: Callable[[sqlite3.Connection], None]) -> None:
    """Call ``fn(conn)`` on every non-archived board EXCEPT ``board``.

    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) visits nothing. Fails open: a board that cannot be
    enumerated, opened or read is skipped — never brick dispatch on healthy
    boards because one is broken.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                fn(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Fails open per board (see :func:`_for_each_other_board`).
    """
    totals = [0]

    def _add(conn: sqlite3.Connection) -> None:
        totals[0] += count_running_tasks(conn)

    _for_each_other_board(board, _add)
    return totals[0]


def count_running_by_assignee(conn: sqlite3.Connection) -> dict[str, int]:
    """``{assignee: running count}`` for one board. Raises on a broken DB;
    the host-wide wrapper decides whether to fail open."""
    return {
        row["assignee"]: int(row["n"])
        for row in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        )
    }


def count_running_by_assignee_host(
    conn: sqlite3.Connection, board: Optional[str] = None,
) -> dict[str, int]:
    """``{assignee: running count}`` summed over THIS board (``conn``) and every
    other board on the host.

    Per-assignee, per-profile and group caps protect a shared resource (one
    GPU, one API quota), not a board: without the other boards, two boards
    each running ``cap`` evo workers put ``2 * cap`` on one llama-server.
    Other boards fail open (skipped when unreadable).
    """
    counts = count_running_by_assignee(conn)

    def _add(other: sqlite3.Connection) -> None:
        for name, n in count_running_by_assignee(other).items():
            counts[name] = counts.get(name, 0) + n

    _for_each_other_board(board, _add)
    return counts


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


class BoardDispatchLockedError(RuntimeError):
    """Another dispatcher holds this board's dispatch lock; the tick was
    refused without any writes. One-shot callers (CLI ``dispatch``) raise this
    instead of silently racing the board's daemon."""
    pass


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    on_locked: Optional[str] = None,
    max_in_progress_by_assignee: Optional[Mapping[str, int]] = None,
    assignee_groups: Optional[Mapping[str, Any]] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.

    ``on_locked="raise"`` makes the losing case raise
    :class:`BoardDispatchLockedError` instead — for one-shot callers (the CLI
    ``dispatch`` command) a silent no-op tick is indistinguishable from
    success and invites exactly the stray-tick races the lock exists to
    prevent; failing loudly points the operator at the running daemon.
    """
    def _locked_tick() -> DispatchResult:
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            reconcile_orphans=reconcile_orphans,
            max_in_progress_by_assignee=max_in_progress_by_assignee,
            assignee_groups=assignee_groups,
        )

    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception:
        # Must not lose the tick — fall through to an unguarded dispatch.
        result = _locked_tick()
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            if on_locked == "raise":
                raise BoardDispatchLockedError(
                    f"another dispatcher holds the dispatch lock for {db_path} "
                    "(is the kanban daemon or gateway watcher running for this "
                    "board?); this one-shot tick was refused without any writes"
                )
            result = DispatchResult(skipped_locked=True)
        else:
            result = _locked_tick()
            # Still under the dispatch lock: periodic PASSIVE WAL checkpoint.
            _kbc._maybe_checkpoint_wal(conn, db_path)
    # Lock released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _stamp_attempt_base_head(conn: sqlite3.Connection, task_id: str) -> None:
    """Record the workspace's current git head on the freshly-opened run.

    The claim-time boundary for the run-end evidence harvest: attempt N lists
    ``base_head..HEAD`` so a retry never re-credits attempt N-1's commits.
    Best-effort — no workspace/git yet (scratch dir, spawn about to fail)
    simply leaves the metadata key absent and the harvest falls back to the
    prior-run boundary. Runs in its own tiny txn AFTER the claim committed.
    """
    try:
        from hermes_cli import kanban_harvest as _harvest

        row = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None or not row["workspace_path"]:
            return
        head = _harvest.workspace_head(row["workspace_path"])
        if not head:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is None:
            return
        with _kb.write_txn(conn):
            metadata = _kb._json_dict(
                conn.execute(
                    "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
                ).fetchone()["metadata"],
            )
            metadata["workspace_head_at_claim"] = head
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (_kb._json_or_null(metadata), run_id),
            )
    except Exception:
        # Boundary stamping is an optimisation for precise commit ranges;
        # failure must never affect the spawn path.
        pass


def _cap_hit(
    assignee: str,
    per_profile_cap: Optional[int],
    assignee_caps: Optional[Mapping[str, int]],
    assignee_groups: Optional[Mapping[str, Mapping]],
    running_by_assignee: Mapping[str, int],
) -> Optional[tuple[int, int, str]]:
    """``(running, cap, scope)`` of the first full cap that defers ``assignee``,
    or None when it has headroom. The assignee's own cap (named, else the
    scalar) is checked first, then every group containing it in name order;
    a group's running count is the sum over its members."""
    own_cap = (assignee_caps or {}).get(assignee, per_profile_cap)
    if own_cap is not None:
        current = running_by_assignee.get(assignee, 0)
        if current >= own_cap:
            return current, own_cap, "assignee"
    for name in sorted(assignee_groups or {}):
        spec = assignee_groups[name]
        members = spec.get("members") or ()
        if assignee not in members:
            continue
        group_running = sum(running_by_assignee.get(m, 0) for m in members)
        if group_running >= spec["max"]:
            return group_running, int(spec["max"]), f"group:{name}"
    return None


def _capped_episode_open(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the task's latest event (ignoring ``capped_alerted``) is
    ``capped``: it is still in the wait episode that event started. Any other
    event since (claim, spawn, status change, ...) ended that episode."""
    row = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind != 'capped_alerted' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None and row["kind"] == "capped"


def _record_capped_event(conn: sqlite3.Connection, task_id: str, payload: dict) -> None:
    """Append a ``capped`` event at most once per wait episode. Best-effort:
    visibility must never fail the dispatch tick."""
    try:
        if _capped_episode_open(conn, task_id):
            return
        with _kb.write_txn(conn):
            if not _capped_episode_open(conn, task_id):
                _kb._append_event(conn, task_id, "capped", payload)
    except Exception:
        _kb._log.debug("kanban dispatch: failed to record capped event for %s",
                       task_id, exc_info=True)


def check_capped_alerts(
    conn: sqlite3.Connection,
    *,
    board: str,
    after_minutes: int,
    command: list[str],
    now: Optional[int] = None,
) -> list[str]:
    """Run ``command`` once for ready tasks whose cap wait is too long.

    A task qualifies when it is ``ready``, its open wait episode (see
    :func:`_capped_episode_open`) began more than ``after_minutes`` ago, and no
    ``capped_alerted`` event follows that episode's ``capped`` event. The
    command gets ``{"board", "alerts": [{"task_id", "title", "assignee",
    "scope", "waiting_minutes"}]}`` as JSON on stdin and 30 s to finish. Every
    alerted task then gets a ``capped_alerted`` event — even if the command
    failed, so a broken command cannot run (and time out) every tick. Never
    raises; returns the alerted task ids. Generic by design: hermes sends no
    Discord/HTTP itself, the operator's command does.
    """
    try:
        now = int(time.time()) if now is None else int(now)
        cutoff = now - int(after_minutes) * 60
        rows = conn.execute(
            "SELECT t.id, t.title, t.assignee, e.payload, e.created_at "
            "FROM tasks t JOIN task_events e ON e.task_id = t.id "
            "WHERE t.status = 'ready' AND e.kind = 'capped' AND e.created_at < ? "
            "AND e.id = (SELECT MAX(x.id) FROM task_events x "
            "            WHERE x.task_id = t.id AND x.kind != 'capped_alerted') "
            "AND NOT EXISTS (SELECT 1 FROM task_events a WHERE a.task_id = t.id "
            "                AND a.kind = 'capped_alerted' AND a.id > e.id) "
            "ORDER BY e.created_at ASC, t.id ASC",
            (cutoff,),
        ).fetchall()
    except Exception:
        _kb._log.warning("kanban capped_alert: query failed", exc_info=True)
        return []
    if not rows:
        return []
    import json

    alerts = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        alerts.append({
            "task_id": row["id"],
            "title": row["title"],
            "assignee": payload.get("assignee") or row["assignee"],
            "scope": payload.get("scope") or "assignee",
            "waiting_minutes": max(0, (now - int(row["created_at"])) // 60),
        })
    body = json.dumps({"board": board, "alerts": alerts})
    try:
        proc = subprocess.run(
            list(command), input=body, text=True, capture_output=True,
            timeout=30, check=False,
        )
        if proc.returncode != 0:
            _kb._log.warning(
                "kanban capped_alert: command %r exited %s: %s",
                command[0], proc.returncode, (proc.stderr or "").strip()[-500:],
            )
    except Exception as exc:
        _kb._log.warning("kanban capped_alert: command %r failed: %s", command[0], exc)
    alerted = [a["task_id"] for a in alerts]
    try:
        with _kb.write_txn(conn):
            for tid in alerted:
                _kb._append_event(conn, tid, "capped_alerted")
    except Exception:
        _kb._log.warning("kanban capped_alert: could not record capped_alerted", exc_info=True)
    return alerted


def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: "DispatchResult",
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
    assignee_caps: Optional[Mapping[str, int]] = None,
    assignee_groups: Optional[Mapping[str, Mapping]] = None,
    allowed_assignees: Optional[frozenset] = None,
    board_slug: Optional[str] = None,
    allowlist_unreadable: Optional[str] = None,
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``. ``assignee_groups`` must already be
    normalised by :func:`parse_assignee_groups`.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        return False
    # Board allowlist (board.json ``allowed_assignees``): the last line of defence for
    # cards that reached the lane without passing create (reassignment, a parked card
    # unblocked, default_assignee, a direct DB write).
    if allowlist_unreadable is not None:
        # Fail closed for this tick only. The policy could not be read (a board.json
        # caught mid-write, a hand-edit typo), which is not a decision about this card:
        # leave it in its lane and retry next tick, never block.
        result.skipped_board_unreadable.append((task_id, assignee))
        return False
    refusal = _kb.assignee_not_allowed_message(
        assignee, allowed_assignees, board_slug or board or _kb.DEFAULT_BOARD)
    if refusal is not None:
        result.skipped_assignee_not_allowed.append((task_id, assignee))
        if not dry_run:
            _refuse_disallowed_assignee(conn, task_id, assignee, refusal, lane=lane)
        return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    hit = _cap_hit(assignee, per_profile_cap, assignee_caps, assignee_groups, per_profile_running)
    if hit is not None:
        running, cap, scope = hit
        result.skipped_per_profile_capped.append((task_id, assignee, running))
        payload = {"assignee": assignee, "running": running, "cap": cap, "scope": scope}
        result.capped_details.append({"task_id": task_id, **payload})
        if not dry_run:
            _record_capped_event(conn, task_id, payload)
        return False
    guard_reason = check_respawn_guard(conn, task_id, lane=lane)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "respawn_guarded", {"reason": guard_reason})
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile and group caps
        # (group running is summed from these counts); subsequent ticks
        # re-query from the DBs.
        if name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        return False
    # Stamp the attempt's evidence boundary before the worker starts: the
    # harvest at run end lists workspace_head_at_claim..HEAD so a retry
    # never re-credits the prior attempt's commits (trust wave #9).
    _stamp_attempt_base_head(conn, claimed.id)
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, f"workspace: {exc}",
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        pid = _call_spawn_fn(spawn_fn if spawn_fn is not None else _default_spawn, claimed, str(workspace), board)
        if pid:
            _set_worker_pid(conn, claimed.id, int(pid))
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        if _record_task_failure(
            conn, claimed.id, str(exc),
            outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def _refuse_disallowed_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, reason: str, *, lane: str,
) -> None:
    """Record why a card was not spawned: an ``assignee_not_allowed`` event and a
    block with the reason (``kind=capability``: a human must reassign it or widen
    the allowlist), so the refusal is durable and is not re-evaluated every tick.
    A review card is held (``source_status=review``): left in ``review`` it would
    stay a spawnable review forever, reserve a slot from the ready lane on every
    tick and trip the gateway's "dispatcher stuck" warning."""
    payload = {"assignee": assignee, "lane": lane, "reason": reason}
    try:
        last = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if last is None or last["kind"] != "assignee_not_allowed":
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "assignee_not_allowed", payload)
        _kb.block_task(conn, task_id, reason=reason, kind="capability", hold_review=lane == "review")
    except Exception:
        _kb._log.warning(
            "kanban dispatch: could not record assignee_not_allowed for %s", task_id, exc_info=True)


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.
    """
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reclaimed = _kb.release_stale_claims(conn)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn, board=board)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.output_limit.extend(getattr(detect_crashed_workers, "_last_output_limit", []))
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker calls
    # kanban_complete/kanban_block or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def _any_spawnable_review(
    review_rows: list[sqlite3.Row], allowed_assignees: Optional[frozenset] = None,
    board_slug: str = "",
) -> bool:
    """Mirrors the review loop's own gate so human-pulled control-plane lanes
    don't tax ready throughput; assumes spawnable when profiles are unimportable.
    A card the board allowlist refuses never spawns (the review loop holds it),
    so it reserves no slot either."""
    rows = [
        row for row in review_rows
        if row["assignee"] and _kb.assignee_not_allowed_message(
            row["assignee"], allowed_assignees, board_slug) is None
    ]
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile. When the
    profiles module isn't importable trust the operator's config: the
    downstream profile_exists check still buckets a missing profile as
    nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        try:
            from hermes_cli.profiles import profile_exists
            if not profile_exists(name):
                return None
        except Exception:
            pass
    return name


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    max_in_progress_by_assignee: Optional[Mapping[str, int]] = None,
    assignee_groups: Optional[Mapping[str, Any]] = None,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`."""
    result = DispatchResult()
    # Model truth (#7): surface override-vs-answered mismatches as card
    # comments. Runs before any spawn/reclaim so a tick that only reconciles
    # still notifies; idempotent per run.
    with contextlib.suppress(Exception):
        notify_model_fallbacks(conn)
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans,
        board=board,
    )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    # Board allowlist, read once per tick from the board this connection actually has open.
    board_slug = _kb.board_slug_for_conn(conn, board)
    allowed_assignees, allowlist_unreadable = _kb.board_allowlist_state(board=board_slug)
    if allowlist_unreadable is not None:
        result.board_allowlist_unreadable = allowlist_unreadable
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(
            review_rows, allowed_assignees, board_slug):
        ready_budget = max(spawn_budget - 1, 0)
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    # Named per-assignee caps override the scalar for those profiles only (#83): a single-GPU local
    # profile can be held at 2 while cloud profiles keep fanning out.
    assignee_caps = parse_assignee_caps(max_in_progress_by_assignee)
    # Groups cap profiles that share one GPU / model server together.
    groups = parse_assignee_groups(assignee_groups)
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None or assignee_caps or groups:
        # Host-wide: the capped resource is shared by every board on the host.
        per_profile_running = count_running_by_assignee_host(conn, board)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        assignee_caps=assignee_caps, assignee_groups=groups,
        allowed_assignees=allowed_assignees, board_slug=board_slug,
        allowlist_unreadable=allowlist_unreadable,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    spawned = 0
    for row in ready_rows:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
            spawned += 1
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package)."""
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then ``which("hermes")`` (Windows: safe PATH search,
    batch shims fall back to the module form), then ``sys.executable -m
    hermes_cli.main`` for shim-less environments (cron, systemd ``User=``,
    launchd). Mirrors ``gateway.run._resolve_hermes_bin``; local because
    ``hermes_cli`` sits below ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from agent.secret_scope import (
            build_profile_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope)
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        token = set_hermes_home_override(hermes_home)
        # Toolset availability probes read credentials (``get_secret``); under multiplex an
        # unscoped read raises and the pin was silently dropped for every worker.
        secret_token = (
            set_secret_scope(build_profile_secret_scope(Path(hermes_home)))
            if is_multiplex_active() else None)
        try:
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        finally:
            if secret_token is not None:
                reset_secret_scope(secret_token)
            reset_hermes_home_override(token)
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            db.close()
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    # An output-limit respawn must do paperwork, not exploration: when the
    # latest failure carries the truncation marker, prefix the terse-complete
    # resume note onto the prompt (``last_failure_error`` survives the claim;
    # only a profile reassign or unblock clears it).
    prompt = f"work kanban task {task.id}"
    _lfe = (task.last_failure_error or "")
    if (
        _lfe.startswith("worker hit the output length limit")
        or "output length limit" in _lfe[:120]
    ):
        prompt = _output_limit_resume_note_prompt(prompt)
    cmd.extend(["chat", "-q", prompt])
    if task.goal_mode:
        # The kanban goal-loop hook only runs in cli.py's fully-quiet branch.
        # Without -Q the worker gets one turn, prints text, exits rc=0, and the
        # dispatcher records a protocol violation.
        cmd.append("-Q")
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a managed-gateway worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs, so they never take cron's
    degraded mode: ``require_restart_safe_scope=True`` makes the helper raise.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
    ).argv


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import build_subprocess_env, strip_launch_profile_env

    env = build_subprocess_env(
        scrub_secrets=is_multiplex_active(),
        inherit_profile_home=True,
    )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
        # A multiplexer dispatching for another profile must not hand it the launch
        # profile's .env settings / TERMINAL_* policy — a standalone dispatcher never would.
        strip_launch_profile_env(env, env["HERMES_HOME"])
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        pass
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # A worker spawned by a managed systemd gateway must leave the gateway's
    # cgroup before startup; otherwise restarting the service kills the worker
    # that is performing the handoff.
    cmd = _restart_safe_worker_argv(task, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    log_f = _open_worker_log(task, board)
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def _run_capped_alert(conn: sqlite3.Connection, after_minutes: int, command: list[str]) -> None:
    """Daemon-tick wrapper around :func:`check_capped_alerts`; never raises."""
    try:
        try:
            board = _kb.get_current_board()
        except Exception:
            board = _kb.DEFAULT_BOARD
        check_capped_alerts(conn, board=board, after_minutes=after_minutes, command=command)
    except Exception:
        _kb._log.warning("kanban capped_alert: tick check failed", exc_info=True)


def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
    max_in_progress_by_assignee: Optional[Mapping[str, int]] = None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.
    """
    import threading

    if stop_event is None:
        stop_event = threading.Event()
    cli_assignee_caps = parse_assignee_caps(max_in_progress_by_assignee)

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            # Per-profile caps re-resolve every tick too (#83). Explicit CLI
            # caps win per assignee; an explicit ``--max`` wins over
            # ``kanban.max_spawn``, otherwise the config value is live.
            per_profile, by_assignee = configured_per_assignee_caps()
            by_assignee.update(cli_assignee_caps)
            tick_max_spawn = max_spawn if max_spawn is not None else configured_max_spawn()
            # GPU-sharing groups and the long-wait alert are live too.
            groups = configured_assignee_groups()
            capped_alert = configured_capped_alert()
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=tick_max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    max_in_progress_per_profile=per_profile,
                    max_in_progress_by_assignee=by_assignee,
                    assignee_groups=groups,
                )
                if capped_alert is not None:
                    _run_capped_alert(conn, *capped_alert)
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb  # noqa: E402
from hermes_cli import kanban_db_connect as _kbc  # noqa: E402
from hermes_cli import kanban_db_workspace as _kbw  # noqa: E402
