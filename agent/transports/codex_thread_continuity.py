"""Durable Codex thread continuity for the codex app-server runtime.

The app-server runtime spawns one `codex app-server` subprocess per AIAgent
instance and calls ``thread/start`` on it (see
``codex_app_server_session.CodexAppServerSession.ensure_started``). That is
correct for a fresh Hermes session and wrong for an existing one: when the
Hermes process restarts (service restart, gateway respawn, crash recovery) the
same Hermes session came back with a *brand new* Codex thread, so everything the
user said before the restart was gone from the model's own context even though
codex had persisted the rollout on disk and supports ``thread/resume``.

This module is the persistence + ownership layer that closes that gap:

* :class:`CodexThreadRecord` — exactly what must survive a process restart:
  the thread id plus the runtime identity it was started under (cwd, model,
  permission profile) and the owning process.
* :class:`SessionModelConfigThreadStore` — the concrete store, writing into the
  existing per-session ``model_config`` JSON in the session DB via
  ``patch_session_model_config`` (no schema change, atomic merge).
* :class:`CodexThreadContinuity` — the small policy object the session uses:
  load / claim / record / release, with explicit refusals.

Design rules this implements (all of them are behaviour the tests lock in):

1. **Exact id or nothing.** A new process for the same Hermes session resumes
   the exact saved thread id. If ``thread/resume`` fails (fabricated id, rollout
   deleted, codex refuses), we raise :class:`CodexThreadResumeError`. We never
   silently fall back to ``thread/start`` — that is the failure mode that made
   the bug invisible in the first place.
2. **A fresh Hermes session gets a fresh thread.** Continuity is keyed by the
   Hermes session id; no record means ``thread/start``.
3. **One owner at a time.** A record carries the owning pid, boot id and host
   id. A *live* owner on the same host+boot means
   :class:`CodexThreadOwnershipError`. Same host with a *different* boot means
   the predecessor was killed by a reboot — a legitimate takeover, so an Evo
   service that comes back after a cold reboot reaches its own conversation.
   A *different host* with no explicit release is refused outright: its pid
   cannot be probed, so we cannot prove that owner is gone.
4. **Runtime identity must match.** cwd / permission profile are part of the
   record. A resume request under a different cwd or profile is refused
   explicitly (:class:`CodexThreadIdentityMismatch`) rather than silently
   resuming a thread whose sandbox roots no longer match.
5. **Persistence failure is loud and fatal, never silent.** Store errors
   propagate as :class:`CodexThreadPersistenceError`. On BOTH the resume path
   and the record-started path this aborts the turn: a thread we could not
   record is a thread the next restart will lose, so the user must not be
   allowed to build a conversation on top of it believing it is durable.
6. **Reservation before decision.** A per-session ``flock`` is taken before the
   record is even read and held for the object's lifetime, so two processes
   racing on the same session cannot both conclude "no owner, start fresh".
7. **Host vs boot are distinct.** A different *boot* on the same host proves the
   predecessor died and permits an exact resume; a different *host* without an
   explicit release is refused. Comparing boot alone would lock a rebooted Evo
   host out of its own conversation forever.

Nothing here talks to codex. The RPC is issued by the session, which owns the
client; this module owns *what may be resumed and by whom*.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Optional, Protocol

logger = logging.getLogger(__name__)

# Key inside the session row's model_config JSON blob.
THREAD_RECORD_KEY = "codex_thread"

# Record schema version, so a future shape change can be detected instead of
# mis-parsed into a resume attempt with missing fields.
RECORD_VERSION = 1


class CodexThreadContinuityError(RuntimeError):
    """Base for every explicit continuity refusal."""


class CodexThreadResumeError(CodexThreadContinuityError):
    """The saved thread exists but could not be resumed (codex refused it)."""


class CodexThreadOwnershipError(CodexThreadContinuityError):
    """Another live process currently owns this thread."""


class CodexThreadIdentityMismatch(CodexThreadContinuityError):
    """The saved thread was started under a different cwd/permission profile."""


class CodexThreadPersistenceError(CodexThreadContinuityError):
    """The continuity store could not be read or written."""


def _boot_id() -> str:
    """Identify this machine boot, so a pid recycled after a reboot cannot be
    mistaken for the original live owner.

    Boot identity alone must NOT be used to decide ownership across machines —
    a reboot of the SAME host changes it, and treating that as "foreign" would
    strand the thread forever. Pair it with :func:`_host_id`.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        # Non-Linux / restricted: no boot identity available. Return empty so
        # callers fall back to pid liveness rather than inventing a value that
        # would look like a different boot on every call.
        return ""


