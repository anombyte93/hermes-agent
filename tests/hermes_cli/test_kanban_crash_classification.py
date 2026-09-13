"""Card #2: crash classification reads the real exit + output-limit retry.

Red-first behaviour tests for the dead-worker classifier:

* SIGBUS (signal 7) and friends surface their signal in the run outcome
  (``signaled:7``) and a distinct event kind, not a bare ``crashed``.
* rc=0 without a terminal kanban call stays ``protocol_violation``.
* A worker whose log ends on the output-length-limit banner
  (``Response truncated due to output length limit``) is
  ``output_limit_reached`` — released to ``ready``, no failure-budget
  consumption, a resume note on the card, and a terse-complete prefix on
  the respawn prompt.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb.Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drive_worker_exit(conn, tid, fake_pid, raw_status, board=None):
    """Claim ``tid``, record ``raw_status`` for its dead worker pid, and run
    one reaper pass. Resolves fresh module objects for the exit registry,
    the liveness patch AND the reaper (stale module reloads silently turn a
    clean-exit protocol violation into a plain crash)."""
    import hermes_cli.kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    host_prefix = _kb._claimer_id().split(":", 1)[0]
    claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
    assert claimed is not None, "task was not claimable for the next attempt"
    _kbd._set_worker_pid(conn, tid, fake_pid)
    _kbd._record_worker_exit(fake_pid, raw_status)
    original_alive = _kb._pid_alive
    _kb._pid_alive = lambda p: False
    try:
        return _kbd.detect_crashed_workers(conn)
    finally:
        _kb._pid_alive = original_alive


def _sig_status(sig: int) -> int:
    """Raw wait status for "killed by ``sig``" (POSIX WIFSIGNALED encoding)."""
    return sig


def _exit_status(code: int) -> int:
    """Raw wait status for "exited with ``code``" (W_EXITCODE, POSIX)."""
    return code << 8


def _latest_run(conn, tid):
    return conn.execute(
        "SELECT id, outcome, status, error, metadata FROM task_runs "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()


def _event_kinds(conn, tid):
    rows = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    ).fetchall()
    return [r["kind"] for r in rows]


def _write_worker_log(home, tid, tail, board=None):
    log_dir = kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{tid}.log").write_bytes(tail)


# The Hermes banner emitted at end-of-session by agent/turn_truncation.py, as
# the worker log captures it: padded to the TUI width and CRLF-terminated.
_BANNER = b"  Response truncated due to output length limit" + b" " * 34 + b"\r\n"
_FIRST_BANNER = b"  First response truncated due to output length limit" + b" " * 26 + b"\r\n"


# ---------------------------------------------------------------------------
# 1. Signaled workers keep their signal in the outcome
# ---------------------------------------------------------------------------

def test_signaled_worker_outcome_and_event(kanban_home):
    """SIGBUS (7) is booked as ``signaled:7`` with its own event kind; the
    breaker still counts it (it is a real crash)."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="sigbus", assignee="worker")
        _drive_worker_exit(conn, tid, 22201, _sig_status(7))
        run = _latest_run(conn, tid)
        assert run["outcome"] == "signaled:7"
        assert run["error"] == "pid 22201 killed by signal 7"
        assert "signaled" in _event_kinds(conn, tid)
        assert "crashed" not in _event_kinds(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert (task.consecutive_failures or 0) == 1
    finally:
        conn.close()


def test_signaled_worker_hook_payload(kanban_home):
    """The exit observer still receives the facts, with the precise outcome."""
    captured = {}
    original = kb._fire_kanban_lifecycle_hook

    def spy(hook_name, *args, **kwargs):
        captured[hook_name] = kwargs
        return original(hook_name, *args, **kwargs)

    kb._fire_kanban_lifecycle_hook = spy
    try:
        conn = kbc.connect()
        try:
            tid = kb.create_task(conn, title="sigbus hook", assignee="worker")
            _drive_worker_exit(conn, tid, 22202, _sig_status(7))
        finally:
            conn.close()
    finally:
        kb._fire_kanban_lifecycle_hook = original
    kw = captured["on_kanban_worker_exited"]
    assert kw["exit_kind"] == "signaled"
    assert kw["exit_code"] == 7
    assert kw["outcome"] == "signaled:7"


# ---------------------------------------------------------------------------
# 2. rc=0-no-complete stays a protocol violation
# ---------------------------------------------------------------------------

def test_clean_exit_without_complete_is_protocol_violation(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="rc0", assignee="worker")
        _drive_worker_exit(conn, tid, 22203, _exit_status(0))
        run = _latest_run(conn, tid)
        assert run["outcome"] == "protocol_violation"
        assert "protocol violation" in (run["error"] or "")
        assert "protocol_violation" in _event_kinds(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert (task.consecutive_failures or 0) == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Output-limit deaths are their own outcome
# ---------------------------------------------------------------------------

def test_output_limit_death_released_without_failure(kanban_home):
    """A worker whose log ends on the truncation banner is
    ``output_limit_reached``: back to ``ready``, zero failure consumption,
    distinct event kind, resume-note comment on the card."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="truncated", assignee="worker")
        _write_worker_log(kanban_home, tid, b"...tail...\n" + _BANNER)
        _drive_worker_exit(conn, tid, 22204, _sig_status(9))
        run = _latest_run(conn, tid)
        assert run["outcome"] == "output_limit_reached"
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        assert (task.consecutive_failures or 0) == 0
        kinds = _event_kinds(conn, tid)
        assert "output_limit_reached" in kinds
        assert "crashed" not in kinds
        comments = kb.list_comments(conn, tid)
        assert any("resume note" in c.body.lower() for c in comments), (
            "expected an auto resume-note comment on the card"
        )
    finally:
        conn.close()


def test_output_limit_first_response_banner_matches_too(kanban_home):
    """The first-turn banner variant classifies identically."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="first truncated", assignee="worker")
        _write_worker_log(kanban_home, tid, b"noise\n" + _FIRST_BANNER)
        _drive_worker_exit(conn, tid, 22205, _sig_status(9))
        assert _latest_run(conn, tid)["outcome"] == "output_limit_reached"
    finally:
        conn.close()


def test_output_limit_marker_must_be_in_log_tail_not_history(kanban_home):
    """A truncation banner from a much earlier run (outside the tail window)
    must not reclassify a genuine crash."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="old banner", assignee="worker")
        body = b"Response truncated due to output length limit\n"
        _write_worker_log(kanban_home, tid, body + b"\x00" * 200_000 + b"real tail\n")
        _drive_worker_exit(conn, tid, 22206, _sig_status(9))
        assert _latest_run(conn, tid)["outcome"] == "signaled:9"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. The retry prompt gets a terse-complete resume prefix
# ---------------------------------------------------------------------------

def test_output_limit_retry_prompt_carries_resume_note(kanban_home):
    """The worker argv for a retry after ``output_limit_reached`` is prefixed
    with the terse-complete resume instruction (commit, checks, complete)."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="resume prompt", assignee="worker")
        _write_worker_log(kanban_home, tid, b"..." + _BANNER)
        _drive_worker_exit(conn, tid, 22207, _sig_status(9))
    finally:
        conn.close()

    task = kb.get_task(kbc.connect(), tid)
    assert task is not None
    conn2 = kbc.connect()
    try:
        argv = kbd._worker_argv(task, "codex", None)
    finally:
        conn2.close()
    prompt = argv[argv.index("-q") + 1]
    assert "work kanban task" in prompt
    assert "Response truncated" in prompt
    assert "kanban_complete" in prompt
