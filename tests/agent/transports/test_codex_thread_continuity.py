"""Behavioural regression for durable Codex thread continuity.

The bug this locks in: a Hermes session that survives a process restart used to
come back with a *brand new* Codex thread, because
``CodexAppServerSession.ensure_started`` unconditionally called ``thread/start``
on the freshly constructed object. Everything the user said before the restart
was gone from the model's own context even though codex had persisted the
rollout and supports ``thread/resume``.

These tests drive the real ``CodexAppServerSession`` against a fake JSON-RPC
client and the real ``SessionDB`` (temp file, no mocks of the persistence
layer), and assert the *behaviour*:

* same Hermes session, new process  → ``thread/resume`` with the EXACT saved id
* fresh Hermes session              → ``thread/start`` (no cross-talk)
* saved id codex refuses            → explicit error, never a silent new thread
* another live process owns it      → explicit refusal
* store cannot be read              → explicit refusal, never a silent start

Nothing here spawns codex; the live-subprocess proof is a separate script
(``tests/agent/transports/codex_continuity_live_probe.py``).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import pytest

from agent.transports.codex_app_server import CodexAppServerError
from agent.transports.codex_app_server_session import CodexAppServerSession
from agent.transports.codex_thread_continuity import (
    CodexThreadContinuity,
    CodexThreadIdentityMismatch,
    CodexThreadOwnershipError,
    CodexThreadPersistenceError,
    CodexThreadRecord,
    CodexThreadResumeError,
    SessionModelConfigThreadStore,
    THREAD_RECORD_KEY,
    build_session_continuity,
)
from hermes_state import SessionDB


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class FakeClient:
    """Minimal stand-in for CodexAppServerClient.

    ``known_threads`` is the codex-side rollout store: ``thread/resume``
    succeeds only for ids in it, exactly like real codex (verified against
    codex app-server: a fabricated uuid is refused).
    """

    def __init__(self, *, codex_bin: str = "codex", codex_home=None,
                 known_threads: Optional[set] = None,
                 next_thread_id: str = "thread-new-001") -> None:
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.requests: list[tuple[str, dict]] = []
        self.known_threads = set(known_threads or ())
        self.next_thread_id = next_thread_id
        self.closed = False

    def initialize(self, **kwargs):
        return {"userAgent": "fake/0.0.0"}

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = 30.0):
        params = params or {}
        self.requests.append((method, dict(params)))
        if method == "thread/start":
            self.known_threads.add(self.next_thread_id)
            return {"thread": {"id": self.next_thread_id}}
        if method == "thread/resume":
            tid = params.get("threadId")
            if tid not in self.known_threads:
                raise CodexAppServerError(
                    code=-32602,
                    message=f"thread not found: {tid}",
                )
            return {"thread": {"id": tid, "turns": [{"id": "t1"}]}}
        return {}

    def notify(self, *a, **kw):
        pass

    def take_notification(self, timeout: float = 0.0):
        return None

    def take_server_request(self, timeout: float = 0.0):
        return None

    def close(self):
        self.closed = True

    def is_alive(self) -> bool:
        return not self.closed

    def stderr_tail(self, n: int = 20):
        return []

    # convenience
    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]

    def params_for(self, method: str) -> dict:
        for m, p in self.requests:
            if m == method:
                return p
        raise AssertionError(f"{method} was never requested; saw {self.methods()}")


CWD = "/tmp"


def make_session(client: FakeClient, continuity=None, **kwargs):
    return CodexAppServerSession(
        cwd=CWD,
        client_factory=lambda **kw: client,
        continuity=continuity,
        **kwargs,
    )


@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _session_row(db: SessionDB, session_id: str) -> str:
    db.create_session(session_id, source="cli")
    return session_id


def make_continuity(db: SessionDB, session_id: str, *, cwd: str = CWD,
                    permission_profile: str = "workspace-write",
                    model: str = "gpt-5-codex") -> CodexThreadContinuity:
    return CodexThreadContinuity(
        SessionModelConfigThreadStore(db, session_id),
        cwd=cwd,
        permission_profile=permission_profile,
        model=model,
    )


# --------------------------------------------------------------------------
# the core user story
# --------------------------------------------------------------------------

class TestRestartResumesExactThread:
    def test_same_session_new_process_resumes_exact_thread_id(self, db):
        """Hayden resumes the same conversation after a service restart."""
        sid = _session_row(db, "sess-restart")

        # --- process 1: fresh session, fresh thread
        client1 = FakeClient(next_thread_id="thread-REAL-abc123")
        s1 = make_session(client1, continuity=make_continuity(db, sid))
        started = s1.ensure_started()
        assert started == "thread-REAL-abc123"
        assert client1.methods() == ["thread/start"]
        s1.close()  # graceful release

        # --- process 2: same Hermes session id, brand new objects
        client2 = FakeClient(known_threads={"thread-REAL-abc123"},
                             next_thread_id="thread-WRONG-should-not-happen")
        s2 = make_session(client2, continuity=make_continuity(db, sid))
        resumed = s2.ensure_started()

        assert resumed == "thread-REAL-abc123", "must reuse the EXACT saved id"
        assert "thread/start" not in client2.methods(), (
            "restart silently started a new thread — this is the bug"
        )
        assert client2.params_for("thread/resume")["threadId"] == "thread-REAL-abc123"

    def test_record_persists_runtime_identity(self, db):
        sid = _session_row(db, "sess-identity")
        client = FakeClient(next_thread_id="thread-ident")
        s = make_session(
            client,
            continuity=make_continuity(db, sid, model="gpt-5-codex"),
            permission_profile="workspace-write",
        )
        s.ensure_started()

        raw = db.get_session_model_config_value(sid, THREAD_RECORD_KEY)
        record = CodexThreadRecord.from_dict(raw)
        assert record is not None
        assert record.thread_id == "thread-ident"
        assert record.cwd == CWD
        assert record.model == "gpt-5-codex"
        assert record.permission_profile == "workspace-write"
        assert record.owner_pid == os.getpid()

    def test_model_config_lineage_is_preserved(self, db):
        """Continuity must merge into model_config, not clobber it."""
        sid = _session_row(db, "sess-merge")
        db.patch_session_model_config(sid, {"lineage_marker": "keep-me"})
        client = FakeClient(next_thread_id="thread-merge")
        make_session(client, continuity=make_continuity(db, sid)).ensure_started()

        assert db.get_session_model_config_value(sid, "lineage_marker") == "keep-me"
        assert db.get_session_model_config_value(sid, THREAD_RECORD_KEY)


# --------------------------------------------------------------------------
# negative controls
# --------------------------------------------------------------------------

class TestNegativeControls:
    def test_fresh_session_gets_a_fresh_thread(self, db):
        """An independent session cannot recall the other conversation."""
        sid_a = _session_row(db, "sess-a")
        make_session(
            FakeClient(next_thread_id="thread-A"),
            continuity=make_continuity(db, sid_a),
        ).ensure_started()

        sid_b = _session_row(db, "sess-b")
        client_b = FakeClient(known_threads={"thread-A"}, next_thread_id="thread-B")
        tid_b = make_session(
            client_b, continuity=make_continuity(db, sid_b)
        ).ensure_started()

        assert tid_b == "thread-B"
        assert client_b.methods() == ["thread/start"]

    def test_fabricated_thread_id_is_refused_not_restarted(self, db):
        """codex refuses the saved id → explicit error, never a silent start."""
        sid = _session_row(db, "sess-fabricated")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="00000000-dead-beef-0000-000000000000",
                cwd=CWD,
                permission_profile="workspace-write",
                owner_pid=0,
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads=set(), next_thread_id="thread-fallback")
        s = make_session(client, continuity=make_continuity(db, sid))

        with pytest.raises(CodexThreadResumeError):
            s.ensure_started()
        assert "thread/start" not in client.methods()

    def test_live_concurrent_owner_is_refused(self, db):
        sid = _session_row(db, "sess-owner")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-owned",
                cwd=CWD,
                permission_profile="workspace-write",
                owner_pid=os.getppid(),  # a real, live, different process
                owner_boot_id=_current_boot_id(),
                claimed_at=1.0,
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads={"thread-owned"})
        s = make_session(client, continuity=make_continuity(db, sid))

        with pytest.raises(CodexThreadOwnershipError):
            s.ensure_started()
        assert client.requests == []

    def test_dead_owner_is_a_legitimate_takeover(self, db):
        """Crash recovery: the previous owner died without releasing."""
        sid = _session_row(db, "sess-crash")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-crashed",
                cwd=CWD,
                permission_profile="workspace-write",
                owner_pid=_dead_pid(),
                owner_boot_id=_current_boot_id(),
                claimed_at=1.0,
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads={"thread-crashed"})
        s = make_session(client, continuity=make_continuity(db, sid))
        assert s.ensure_started() == "thread-crashed"
        assert client.methods() == ["thread/resume"]

    def test_identity_mismatch_is_refused(self, db):
        sid = _session_row(db, "sess-mismatch")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-elsewhere",
                cwd="/some/other/project",
                permission_profile="workspace-write",
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads={"thread-elsewhere"})
        s = make_session(client, continuity=make_continuity(db, sid))
        with pytest.raises(CodexThreadIdentityMismatch):
            s.ensure_started()
        assert client.requests == []

    def test_unreadable_store_refuses_rather_than_starting(self, db):
        sid = _session_row(db, "sess-broken-store")

        class BrokenStore:
            def load(self):
                raise CodexThreadPersistenceError("db unavailable")

            def save(self, record):
                raise CodexThreadPersistenceError("db unavailable")

            def clear(self):
                raise CodexThreadPersistenceError("db unavailable")

        continuity = CodexThreadContinuity(
            BrokenStore(), cwd=CWD, permission_profile="workspace-write"
        )
        client = FakeClient()
        s = make_session(client, continuity=continuity)
        with pytest.raises(CodexThreadPersistenceError):
            s.ensure_started()
        assert client.requests == []

    def test_unwritable_store_aborts_the_first_turn(self, db, tmp_path):
        """A thread we cannot record is a thread the next restart loses.

        The user must not get a working-looking turn on top of it.
        """
        sid = _session_row(db, "sess-write-fail")

        class WriteOnlyFailsStore(SessionModelConfigThreadStore):
            def save(self, record):
                raise CodexThreadPersistenceError("disk full")

        continuity = CodexThreadContinuity(
            WriteOnlyFailsStore(db, sid),
            cwd=CWD,
            permission_profile="workspace-write",
        )
        client = FakeClient(next_thread_id="thread-unrecordable")
        s = make_session(client, continuity=continuity)

        result = s.run_turn(user_input="hello")

        assert result.error and "continuity refused" in result.error
        assert result.should_retire is True
        assert "turn/start" not in client.methods(), (
            "the user turn must NOT run on an unrecordable thread"
        )

    def test_foreign_host_owner_fails_closed(self, db):
        """A pid from another boot cannot be probed — refuse, don't assume."""
        sid = _session_row(db, "sess-foreign")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-remote",
                cwd=CWD,
                permission_profile="workspace-write",
                owner_pid=999999,
                owner_boot_id="00000000-0000-0000-0000-ffffffffffff",
                claimed_at=1.0,
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads={"thread-remote"})
        s = make_session(client, continuity=make_continuity(db, sid))
        with pytest.raises(CodexThreadOwnershipError) as exc:
            s.ensure_started()
        assert "another host" in str(exc.value)
        assert client.requests == []

    def test_foreign_host_release_is_honoured(self, db):
        """An explicit release from the other host IS a legitimate handover."""
        sid = _session_row(db, "sess-foreign-released")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-handed-over",
                cwd=CWD,
                permission_profile="workspace-write",
                owner_pid=999999,
                owner_boot_id="00000000-0000-0000-0000-ffffffffffff",
                claimed_at=1.0,
                released_at=2.0,
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads={"thread-handed-over"})
        s = make_session(client, continuity=make_continuity(db, sid))
        assert s.ensure_started() == "thread-handed-over"
        assert client.methods() == ["thread/resume"]