def _host_id() -> str:
    """Stable identity of THIS machine, surviving reboots.

    Prefers /etc/machine-id (systemd, stable for the life of the install),
    falling back to the hostname. Used to distinguish "same host, new boot"
    (predecessor is provably dead → resume allowed) from "different host"
    (predecessor unknowable → refuse).
    """
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    try:
        return os.uname().nodename
    except Exception:  # pragma: no cover - defensive
        return ""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else.
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


@dataclass
class CodexThreadRecord:
    """Everything needed to resume a Codex thread in a later process."""

    thread_id: str
    cwd: str = ""
    model: str = ""
    permission_profile: str = ""
    owner_pid: int = 0
    owner_boot_id: str = ""
    owner_host_id: str = ""
    claimed_at: float = 0.0
    released_at: float = 0.0
    version: int = RECORD_VERSION
    # Free-form continuity notes (e.g. "recovered from crashed pid 123").
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["CodexThreadRecord"]:
        if not isinstance(raw, dict):
            return None
        thread_id = raw.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            return None
        version = raw.get("version")
        if not isinstance(version, int) or version > RECORD_VERSION:
            logger.warning(
                "codex continuity: ignoring thread record with unsupported "
                "version %r (this build understands <= %d)",
                version,
                RECORD_VERSION,
            )
            return None
        def _s(key: str) -> str:
            value = raw.get(key)
            return value if isinstance(value, str) else ""

        def _f(key: str) -> float:
            value = raw.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        owner_pid = raw.get("owner_pid")
        return cls(
            thread_id=thread_id,
            cwd=_s("cwd"),
            model=_s("model"),
            permission_profile=_s("permission_profile"),
            owner_pid=owner_pid if isinstance(owner_pid, int) else 0,
            owner_boot_id=_s("owner_boot_id"),
            owner_host_id=_s("owner_host_id"),
            claimed_at=_f("claimed_at"),
            released_at=_f("released_at"),
            version=version,
            note=_s("note"),
        )

    # ---- ownership ----

    def is_released(self) -> bool:
        return bool(self.released_at) and self.released_at >= self.claimed_at

    def owner_is_live(self) -> bool:
        """True only when the recorded owner is a live process on THIS boot.

        Requires same host AND same boot: only then is the recorded pid a
        meaningful handle in this namespace. Anything else is judged by
        :meth:`owner_is_foreign` / :meth:`predecessor_died_at_reboot`.
        """
        if self.is_released():
            return False
        if not self.owner_pid:
            return False
        if self.owner_host_id and self.owner_host_id != _host_id():
            return False
        if self.owner_boot_id and self.owner_boot_id != _boot_id():
            return False
        if self.owner_pid == os.getpid():
            return False
        return _pid_alive(self.owner_pid)

    def predecessor_died_at_reboot(self) -> bool:
        """True for "same host, different boot" — a legitimate takeover.

        A reboot of this machine provably killed the recorded process, so its
        claim is stale and the exact thread may be resumed. This is the
        approved reboot-persistence case: an Evo service that comes back after
        a cold reboot must reach its own conversation, not be locked out of it.
        """
        if self.is_released():
            return False
        if not self.owner_host_id or not self.owner_boot_id:
            return False
        if self.owner_host_id != _host_id():
            return False
        return self.owner_boot_id != _boot_id()

    def owner_is_foreign(self) -> bool:
        """True when the owner is on a DIFFERENT host and never released.

        A pid on another machine cannot be probed, so we cannot prove that
        owner is gone; refusing is the only safe answer. Note this is a host
        comparison, deliberately NOT a boot comparison: a reboot of the same
        host is :meth:`predecessor_died_at_reboot`, which is allowed.

        The only ways past this are an explicit release written by that owner
        or an operator calling :meth:`CodexThreadContinuity.invalidate`.
        """
        if self.is_released():
            return False
        if not self.owner_host_id:
            # No host identity recorded (older record or restricted host):
            # fall back to same-boot pid liveness, which is all we have.
            return False
        return self.owner_host_id != _host_id()


