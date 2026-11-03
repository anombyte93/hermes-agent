"""Provider quota walls survive CLI rendering and wait through real dispatch ticks."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

FIXTURE = Path(__file__).parents[1] / "fixtures/kanban/kimi_5h_usage_limit_worker.log"
NOW = int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setattr(kbd.time, "time", lambda: NOW)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda: "normal")
    kb.init_db()
    with kbc.connect() as conn:
        yield conn


def _task(conn):
    return kb.create_task(conn, title="quota worker", assignee="test-worker",
                          model_override="k3.1-pro", provider_override="kimi-coding")


def _run(conn, tid, text=None, code=75):
    claimed = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert claimed is not None
    with kbd._open_worker_log(claimed, None) as log:
        if text is not None:
            log.write(text.encode())
    pid = 98001
    kbd._set_worker_pid(conn, tid, pid)
    kbd._record_worker_exit(pid, code << 8)
    kbd.detect_crashed_workers(conn)
    return conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()


@pytest.mark.linux_only
@pytest.mark.parametrize("code", [0, 17])
def test_fixture_printing_worker_requeues_without_failure(board, code, capsys):
    tid = _task(board)
    claimed = kb.claim_task(board, tid, claimer=kb._claimer_id())
    assert claimed is not None
    with kbd._open_worker_log(claimed, None) as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes()); sys.exit(int(sys.argv[2]))",
             str(FIXTURE), str(code)], stdout=log, stderr=subprocess.STDOUT,
        )
        proc.wait(timeout=10)
    assert proc.returncode == code
    kbd._set_worker_pid(board, tid, proc.pid)
    kbd._record_worker_exit(proc.pid, code << 8)
    assert kbd.detect_crashed_workers(board) == []
    task = kb.get_task(board, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    run = kb.list_runs(board, tid)[-1]
    assert run.outcome == "rate_limited"
    assert run.metadata is not None
    until = NOW + 5 * 3600
    assert run.metadata["rate_limit_until"] == until
    iso = datetime.fromtimestamp(until, timezone.utc).isoformat().replace("+00:00", "Z")
    expected = f"provider usage limit (HTTP 429) on kimi-coding/k3.1-pro; next try after {iso}"
    assert task.last_failure_error == expected
    events = board.execute("SELECT kind,payload FROM task_events WHERE task_id=?", (tid,)).fetchall()
    assert not {"crashed", "protocol_violation"} & {r["kind"] for r in events}
    payload = json.loads(next(r["payload"] for r in events if r["kind"] == "rate_limited"))
    assert (payload["provider"], payload["model"], payload["until"]) == ("kimi-coding", "k3.1-pro", until)
    from hermes_cli.kanban import _cmd_show
    assert _cmd_show(argparse.Namespace(task_id=tid, json=False)) == 0
    assert expected in capsys.readouterr().out


def test_five_dispatches_wait_then_one_spawns(board, monkeypatch):
    tid = _task(board)
    run = _run(board, tid, FIXTURE.read_text(), code=0)
    until = json.loads(run["metadata"])["rate_limit_until"]
    spawned = []
    def spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 98002
    for offset in [1, 60, 300, 900, 17999]:
        monkeypatch.setattr(kbd.time, "time", lambda: NOW + offset)
        result = kbd.dispatch_once(board, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
        assert result.spawned == []
        assert (tid, "rate_limit_cooldown") in result.respawn_guarded
    assert spawned == []
    monkeypatch.setattr(kbd.time, "time", lambda: until + 1)
    result = kbd.dispatch_once(board, spawn_fn=spawn, max_spawn=1, reconcile_orphans=False)
    assert [entry[0] for entry in result.spawned] == [tid]
    assert spawned == [tid]


@pytest.mark.parametrize("text,delay", [
    ("5-hour usage limit; resets in 20 minutes", 1200),
    ("5-hour usage limit; try again in 1h 30m", 5400),
    ("5-hour usage limit; retry-after: 45", 45),
    ("weekly usage limit", 86400),
    ("usage limit; resets at 2026-11-03T02:00:00Z", 7200),
    ("usage limit; retry-after: Tue, 03 Nov 2026 02:00:00 GMT", 7200),
    ("weekly usage limit; resets in 0s", 0),
    ("weekly usage limit; quotaResetDelay: 1.5s", 2),
    ("5-hour usage limit; resets at 2026-11-03T10:00:00+08:00", 7200),
    ("usage limit; resets in 9999999999999999999999999999999 hours", 300),
    ("5-hour usage limit; resets in 1 hour and 30 minutes", 5400),
])
def test_reset_and_window_deadlines(board, text, delay):
    tid = _task(board)
    run = _run(board, tid, "HTTP 429: " + text)
    assert json.loads(run["metadata"])["rate_limit_until"] == NOW + delay


def test_five_hour_streak_does_not_slide(board, monkeypatch):
    tid = _task(board)
    first = _run(board, tid, "HTTP 429: 5-hour usage limit")
    for offset in (600, 17999):
        monkeypatch.setattr(kbd.time, "time", lambda: NOW + offset)
        second = _run(board, tid, "HTTP 429: 5-hour usage limit")
        assert json.loads(second["metadata"])["rate_limit_until"] == NOW + 18000
    assert json.loads(first["metadata"])["rate_limit_until"] == NOW + 18000
    monkeypatch.setattr(kbd.time, "time", lambda: NOW + 18001)
    probe = _run(board, tid, "HTTP 429: 5-hour usage limit")
    assert json.loads(probe["metadata"])["rate_limit_until"] == NOW + 18001 + 3600


def test_backoff_ladder_and_streak_reset(board, monkeypatch):
    tid = _task(board)
    now = NOW
    for delay in [300, 900, 3600, 3600]:
        monkeypatch.setattr(kbd.time, "time", lambda: now)
        run = _run(board, tid)
        assert json.loads(run["metadata"])["rate_limit_until"] == now + delay
        now += delay + 1
    _run(board, tid, "real failure", code=2)
    run = _run(board, tid)
    assert json.loads(run["metadata"])["rate_limit_until"] == now + 300


@pytest.mark.parametrize("text", [
    "HTTP 429: usage limit", "HTTP 429: rate limit", "HTTP 429: quota",
    "HTTP 429: rate_limit_error", "usage limit (HTTP 429)", "rate_limit_error: HTTP 429",
])
def test_provider_markers_are_order_independent(board, text):
    run = _run(board, _task(board), text, code=2)
    assert run["outcome"] == "rate_limited"
    assert json.loads(run["metadata"])["rate_limit_until"] == NOW + 300


@pytest.mark.parametrize("text", [
    "HTTP 429: usage limit\n" + "x" * 200_000 + "real crash",
    "HTTP 429: unrelated bad request", "usage limit without HTTP status",
])
def test_unrelated_or_old_log_does_not_reclassify(board, text):
    run = _run(board, _task(board), text, code=2)
    assert run["outcome"] == "crashed"


def test_previous_short_run_cannot_reclassify_current_crash(board):
    tid = _task(board)
    _run(board, tid, FIXTURE.read_text(), code=0)
    run = _run(board, tid, "a genuinely different crash", code=2)
    assert run["outcome"] == "crashed"


def test_missing_and_unreadable_log_cannot_reclassify(board, monkeypatch):
    for unreadable in [False, True]:
        tid = _task(board)
        claimed = kb.claim_task(board, tid)
        if unreadable:
            path = kb.worker_logs_dir() / f"{tid}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.mkdir()  # opening a directory as a file fails even for root
        kbd._set_worker_pid(board, tid, 98003)
        kbd._record_worker_exit(98003, 2 << 8)
        kbd.detect_crashed_workers(board)
        assert kb.list_runs(board, tid)[0].outcome == "crashed"


def test_zero_cooldown_dispatches_next_tick(board, monkeypatch):
    tid = _task(board)
    _run(board, tid, FIXTURE.read_text())
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
    result = kbd.dispatch_once(board, spawn_fn=lambda task, workspace: 98004,
                               max_spawn=1, reconcile_orphans=False)
    assert [r[0] for r in result.spawned] == [tid]


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("status,expected", [(429, 75), (402, 75), (500, 1)])
def test_single_query_exits_with_classified_failure(monkeypatch, quiet, status, expected):
    import cli
    from agent.error_classifier import classify_api_error
    from agent.turn_recovery import max_retries_exhausted_result
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-card")
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    error = RuntimeError("HTTP 429: 5-hour usage limit; current window ends later" if status == 429 else "provider error")
    error.status_code = status
    agent = Mock(model="k3.1-pro", provider="kimi-coding", session_id="test-session", log_prefix="test")
    agent._summarize_api_error.return_value = str(error)
    classified = classify_api_error(error, provider=agent.provider, model=agent.model)
    result = max_retries_exhausted_result(
        agent, error, classified, max_retries=3, is_rate_limited=status == 429,
        error_msg=str(error), api_kwargs=None, api_messages=[], messages=[],
        conversation_history=[], api_call_count=3, approx_tokens=0, provider=agent.provider,
        base_url="https://example.invalid", model=agent.model,
    )
    assert result["failed"] is True
    agent.run_conversation.return_value = result
    subject = Mock(agent=agent, session_id="test-session", conversation_history=[], _no_fallback=False)
    subject.chat.side_effect = lambda *a, **kw: setattr(subject, "_last_turn_result", result)
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a: False)
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda images: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda subject: None)
    with pytest.raises(SystemExit) as exc:
        if quiet:
            cli._run_quiet_single_query(subject, "test query")
        else:
            cli._run_single_query_mode(subject, "test query", None, False, True)
    assert exc.value.code == expected


@pytest.mark.parametrize("quiet", [False, True])
def test_no_fallback_still_blocks_own_card(board, monkeypatch, quiet):
    import cli
    tid = kb.create_task(board, title="pinned route", assignee="test-worker", no_fallback=True)
    claimed = kb.claim_task(board, tid)
    assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    result = {"failed": True, "failure_reason": "rate_limit", "error": "HTTP 429: weekly usage limit"}
    subject = Mock(_no_fallback=True, session_id="test-session", conversation_history=[])
    subject.agent = Mock(model="k3.1-pro", provider="kimi-coding", session_id="test-session")
    subject.agent.run_conversation.return_value = result
    subject.chat.side_effect = lambda *a, **kw: setattr(subject, "_last_turn_result", result)
    monkeypatch.setattr(cli, "_should_seed_interactive", lambda *a: False)
    monkeypatch.setattr(cli, "_collect_kanban_task_images", lambda images: [])
    monkeypatch.setattr(cli, "_finalize_single_query", lambda subject: None)
    with pytest.raises(SystemExit) as exc:
        if quiet:
            cli._run_quiet_single_query(subject, "test")
        else:
            cli._run_single_query_mode(subject, "test", None, False, True)
    assert exc.value.code == 1
    task = kb.get_task(board, tid)
    assert task is not None and task.status == "blocked"
    assert "fallback disabled (--no-fallback)" in task.block_reason
    assert "weekly usage limit" in task.block_reason


@pytest.mark.parametrize("unknown_exit", [False, True])
def test_legacy_unmarked_log_backstop(board, unknown_exit):
    tid = _task(board)
    kb.claim_task(board, tid)
    path = kb.worker_logs_dir() / f"{tid}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FIXTURE.read_bytes())
    kbd._set_worker_pid(board, tid, 98005)
    if not unknown_exit:
        kbd._record_worker_exit(98005, 0)
    assert kbd.detect_crashed_workers(board) == []
    assert kb.list_runs(board, tid)[-1].outcome == "rate_limited"


def test_exit_codes_without_a_worker_and_after_success(monkeypatch):
    from hermes_cli.cli_single_query import single_query_exit_code
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert single_query_exit_code({"failed": True, "failure_reason": "rate_limit"}) == 1
    monkeypatch.setenv("HERMES_KANBAN_TASK", "test-card")
    assert single_query_exit_code({"failed": False, "final_response": "HTTP 429: usage limit in a document"}) == 0
