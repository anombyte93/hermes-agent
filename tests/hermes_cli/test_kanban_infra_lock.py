"""State DB contention is infrastructure, not a worker protocol failure (#143)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_state import SessionDB


def test_worker_transcript_lock_retries_are_bounded(tmp_path, monkeypatch):
    """Exercise a real SQLite writer lock, without waiting on wall-clock time."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="worker", source="cli")
    db._conn.execute("PRAGMA busy_timeout=0")
    holder = sqlite3.connect(str(db.db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    clock = [0.0]
    sleeps = []
    begins = []
    db._conn.set_trace_callback(lambda sql: begins.append(sql) if sql == "BEGIN IMMEDIATE" else None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setattr("hermes_state.time.monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("hermes_state.time.sleep", sleep)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.append_messages_batch("worker", [{"role": "user", "content": "work"}])
        # Finalisation/shutdown can flush again after the turn stop. They must
        # not mint another retry budget and stretch a 15s retry into minutes.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.append_messages_batch("worker", [{"role": "user", "content": "work"}])
    finally:
        holder.rollback()
        holder.close()
        db.close()
    assert len(begins) == 3
    assert sleeps == [5.0, 10.0]
    assert clock[0] <= 30.0


def test_nonworker_transcript_lock_keeps_existing_patience(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db._conn.execute("PRAGMA busy_timeout=0")
    holder = sqlite3.connect(str(db.db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    clock = [0.0]
    sleeps = []
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("hermes_state.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("hermes_state.random.uniform", lambda low, high: high)

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("hermes_state.time.sleep", sleep)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.append_messages_batch("chat", [{"role": "user", "content": "work"}])
    finally:
        holder.rollback()
        holder.close()
        db.close()
    assert len(sleeps) > 3
    assert clock[0] == pytest.approx(db._TRANSCRIPT_WRITE_PATIENCE_S)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "_resolve_crash_grace_seconds", lambda: 0)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda: "normal")
    kb.init_db()
    with kb.connect() as conn:
        yield conn, clock, tmp_path


def _exit(conn, task_id, monkeypatch, kind="infra_lock", code=76):
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    kb._set_worker_pid(conn, task_id, 999991)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: (kind, code))
    return kb.detect_crashed_workers(conn), claimed.current_run_id


def test_infra_lock_preserves_existing_failure_budget(board, monkeypatch):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="locked", assignee="worker", max_retries=1)
    conn.execute("UPDATE tasks SET consecutive_failures=1 WHERE id=?", (task_id,))
    conn.commit()
    crashed, _ = _exit(conn, task_id, monkeypatch)
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.consecutive_failures == 1
    assert crashed == []
    run = kb.list_runs(conn, task_id)[0]
    assert run.outcome == "infra_lock"
    assert run.metadata["infra"] == "state_db_locked"
    assert run.metadata["exit_kind"] == "infra_lock"
    assert "verify" in run.error and "kanban_complete" in run.error
    assert not {"gave_up", "protocol_violation", "crashed"} & {
        event.kind for event in kb.list_events(conn, task_id)
    }


@pytest.mark.linux_only
def test_storage_busy_sentinel_is_classified_without_log(monkeypatch):
    monkeypatch.setattr(kb, "_recent_worker_exits", {})
    kb._record_worker_exit(999991, 76 << 8)
    assert kb._classify_worker_exit(999991) == ("infra_lock", 76)


@pytest.mark.parametrize("kind,code", [("clean_exit", 0), ("nonzero_exit", 1), ("unknown", None)])
def test_run_scoped_lock_marker_survives_missing_exit_registry(board, monkeypatch, kind, code):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="marker", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    kb._set_worker_pid(conn, task_id, 999991)
    log = kb.worker_log_path(task_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "⚠️ No reply: the turn was stopped because session storage was busy\n"
        f"kanban_exit: infra_lock run_id={claimed.current_run_id}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: (kind, code))
    assert kb.detect_crashed_workers(conn) == []
    assert kb.list_runs(conn, task_id)[0].outcome == "infra_lock"
    assert kb.get_task(conn, task_id).consecutive_failures == 0


@pytest.mark.parametrize("kind,code", [("clean_exit", 0), ("nonzero_exit", 1), ("signaled", 9), ("unknown", None)])
def test_old_lock_log_cannot_hide_a_new_crash(board, monkeypatch, kind, code):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="old log", assignee="worker", max_retries=1)
    log = kb.worker_log_path(task_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "⚠️ No reply: the turn was stopped because session storage was busy\n"
        "kanban_exit: infra_lock run_id=0\n",
        encoding="utf-8",
    )
    crashed, _ = _exit(conn, task_id, monkeypatch, kind, code)
    assert crashed == [task_id]
    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert task.consecutive_failures == 1
    assert kb.list_runs(conn, task_id)[0].outcome == "crashed"
    assert any(event.kind == "gave_up" for event in kb.list_events(conn, task_id))


def test_infra_lock_is_neutral_to_protocol_violation_streak(board, monkeypatch):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="mixed", assignee="worker")
    for _ in range(2):
        _exit(conn, task_id, monkeypatch, "clean_exit", 0)
    assert kb._protocol_violation_streak(conn, task_id) == 2
    _exit(conn, task_id, monkeypatch)
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb._protocol_violation_streak(conn, task_id) == 2
    _exit(conn, task_id, monkeypatch, "clean_exit", 0)
    assert kb.get_task(conn, task_id).status == "blocked"
    gave_up = [event for event in kb.list_events(conn, task_id) if event.kind == "gave_up"]
    assert gave_up[0].payload["protocol_violations"] == 3


def test_only_lock_exits_respawn_until_separate_hourly_cap(board, monkeypatch, all_assignees_spawnable):
    conn, clock, tmp_path = board
    task_id = kb.create_task(
        conn, title="contended", assignee="worker", max_retries=1,
        workspace_kind="dir", workspace_path=str(tmp_path),
    )
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("infra_lock", 76))
    spawn = lambda *_args, **_kwargs: 999991
    result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    assert [row[0] for row in result.spawned] == [task_id]
    for _ in range(10):
        clock[0] += 31
        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert result.crashed == []
        assert result.infra_lock == [task_id]
        assert result.auto_blocked == []
        assert result.spawned == []  # contention cooldown, not a hot loop
        clock[0] += 31
        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
        assert [row[0] for row in result.spawned] == [task_id]
    clock[0] += 31
    result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    task = kb.get_task(conn, task_id)
    assert task.status == "blocked"
    assert task.block_kind == "transient"
    assert "state.db lock contention" in task.block_reason
    assert "10" in task.block_reason and "hour" in task.block_reason
    assert task.consecutive_failures == 0
    assert result.spawned == []
    assert not any(event.kind == "gave_up" for event in kb.list_events(conn, task_id))
    assert all(run.outcome == "infra_lock" for run in kb.list_runs(conn, task_id))
    kb.recompute_ready(conn)
    assert kb.get_task(conn, task_id).status == "blocked"


def test_hourly_lock_budget_expires_and_is_per_card(board, monkeypatch):
    conn, clock, _ = board
    task_id = kb.create_task(conn, title="window", assignee="worker", max_retries=1)
    for _ in range(10):
        _exit(conn, task_id, monkeypatch)
    clock[0] += 3601
    _exit(conn, task_id, monkeypatch)
    assert kb.get_task(conn, task_id).status == "ready"
    other_id = kb.create_task(conn, title="another card", assignee="worker", max_retries=1)
    _exit(conn, other_id, monkeypatch)
    assert kb.get_task(conn, other_id).status == "ready"


def test_lock_requeues_and_capped_unblock_keep_review_phase(board, monkeypatch):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="review", assignee="reviewer")
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    conn.commit()
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("infra_lock", 76))
    for _ in range(10):
        assert kb.claim_review_task(conn, task_id) is not None
        kb._set_worker_pid(conn, task_id, 999991)
        assert kb.detect_crashed_workers(conn) == []
        assert kb.get_task(conn, task_id).status == "review"
    assert kb.claim_review_task(conn, task_id) is not None
    kb._set_worker_pid(conn, task_id, 999991)
    kb.detect_crashed_workers(conn)
    assert kb.get_task(conn, task_id).status == "blocked"
    assert kb.unblock_task(conn, task_id)
    assert kb.get_task(conn, task_id).status == "review"