class CodexThreadStore(Protocol):
    """Minimal persistence surface. Implementations must raise on failure."""

    def load(self) -> Optional[CodexThreadRecord]: ...

    def save(self, record: CodexThreadRecord) -> None: ...

    def clear(self) -> None: ...


class SessionModelConfigThreadStore:
    """Store the record in the session row's ``model_config`` JSON.

    Uses the existing atomic merge helpers on ``SessionDB``
    (``patch_session_model_config`` / ``get_session_model_config_value``) so
    lineage markers in that blob are preserved and no migration is needed.
    """

    def __init__(self, session_db: Any, session_id: str) -> None:
        if session_db is None or not session_id:
            raise ValueError("session_db and session_id are required")
        self._db = session_db
        self._session_id = session_id

    def lock_path(self) -> str:
        """Filesystem path used for the cross-process reservation.

        Lives beside the session DB so every process sharing that DB shares
        the same lock file, which is exactly the set of processes that could
        race for the same session's codex thread.
        """
        db_path = getattr(self._db, "db_path", None)
        base = os.path.dirname(os.path.abspath(str(db_path))) if db_path else ""
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".hermes")
        lock_dir = os.path.join(base, "codex-threads")
        os.makedirs(lock_dir, exist_ok=True)
        safe = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in self._session_id
        )
        return os.path.join(lock_dir, f"{safe}.lock")

    def load(self) -> Optional[CodexThreadRecord]:
        try:
            raw = self._db.get_session_model_config_value(
                self._session_id, THREAD_RECORD_KEY
            )
        except Exception as exc:
            raise CodexThreadPersistenceError(
                f"could not read codex thread record for session "
                f"{self._session_id}: {exc}"
            ) from exc
        return CodexThreadRecord.from_dict(raw)

    def save(self, record: CodexThreadRecord) -> None:
        try:
            self._db.patch_session_model_config(
                self._session_id, {THREAD_RECORD_KEY: record.to_dict()}
            )
        except Exception as exc:
            raise CodexThreadPersistenceError(
                f"could not persist codex thread record for session "
                f"{self._session_id}: {exc}"
            ) from exc

    def clear(self) -> None:
        try:
            self._db.patch_session_model_config(
                self._session_id, {THREAD_RECORD_KEY: None}
            )
        except Exception as exc:
            raise CodexThreadPersistenceError(
                f"could not clear codex thread record for session "
                f"{self._session_id}: {exc}"
            ) from exc


