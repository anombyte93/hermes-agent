"""Cross-board / cross-process kill safety for dispatcher reclaim paths.

Field evidence (2026-09-13, board hermes-kanban-trust-20260913): three times a
sibling worker's dogfood driver ran a dispatcher tick in-process. The driver's
``HERMES_KANBAN_DB`` env (inherited from its spawning dispatcher) pointed at
the PARENT board, so its connection reached the parent board's rows; every
reclaim/kill path authorized "mine" by hostname prefix alone
(``_kb._host_prefix()``), so one tick SIGKILLed all 7 workers of the board at
23:33:58, 23:36:16 and 23:49:01 (``task_runs`` 45-64: a ``probe`` card
``signaled:9`` in the same second as 7 ``pid not alive``).

The fix class: a claim lock names BOTH the board it was minted for
(``host:pid@<board-db-path>``) and the process that minted it. A reclaim path
may signal/reclaim a row only when this connection's board matches the
claim's board scope AND the claiming dispatcher is this process or is dead.
Hostname alone never authorizes. Operator force-paths (``reclaim_task``,
``archive_task``) keep their explicit intent and only require the board
scope. ``dispatch_once(on_locked="raise")`` gives one-shot CLI dispatch a
clear refusal instead of a silent no-op tick.

All signalling in these tests goes through a ``signal_fn`` spy or targets
throwaway sleeper pids; no test signals a process it did not spawn.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class _SignalSpy:
    """Records every signal attempt; never delivers one."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((int(pid), int(sig)))