# --------------------------------------------------------------------------
# concurrency: an ACTUAL race between two real processes
# --------------------------------------------------------------------------

class TestConcurrentStartRace:
    def test_in_process_second_holder_is_refused(self, db):
        """Two live continuity objects, same session, no record yet.

        Without a reservation both would see "no owner" and both would call
        thread/start. The flock makes the second one lose explicitly.
        """
        sid = _session_row(db, "sess-race-inproc")
        first = make_continuity(db, sid)
        first.load_resumable()  # takes and holds the reservation

        second = make_continuity(db, sid)
        with pytest.raises(CodexThreadOwnershipError):
            second.load_resumable()

        first.release()
        # after release the next process may proceed
        third = make_continuity(db, sid)
        assert third.load_resumable() is None
        third.release()

    def test_two_real_processes_cannot_both_start(self, db):
        """Real subprocess race: N processes, one winner, no double-start.

        This is the control the parent asked for — actual concurrent OS
        processes contending on the same session, not sequential fakes.
        """
        import json
        import subprocess
        import sys

        sid = _session_row(db, "sess-race-subproc")
        repo = _repo_root()
        script = os.path.join(repo, "tests", "agent", "transports",
                              "_continuity_race_child.py")
        procs = [
            subprocess.Popen(
                [sys.executable, script, str(db.db_path), sid, CWD],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=repo, env={**os.environ, "PYTHONPATH": repo},
            )
            for _ in range(6)
        ]
        outcomes = []
        for p in procs:
            out, err = p.communicate(timeout=180)
            line = (out or "").strip().splitlines()
            assert line, f"child produced no output; stderr={err[-1500:]}"
            outcomes.append(json.loads(line[-1]))

        winners = [o for o in outcomes if o["outcome"] == "reserved"]
        losers = [o for o in outcomes if o["outcome"] == "refused"]
        assert len(winners) == 1, (
            f"exactly one process may start the thread; got {outcomes}"
        )
        assert len(losers) == len(procs) - 1, outcomes
        assert all("Ownership" in o["error_type"] for o in losers), outcomes