class CodexThreadContinuity:
    """Policy object the Codex session consults around start/resume/close.

    **Reservation, not check-then-act.** Two processes starting the same
    Hermes session simultaneously would both pass a naive load→check→save
    (neither has saved yet, so neither sees an owner) and both would call
    ``thread/start``, producing two threads and a lost conversation. So the
    first thing :meth:`load_resumable` does is take an exclusive, non-blocking
    ``flock`` on a per-session lock file, and it is held for the lifetime of
    this object (released in :meth:`release`). Losing that race is a
    :class:`CodexThreadOwnershipError` before any codex RPC is issued.

    The flock covers same-host races. Cross-host is covered separately by
    :meth:`CodexThreadRecord.owner_is_foreign`, which fails closed because a
    foreign pid cannot be probed.
    """

    def __init__(
        self,
        store: CodexThreadStore,
        *,
        cwd: str = "",
        permission_profile: str = "",
        model: str = "",
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._cwd = cwd
        self._permission_profile = permission_profile
        self._model = model
        self._now = now
        self._record: Optional[CodexThreadRecord] = None
        self._lock_fd: Optional[int] = None
        self._reserved = False

    # ---- reservation ----

    def reserve(self) -> None:
        """Take the exclusive per-session reservation, or raise.

        Idempotent within one object.

        **A store without ``lock_path`` is NOT production-safe.** Such a store
        gets record-level checks only, which cannot stop two processes that
        both start before either has saved. It is supported for tests and
        in-memory experiments; it is logged as unsupported for durable
        exclusive ownership so the gap can never be mistaken for a guarantee.
        """
        if self._reserved:
            return
        lock_path_fn = getattr(self._store, "lock_path", None)
        if lock_path_fn is None:
            logger.warning(
                "codex continuity: store %s provides no lock_path; durable "
                "exclusive thread ownership is UNSUPPORTED for this store "
                "(record-level checks only, concurrent starts are possible). "
                "Do not use in production.",
                type(self._store).__name__,
            )
            self._reserved = True
            return
        try:
            path = lock_path_fn()
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise CodexThreadPersistenceError(
                f"could not open codex thread reservation lock: {exc}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = ""
            try:
                holder = os.read(fd, 256).decode("utf-8", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            raise CodexThreadOwnershipError(
                "another live process already holds this Hermes session's "
                f"codex thread reservation{(' (' + holder + ')') if holder else ''}"
            ) from exc
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"pid={os.getpid()} boot={_boot_id()}".encode())
        except OSError:  # pragma: no cover - annotation only
            pass
        self._lock_fd = fd
        self._reserved = True

    def _release_reservation(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        self._reserved = False
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - defensive
            pass
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - defensive
            pass

    # ---- read side ----

    def load_resumable(self) -> Optional[CodexThreadRecord]:
        """Reserve the session, then return the record to resume (or None).

        Raises when this process may not proceed:
        :class:`CodexThreadOwnershipError` (reservation lost, live same-host
        owner, or an unreleased foreign-host owner) or
        :class:`CodexThreadIdentityMismatch` (different cwd / permission
        profile). Store failures raise :class:`CodexThreadPersistenceError`.

        The reservation is taken BEFORE the record is read and held until
        :meth:`release`, so the whole start-or-resume decision is serialised.
        """
        self.reserve()
        record = self._store.load()
        if record is None:
            return None
        if record.owner_is_foreign():
            raise CodexThreadOwnershipError(
                f"codex thread {record.thread_id} is claimed by a process on "
                f"another host ({record.owner_host_id[:12]}, pid "
                f"{record.owner_pid}) and was never released. Refusing: this "
                f"host cannot prove that owner is gone. (A reboot of THIS "
                f"host would be a legitimate takeover; a different host is "
                f"not.)"
            )
        if record.owner_is_live():
            raise CodexThreadOwnershipError(
                f"codex thread {record.thread_id} is owned by live process "
                f"{record.owner_pid}; refusing to drive the same thread from "
                f"two processes"
            )
        if self._cwd and record.cwd and os.path.normpath(
            record.cwd
        ) != os.path.normpath(self._cwd):
            raise CodexThreadIdentityMismatch(
                f"codex thread {record.thread_id} was started in "
                f"{record.cwd!r}, this process runs in {self._cwd!r}"
            )
        if (
            self._permission_profile
            and record.permission_profile
            and record.permission_profile != self._permission_profile
        ):
            raise CodexThreadIdentityMismatch(
                f"codex thread {record.thread_id} was started under "
                f"permission profile {record.permission_profile!r}, this "
                f"process requests {self._permission_profile!r}"
            )
        self._record = record
        return record

    # ---- write side ----

    def record_started(self, thread_id: str, *, model: str = "") -> CodexThreadRecord:
        """Persist a newly started thread and claim ownership."""
        record = CodexThreadRecord(
            thread_id=thread_id,
            cwd=self._cwd,
            model=model or self._model,
            permission_profile=self._permission_profile,
            owner_pid=os.getpid(),
            owner_boot_id=_boot_id(),
            owner_host_id=_host_id(),
            claimed_at=self._now(),
            released_at=0.0,
        )
        self._store.save(record)
        self._record = record
        return record

    def record_resumed(
        self, record: CodexThreadRecord, *, model: str = "", note: str = ""
    ) -> CodexThreadRecord:
        """Re-claim an existing thread after a successful ``thread/resume``.

        Records a continuity note when the previous claim was invalidated by a
        reboot of this same host, so the takeover is auditable rather than
        silent.
        """
        takeover_note = note or record.note
        if record.predecessor_died_at_reboot():
            takeover_note = (
                f"recovered after host reboot (previous owner pid "
                f"{record.owner_pid} on boot {record.owner_boot_id[:8]})"
            )
        claimed = CodexThreadRecord(
            thread_id=record.thread_id,
            cwd=record.cwd or self._cwd,
            model=model or record.model or self._model,
            permission_profile=(
                record.permission_profile or self._permission_profile
            ),
            owner_pid=os.getpid(),
            owner_boot_id=_boot_id(),
            owner_host_id=_host_id(),
            claimed_at=self._now(),
            released_at=0.0,
            note=takeover_note,
        )
        self._store.save(claimed)
        self._record = claimed
        return claimed

    def release(self) -> None:
        """Graceful hand-back: keep the thread id, drop the ownership claim,
        and release the cross-process reservation.

        The record write is best-effort by design — this runs on ``close()``,
        including during interpreter shutdown, and a store failure here must
        not mask the real exit path. A crash that skips release is covered by
        the liveness check in :meth:`CodexThreadRecord.owner_is_live` (same
        host) and, cross-host, by the deliberate fail-closed refusal in
        :meth:`CodexThreadRecord.owner_is_foreign`.

        The flock is ALWAYS released, even when the record write fails: the OS
        drops it on process exit anyway, so holding it here would only strand
        the session inside a long-lived process.
        """
        try:
            record = self._record
            if record is None or record.owner_pid != os.getpid():
                return
            released = CodexThreadRecord(**{**record.to_dict()})
            released.released_at = self._now()
            try:
                self._store.save(released)
                self._record = released
            except Exception:
                logger.debug(
                    "codex continuity: release of thread %s failed "
                    "(crash-recovery liveness check still covers this)",
                    record.thread_id[:8],
                    exc_info=True,
                )
        finally:
            self._release_reservation()

    def invalidate(self, reason: str) -> None:
        """Drop an unusable record so the next start is a clean fresh thread.

        Only called after an explicit, surfaced refusal — never as a silent
        fallback inside the resume path.
        """
        try:
            self._store.clear()
        except Exception:
            logger.warning(
                "codex continuity: could not clear unusable thread record (%s)",
                reason,
                exc_info=True,
            )
        self._record = None

    @property
    def record(self) -> Optional[CodexThreadRecord]:
        return self._record


def build_session_continuity(
    agent: Any,
    *,
    cwd: str = "",
    permission_profile: str = "",
    model: str = "",
) -> Optional[CodexThreadContinuity]:
    """Build continuity for an AIAgent, or None when it has no durable session.

    A session DB plus a session id is the whole requirement: those two identify
    "the same Hermes conversation" across processes. Ephemeral agents (no DB,
    e.g. one-shot subagents) get None and keep today's fresh-thread behaviour.
    """
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        return None
    try:
        store = SessionModelConfigThreadStore(session_db, str(session_id))
    except ValueError:
        return None
    return CodexThreadContinuity(
        store,
        cwd=cwd,
        permission_profile=permission_profile,
        model=model,
    )
