"""Tests: run-end evidence harvest wired into ``_end_run`` (trust wave #9).

Lane A of the 2026-09-13 Align wave died on output length with six commits on
disk and zero metadata; the board's record of what a run produced existed only
when the worker managed to self-report. These tests prove the dispatcher now
DERIVES that record at every run end (completed, crashed, timed out) and
records agreement against whatever the worker did claim.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _git(path: Path, *args: str):
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    )


def _git_out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def git_workspace(conn, tmp_path):
    """A task whose workspace is a git repo with one base commit."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@example.com")
    _git(ws, "config", "user.name", "T")
    (ws / "base.txt").write_text("base\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "base")
    tid = kb.create_task(conn, title="harvest me", assignee="w", workspace_kind="dir",
                         workspace_path=str(ws))
    return tid, ws


def _run_metadata(conn, run_id: int) -> dict:
    row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    return json.loads(row["metadata"]) if row and row["metadata"] else {}


def _claim_with_stamp(conn, tid, claimer, ws):
    """Claim like the dispatcher does, stamping the evidence boundary."""
    kb.claim_task(conn, tid, claimer=claimer)
    from hermes_cli import kanban_db_dispatch as kbd

    kbd._stamp_attempt_base_head(conn, tid)
    _ = ws  # workspace used implicitly via the task row


def test_completed_run_records_harvest_and_match(conn, git_workspace):
    """Worker completes with commits + a self-report that agrees: the run row
    carries the derived harvest and ``agreement: match`` per field."""
    tid, ws = git_workspace
    _claim_with_stamp(conn, tid, "h:A", ws)
    (ws / "work.txt").write_text("done\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat: the work")
    head = _git_out(ws, "rev-parse", "HEAD")

    ok = kb.complete_task(
        conn, tid, summary="done", metadata={"head_sha": head, "tests_run": {"unit": 4}},
    )
    assert ok

    run = kb.get_run(conn, kb.list_runs(conn, tid)[0].id) if kb.list_runs(conn, tid) else None
    # The completed run is the one with outcome=completed.
    runs = [r for r in kb.list_runs(conn, tid) if r.outcome == "completed"]
    assert runs, "no completed run recorded"
    meta = _run_metadata(conn, runs[-1].id)

    harvest = meta["harvest"]
    assert harvest["head_sha"] == head
    assert [c["subject"] for c in harvest["commits"]] == ["feat: the work"]
    assert harvest["diffstat"]["files"] >= 1
    assert harvest["branch"]
    assert meta["agreement"]["head_sha"] == "match"


def test_crashed_run_harvests_even_when_worker_silent(conn, git_workspace):
    """The Lane A shape: worker dies having committed but reported nothing.
    A crash close still records the derived facts (worker_silent agreement)."""
    tid, ws = git_workspace
    _claim_with_stamp(conn, tid, "h:A", ws)
    (ws / "orphan.txt").write_text("never reported\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat: unreported work")

    # Simulate the dispatcher's crash sweep closing the run.
    with kb.write_txn(conn):
        run_id = kb._end_run(conn, tid, outcome="crashed", status="crashed",
                             error="pid not alive")
    assert run_id is not None
    meta = _run_metadata(conn, run_id)

    harvest = meta["harvest"]
    assert [c["subject"] for c in harvest["commits"]] == ["feat: unreported work"]
    assert meta["agreement"] == {"head_sha": "worker_silent", "tests_run": "worker_silent"}


def test_worker_log_feeds_test_counts(conn, git_workspace, kanban_home):
    """The per-task worker log is parsed for runner summaries at run end."""
    tid, ws = git_workspace
    log_dir = kb.worker_logs_dir(board="default")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{tid}.log").write_text(
        "- Unit: 483 passed across 53 files\n- Isolated Chromium e2e: 320/320 passed\n"
    )
    _claim_with_stamp(conn, tid, "h:A", ws)
    with kb.write_txn(conn):
        run_id = kb._end_run(conn, tid, outcome="completed", status="done")
    meta = _run_metadata(conn, run_id)
    assert meta["harvest"]["test_counts"] == {"unit": 483, "e2e": 320}


def test_retry_attempt_does_not_recredit_prior_commits(conn, git_workspace):
    """Attempt 2's commit list starts from attempt 1's recorded head."""
    tid, ws = git_workspace
    # Attempt 1: one commit, crashed.
    _claim_with_stamp(conn, tid, "h:A", ws)
    (ws / "a1.txt").write_text("x\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat: attempt one")
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="crashed", status="crashed")
    # Task back to ready for attempt 2 (crash path normally resets this).
    conn.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,),
    )
    conn.commit()

    # Attempt 2: one more commit, completed.
    _claim_with_stamp(conn, tid, "h:B", ws)
    (ws / "a2.txt").write_text("y\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat: attempt two")
    kb.complete_task(conn, tid, summary="second try")

    runs = [r for r in kb.list_runs(conn, tid) if r.outcome == "completed"]
    meta = _run_metadata(conn, runs[-1].id)
    assert [c["subject"] for c in meta["harvest"]["commits"]] == ["feat: attempt two"]


def test_mismatch_surfaces_as_comment(conn, git_workspace):
    """A worker claiming a head it does not stand on gets a dispatcher
    comment naming the mismatch."""
    tid, ws = git_workspace
    _claim_with_stamp(conn, tid, "h:A", ws)
    (ws / "w.txt").write_text("z\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "feat: real work")

    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="completed", status="done",
                    metadata={"head_sha": "0" * 40})
    comments = kb.list_comments(conn, tid)
    dispatcher_comments = [c for c in comments if c.author == "dispatcher"]
    assert dispatcher_comments, "no mismatch comment surfaced"
    assert "mismatch: head_sha" in dispatcher_comments[-1].body


def test_harvest_failure_never_breaks_run_close(conn, git_workspace, monkeypatch):
    """A harvest explosion is swallowed: the run still closes cleanly."""
    from hermes_cli import kanban_harvest_run

    def _boom(*a, **k):
        raise RuntimeError("harvest exploded")

    monkeypatch.setattr(kanban_harvest_run, "_merge_harvest_into_run_metadata", _boom)
    tid, ws = git_workspace
    kb.claim_task(conn, tid, claimer="h:A")
    with kb.write_txn(conn):
        run_id = kb._end_run(conn, tid, outcome="completed", status="done")
    assert run_id is not None
    row = conn.execute(
        "SELECT status, outcome FROM task_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "done" and row["outcome"] == "completed"


def test_non_git_workspace_records_partial_harvest(conn, tmp_path):
    """A dir workspace with no git still gets a (partial) harvest record, and
    the close never raises."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "f.txt").write_text("x")
    tid = kb.create_task(conn, title="no git", assignee="w", workspace_kind="dir",
                         workspace_path=str(plain))
    kb.claim_task(conn, tid, claimer="h:A")
    with kb.write_txn(conn):
        run_id = kb._end_run(conn, tid, outcome="completed", status="done")
    meta = _run_metadata(conn, run_id)
    assert meta["harvest"]["git"] == "absent"
    assert meta["harvest"]["head_sha"] is None