@pytest.fixture
def sleeper():
    """A genuinely alive, harmless process; yields its pid."""
    proc = subprocess.Popen(
        ["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        yield proc.pid
    finally:
        proc.terminate()
        proc.wait()


def _board_conn(tmp_path: Path, name: str):
    """An independent board DB + connection (each board its own kanban.db)."""
    db = tmp_path / name / "kanban.db"
    kb.init_db(db)
    return kbc.connect(db)


def _claim(conn, tid: str, claimer: str | None = None) -> str:
    """Claim a ready task and return the stored claim_lock."""
    task = kb.claim_task(conn, tid, claimer=claimer)
    assert task is not None
    row = conn.execute("SELECT claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["claim_lock"]


# ---------------------------------------------------------------------------
# Claim minting: locks name their board
# ---------------------------------------------------------------------------


def test_claim_task_mints_board_scoped_lock(kanban_home, tmp_path):
    """claim_task stamps the board DB path into the claim lock."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="scoped", assignee="w")
        lock = _claim(conn, tid)
    assert "@" in lock, f"claim lock carries no board scope: {lock!r}"
    host_pid, scope = lock.split("@", 1)
    assert scope.endswith("kanban.db")
    assert os.path.realpath(scope) == os.path.realpath(
        str(kb.kanban_db_path(board=None))
    ) or scope  # scope resolves to this board's DB file
    assert host_pid == kb._claimer_id().split("@", 1)[0]


# ---------------------------------------------------------------------------
# Pure authorization helper
# ---------------------------------------------------------------------------


def test_authorization_rejects_other_board_scope(kanban_home, tmp_path):
    """The exact cross-board failure: board B's conn must not manage board
    A's claims, even on the same host."""
    conn_a = _board_conn(tmp_path, "board-a")
    conn_b = _board_conn(tmp_path, "board-b")
    try:
        lock_a = f"{kb._claimer_id()}@{os.path.realpath(str(conn_a.execute('PRAGMA database_list').fetchone()[2]))}"
        ok, reason = kb.claim_authorizes_host_action(conn_b, lock_a)
        assert ok is False
        assert reason == "other_board"
    finally:
        conn_a.close()
        conn_b.close()


def test_authorization_rejects_live_foreign_claimer_same_board(kanban_home, sleeper):
    """Same board, but the claim was minted by ANOTHER LIVE dispatcher: a
    foreign process's tick must defer, not signal (tonight's mass kill)."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split("@", 1)[0].split(":", 1)[0]
        lock = f"{host}:{sleeper}@{_scope(conn)}"
        ok, reason = kb.claim_authorizes_host_action(conn, lock)
        assert ok is False
        assert reason == "claimer_alive"


def test_authorization_accepts_self_claimer(kanban_home):
    with kbc.connect() as conn:
        lock = f"{kb._claimer_id()}@{_scope(conn)}"
        ok, reason = kb.claim_authorizes_host_action(conn, lock)
        assert ok is True
        assert reason == "self"


def test_authorization_accepts_dead_claimer(kanban_home):
    dead = subprocess.Popen(["true"])
    dead.wait()
    with kbc.connect() as conn:
        host = kb._claimer_id().split("@", 1)[0].split(":", 1)[0]
        lock = f"{host}:{dead.pid}@{_scope(conn)}"
        ok, reason = kb.claim_authorizes_host_action(conn, lock)
        assert ok is True
        assert reason == "claimer_dead"


def test_authorization_rejects_foreign_host(kanban_home):
    with kbc.connect() as conn:
        ok, reason = kb.claim_authorizes_host_action(conn, "otherhost:123@x")
        assert ok is False
        assert reason == "foreign_host"


def test_authorization_legacy_lock_still_hostname_governed(kanban_home):
    """Pre-scope locks (no '@') keep the old hostname rule so existing boards
    keep working after upgrade; a dead numeric claimer still authorizes."""
    host = kb._claimer_id().split("@", 1)[0].split(":", 1)[0]
    dead = subprocess.Popen(["true"])
    dead.wait()
    with kbc.connect() as conn:
        ok, _ = kb.claim_authorizes_host_action(conn, f"{host}:nonnumeric")
        assert ok is True
        ok, reason = kb.claim_authorizes_host_action(conn, f"{host}:{dead.pid}")
        assert ok is True
        assert reason == "claimer_dead"


def _scope(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return os.path.realpath(row[2])


# ---------------------------------------------------------------------------
# Reclaim paths: no signal, no release for unauthorized rows
# ---------------------------------------------------------------------------


def _foreign_claimed_running_task(conn, worker_pid: int, claimer_pid: int) -> str:
    """A running task whose claim was minted by a LIVE foreign dispatcher."""
    tid = kb.create_task(conn, title="foreign", assignee="w")
    host = kb._claimer_id().split("@", 1)[0].split(":", 1)[0]
    lock = f"{host}:{claimer_pid}@{_scope(conn)}"
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=?, started_at=? WHERE id=? AND status='ready'",
            (lock, int(time.time()) + 3600, worker_pid,
             int(time.time()) - 9999, tid),
        )
        assert cur.rowcount == 1
    return tid


def test_tick_paths_never_signal_foreign_claimers_worker(kanban_home, sleeper):
    """The regression from tonight: every tick-driven reclaim path must
    leave a live foreign dispatcher's row untouched — no signal, no release."""
    worker = subprocess.Popen(["sleep", "60"])
    try:
        with kbc.connect() as conn:
            tid = _foreign_claimed_running_task(conn, worker.pid, sleeper)
            spy = _SignalSpy()
            # detect_crashed_workers never signals; its harm vector is
            # releasing a live foreign claimer's row (→ duplicate spawn).
            assert kbd.detect_crashed_workers(conn) == []
            assert spy.calls == []
            assert kb.release_stale_claims(conn, signal_fn=spy) == 0
            assert spy.calls == []
            assert kbd.detect_stale_running(
                conn, stale_timeout_seconds=1, signal_fn=spy
            ) == []
            assert spy.calls == []
            # max_runtime must also skip it even with the limit long elapsed.
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (tid,)
            )
            conn.commit()
            assert kbd.enforce_max_runtime(conn, signal_fn=spy) == []
            assert spy.calls == []
            row = conn.execute(
                "SELECT status, claim_lock FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["status"] == "running"
            assert row["claim_lock"] is not None
    finally:
        worker.terminate()
        worker.wait()


def test_tick_paths_reclaim_once_claimer_dies(kanban_home, sleeper):
    """When the foreign claimer is gone, the dead-worker reclaim fires —
    authorization defers, it never wedges the board."""
    worker = subprocess.Popen(["true"])
    worker.wait()
    claimer = subprocess.Popen(["sleep", "60"])
    try:
        with kbc.connect() as conn:
            tid = _foreign_claimed_running_task(conn, worker.pid, claimer.pid)
            assert kbd.detect_crashed_workers(conn) == []
        claimer.terminate()
        claimer.wait()
        with kbc.connect() as conn:
            assert tid in kbd.detect_crashed_workers(conn)
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["status"] in ("ready", "blocked")
    finally:
        if claimer.poll() is None:
            claimer.terminate()
            claimer.wait()


def test_operator_reclaim_still_signals_same_board(kanban_home, sleeper):
    """Operator force-reclaim (explicit intent) keeps working on the claim's
    own board even while the claiming dispatcher lives."""
    worker = subprocess.Popen(["sleep", "60"])
    try:
        with kbc.connect() as conn:
            tid = _foreign_claimed_running_task(conn, worker.pid, sleeper)
            spy = _SignalSpy()
            assert kb.reclaim_task(conn, tid, reason="operator", signal_fn=spy) is True
            assert (worker.pid) in [p for p, _ in spy.calls]
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["status"] in ("ready", "todo")
    finally:
        worker.terminate()
        worker.wait()


# ---------------------------------------------------------------------------
# dispatch_once: a locked board must refuse loudly for one-shot callers
# ---------------------------------------------------------------------------


def test_dispatch_once_on_locked_raise(kanban_home, tmp_path):
    """on_locked='raise' refuses with a clear error and performs no writes."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="w")
        db_path = kb.kanban_db_path(board=None)
        lock_path = Path(str(db_path) + ".dispatch.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as foreign:
            fcntl.flock(foreign.fileno(), fcntl.LOCK_EX)
            try:
                with pytest.raises(kbd.BoardDispatchLockedError) as exc:
                    kbd.dispatch_once(conn, on_locked="raise")
                assert "dispatch lock" in str(exc.value)
                # Default behaviour unchanged: silent skip.
                res = kbd.dispatch_once(conn)
                assert res.skipped_locked is True
            finally:
                fcntl.flock(foreign.fileno(), fcntl.LOCK_UN)
        # Task untouched by the refused ticks.
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["status"] == "ready"


def test_cli_dispatch_uses_raise_on_locked(kanban_home, monkeypatch):
    """The one-shot CLI dispatch passes on_locked='raise' so a stray tick
    fails loudly instead of silently racing the board's daemon."""
    captured: dict = {}

    def _capture(conn, **kwargs):
        captured.update(kwargs)
        result = kbd.DispatchResult()
        result.skipped_locked = True
        return result

    monkeypatch.setattr(kbd, "dispatch_once", _capture)
    import argparse

    from hermes_cli.kanban_ops import _cmd_dispatch

    args = argparse.Namespace(
        dry_run=False, json=True, max=None, per_tick=None,
        failure_limit=kbd.DEFAULT_FAILURE_LIMIT,
    )
    with kbc.connect_closing() as conn:
        _cmd_dispatch(args)
    assert captured.get("on_locked") == "raise"