# --------------------------------------------------------------------------
# ownership lifecycle
# --------------------------------------------------------------------------

class TestOwnershipLifecycle:
    def test_close_releases_ownership(self, db):
        sid = _session_row(db, "sess-release")
        client = FakeClient(next_thread_id="thread-rel")
        s = make_session(client, continuity=make_continuity(db, sid))
        s.ensure_started()
        s.close()

        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value(sid, THREAD_RECORD_KEY)
        )
        assert record is not None and record.is_released()
        assert record.thread_id == "thread-rel", "release must keep the thread id"

    def test_resume_reclaims_ownership_for_this_process(self, db):
        sid = _session_row(db, "sess-reclaim")
        first = make_session(
            FakeClient(next_thread_id="thread-rc"),
            continuity=make_continuity(db, sid),
        )
        first.ensure_started()
        first.close()  # release the reservation, as a real process would
        db.patch_session_model_config(sid, {THREAD_RECORD_KEY: {
            **db.get_session_model_config_value(sid, THREAD_RECORD_KEY),
            "owner_pid": _dead_pid(),
            "released_at": 0.0,
        }})

        client = FakeClient(known_threads={"thread-rc"})
        make_session(client, continuity=make_continuity(db, sid)).ensure_started()

        record = CodexThreadRecord.from_dict(
            db.get_session_model_config_value(sid, THREAD_RECORD_KEY)
        )
        assert record.owner_pid == os.getpid()
        assert not record.is_released()

    def test_no_continuity_keeps_legacy_fresh_thread_behaviour(self):
        """Ephemeral agents (no session DB) are unchanged."""
        client = FakeClient(next_thread_id="thread-ephemeral")
        s = make_session(client, continuity=None)
        assert s.ensure_started() == "thread-ephemeral"
        assert client.methods() == ["thread/start"]

    def test_build_session_continuity_requires_durable_session(self, db):
        class Agent:
            _session_db = None
            session_id = None

        assert build_session_continuity(Agent()) is None

        class DurableAgent:
            _session_db = db
            session_id = "sess-build"

        c = build_session_continuity(DurableAgent(), cwd=CWD)
        assert isinstance(c, CodexThreadContinuity)