def test_lock_marker_uses_explicit_board_not_active_board(board, monkeypatch):
    conn, _, _ = board
    task_id = kb.create_task(conn, title="other board", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    kb._set_worker_pid(conn, task_id, 999991)
    log = kb.worker_log_path(task_id, board="lock-board")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"kanban_exit: infra_lock run_id={claimed.current_run_id}\n", encoding="utf-8")
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("unknown", None))
    assert kb.detect_crashed_workers(conn, board="lock-board") == []
    assert kb.list_runs(conn, task_id)[0].outcome == "infra_lock"


def test_goal_continuation_lock_stop_exits_before_judging_or_blocking(board, monkeypatch):
    import cli
    from hermes_cli import goals

    conn, _, _ = board
    task_id = kb.create_task(conn, title="goal", assignee="worker", goal_mode=True)
    claimed = kb.claim_task(conn, task_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    fake_cli = SimpleNamespace(
        agent=SimpleNamespace(session_id="session", run_conversation=lambda **_kw: {
            "failed": True, "failure_reason": "session_persistence_failed:locked",
            "final_response": "busy",
        }), session_id="session", conversation_history=[],
    )

    def continuation(**kwargs):
        kwargs["run_turn"]("continue")
        kwargs["block_fn"]("must not grade a lock stop")

    monkeypatch.setattr(goals, "run_kanban_goal_loop", continuation)
    with pytest.raises(SystemExit) as exc:
        cli._run_kanban_goal_loop_q(fake_cli, "working")
    assert exc.value.code == 76
    assert kb.get_task(conn, task_id).status == "running"


@pytest.mark.parametrize("worker,cause,expected", [
    (True, "locked", 76), (False, "locked", 1), (True, "disk", 1),
    (True, "corrupt", 1), (True, "compression", 1), (True, "turn_lease", 1),
    (True, "unknown", 1), (True, "rate_limit", 75),
])
def test_quiet_worker_exit_uses_exact_structured_lock_cause(monkeypatch, capsys, worker, cause, expected):
    import cli

    reason = cause if cause == "rate_limit" else f"session_persistence_failed:{cause}"
    agent = SimpleNamespace(
        session_id="session", run_conversation=lambda **_kwargs: {
            "failed": True, "failure_reason": reason,
            "final_response": "storage failure", "messages": [],
        },
    )

    class FakeCLI:
        def __init__(self, **_kwargs):
            self.provider = "test"
            self.model = "test"
            self.session_id = "session"
            self.conversation_history = []
            self._active_agent_route_signature = "same"
            self.agent = agent

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {"signature": "same", "model": None, "runtime": None}

        def _init_agent(self, **_kwargs):
            return True

    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if worker and cause == "locked":
        monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
        monkeypatch.setattr(
            cli, "_run_kanban_goal_loop_q",
            lambda *_args: pytest.fail("a first-turn lock stop must not reach the goal judge"),
        )
    else:
        monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    monkeypatch.setattr(cli, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda _cli: None)
    with pytest.raises(SystemExit) as exc:
        cli.main(query="work", quiet=True, toolsets="terminal")
    assert exc.value.code == expected
    output = capsys.readouterr()
    if worker and cause == "locked":
        assert "kanban_exit: infra_lock run_id=42" in output.err
    else:
        assert "kanban_exit: infra_lock" not in output.err