# --------------------------------------------------------------------------
# turn-level surfacing (what the service worker actually sees)
# --------------------------------------------------------------------------

class TestRunTurnSurfacesRefusal:
    def test_run_turn_returns_error_and_retires_on_refusal(self, db):
        """A refusal must reach the caller as a TurnResult error, not a
        silently-different conversation."""
        sid = _session_row(db, "sess-turn-refusal")
        db.patch_session_model_config(sid, {
            THREAD_RECORD_KEY: CodexThreadRecord(
                thread_id="thread-gone",
                cwd=CWD,
                permission_profile="workspace-write",
                version=1,
            ).to_dict()
        })
        client = FakeClient(known_threads=set(), next_thread_id="thread-other")
        s = make_session(client, continuity=make_continuity(db, sid))

        result = s.run_turn(user_input="hello")

        assert result.error and "continuity refused" in result.error
        assert result.should_retire is True
        assert "thread/start" not in client.methods()

    def test_run_turn_on_resumed_thread_targets_saved_id(self, db):
        sid = _session_row(db, "sess-turn-resume")
        first = make_session(
            FakeClient(next_thread_id="thread-keep"),
            continuity=make_continuity(db, sid),
        )
        first.ensure_started()
        first.close()

        client = FakeClient(known_threads={"thread-keep"},
                            next_thread_id="thread-nope")
        s = make_session(client, continuity=make_continuity(db, sid))
        s.run_turn(user_input="hello", turn_timeout=0.2)

        assert s.resumed is True
        assert client.params_for("turn/start")["threadId"] == "thread-keep"


# --------------------------------------------------------------------------
# real cross-process persistence control (no mocks of the DB or the process
# boundary: an actual second python process reads the actual sqlite file)
# --------------------------------------------------------------------------

class TestRealSubprocessPersistence:
    def test_second_process_reads_the_same_record(self, db, tmp_path):
        import json
        import subprocess
        import sys

        sid = _session_row(db, "sess-crossproc")
        make_session(
            FakeClient(next_thread_id="thread-crossproc"),
            continuity=make_continuity(db, sid),
        ).ensure_started()

        repo = _repo_root()
        script = (
            "import json,sys;"
            "from pathlib import Path;"
            "sys.path.insert(0, %r);"
            "from hermes_state import SessionDB;"
            "from agent.transports.codex_thread_continuity import "
            "SessionModelConfigThreadStore;"
            "r=SessionModelConfigThreadStore(SessionDB(db_path=Path(%r)), %r).load();"
            "print(json.dumps({'thread_id': r.thread_id, 'pid': r.owner_pid}))"
            % (repo, str(db.db_path), sid)
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120, cwd=repo,
        )
        assert out.returncode == 0, out.stderr[-2000:]
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        assert payload["thread_id"] == "thread-crossproc"
        assert payload["pid"] == os.getpid(), (
            "the other process must see THIS process as the recorded owner"
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _repo_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
    )


def _current_boot_id() -> str:
    from agent.transports.codex_thread_continuity import _boot_id

    return _boot_id()


def _dead_pid() -> int:
    """A pid that is certainly not running (reaped child)."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os._exit(0)
    os.waitpid(pid, 0)
    return pid
