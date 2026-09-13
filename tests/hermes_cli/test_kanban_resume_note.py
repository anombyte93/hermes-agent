"""Automatic dispatcher resume notes (card #4, hermes-kanban-trust wave).

On every non-completed run the dispatcher must write a self-contained
"resume note" comment (author ``dispatcher``) so the retry worker continues
from the previous attempt's real state — commits since base, dirty files,
last worker-log tool lines, run outcome — instead of restarting blind.

These tests exercise the pure note builder against real temp git repos and
the dispatcher insertion paths against a real board DB. No source-reading,
no snapshots.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _seed_repo(repo: Path) -> str:
    """Init a git repo: one commit on ``main`` (returns its sha = base), then
    a ``work`` branch checked out for the card's commits. The branch matters:
    a card commits on a side branch, so ``merge-base HEAD main`` (the base the
    note derives when no explicit base is recorded) lands below the card's
    commits instead of on HEAD itself."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "kanban@example.com")
    _git(repo, "config", "user.name", "Kanban Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "work")
    return base


# ---------------------------------------------------------------------------
# build_resume_note — pure helper
# ---------------------------------------------------------------------------


def test_resume_note_lists_new_commits_dirty_files_and_outcome(tmp_path):
    base = _seed_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    (repo / "A.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "card work: create A")
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    note = kb.build_resume_note(
        str(repo), base_sha=base,
        run={"outcome": "crashed", "error": "pid 1 exited with code 1"},
    )

    assert "card work: create A" in note          # commit subject present
    assert base not in note                        # base commit itself excluded
    assert "?? dirty.txt" in note                  # untracked file listed
    assert "crashed" in note
    assert "pid 1 exited with code 1" in note


def test_resume_note_caps_commits_and_status_lines(tmp_path):
    base = _seed_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    for i in range(20):
        (repo / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"commit {i:02d}")
    for j in range(30):
        (repo / f"d{j}.txt").write_text("d\n", encoding="utf-8")

    note = kb.build_resume_note(str(repo), base_sha=base, run={"outcome": "timed_out"})

    subject_lines = [l for l in note.splitlines() if "commit " in l]
    assert len(subject_lines) <= 15                # newest 15 commits only
    assert "commit 19" in note and "commit 05" in note
    assert "commit 04" not in note                 # older than the 15-line cap
    status_lines = [l for l in note.splitlines() if "d" in l and ".txt" in l]
    assert len(status_lines) <= 20                 # newest 20 dirty entries


def test_resume_note_derives_base_from_merge_base_when_absent(tmp_path):
    base = _seed_repo(tmp_path / "repo")
    repo = tmp_path / "repo"
    _git(repo, "checkout", "-b", "feature")
    (repo / "B.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature work")

    note = kb.build_resume_note(str(repo), base_sha=None, run={"outcome": "crashed"})

    assert "feature work" in note                  # merge-base vs main derived


def test_resume_note_non_git_workspace_says_so(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("x\n", encoding="utf-8")

    note = kb.build_resume_note(str(plain), base_sha=None, run={"outcome": "stale"})

    assert "not a git repository" in note
    assert "stale" in note                         # outcome still reported


def test_resume_note_includes_last_worker_log_lines(kanban_home, tmp_path):
    base = _seed_repo(tmp_path / "repo")
    log_dir = kb.worker_logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "t_resume1.log").write_text(
        "\x1b[2m┊ ⚡ kanban_sh   0.0s\x1b[0m\nsome reasoning\n\x1b[2m┊ ⚡ terminal    1.2s\x1b[0m\n",
        encoding="utf-8",
    )

    note = kb.build_resume_note(
        str(tmp_path / "repo"), base_sha=base, run={"outcome": "crashed"},
        log_path=str(log_dir / "t_resume1.log"),
    )

    assert "kanban_sh" in note and "terminal" in note  # tool lines surfaced
    assert "some reasoning" not in note                # non-tool lines dropped


# ---------------------------------------------------------------------------
# Dispatcher insertion — idempotent comment per run
# ---------------------------------------------------------------------------


def _claim_with_dead_worker(conn, tid: str, pid: int) -> None:
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w")
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()


def test_crash_reclaim_writes_dispatcher_resume_note(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    repo = kanban_home.parent / "repo"
    base = _seed_repo(repo)
    (repo / "A.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "partial work before crash")

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="crash note", assignee="a")
        kbw.set_workspace_path(conn, tid, str(repo))
        _claim_with_dead_worker(conn, tid, 70001)
        kbd.detect_crashed_workers(conn)

        notes = [c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"]
        assert len(notes) == 1
        assert "partial work before crash" in notes[0].body
        assert base not in notes[0].body

        # Idempotent per run: a re-sweep with no new run adds nothing.
        kbd.detect_crashed_workers(conn)
        assert len([c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"]) == 1


def test_rate_limited_reclaim_also_writes_resume_note(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    repo = kanban_home.parent / "repo"
    _seed_repo(repo)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl note", assignee="a")
        kbw.set_workspace_path(conn, tid, str(repo))
        _claim_with_dead_worker(conn, tid, 70002)
        kbd._record_worker_exit(70002, 75 << 8)  # KANBAN_RATE_LIMIT_EXIT_CODE
        kbd.detect_crashed_workers(conn)

        notes = [c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"]
        assert len(notes) == 1
        assert "rate_limited" in notes[0].body


def test_timeout_reclaim_writes_dispatcher_resume_note(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    repo = kanban_home.parent / "repo"
    _seed_repo(repo)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="timeout note", assignee="a", max_runtime_seconds=1)
        kbw.set_workspace_path(conn, tid, str(repo))
        _claim_with_dead_worker(conn, tid, 70003)
        # Runtime is per attempt: the active task_runs row's started_at is
        # what enforce_max_runtime measures (tasks.started_at records the
        # FIRST start), so age the run, not the task.
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (int(time.time()) - 60, tid),
        )
        conn.commit()
        # signal_fn must be faked: the real path SIGTERMs/SIGKILLs host pids —
        # never risk signal delivery from a test (2026-09-13 board incident).
        kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        notes = [c for c in kb.list_comments(conn, tid) if c.author == "dispatcher"]
        assert len(notes) == 1
        assert "timed_out" in notes[0].body


def test_rate_limited_reclaim_does_not_count_failure(kanban_home, monkeypatch):
    """Regression guard: adding the resume note must not turn a quota-wall
    requeue into a counted failure (the breaker must stay untripped)."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    repo = kanban_home.parent / "repo"
    _seed_repo(repo)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl no count", assignee="a")
        kbw.set_workspace_path(conn, tid, str(repo))
        _claim_with_dead_worker(conn, tid, 70004)
        kbd._record_worker_exit(70004, 75 << 8)
        kbd.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert task.consecutive_failures == 0


# ---------------------------------------------------------------------------
# Worker context — newest dispatcher note hoisted verbatim
# ---------------------------------------------------------------------------


def test_worker_context_hoists_newest_dispatcher_note(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ctx note", assignee="a")
        kb.add_comment(conn, tid, "dispatcher", "note one from run 1")
        kb.add_comment(conn, tid, "dispatcher", "note two from run 2")
        # A closed prior run: the hoisted note must sit above the attempt
        # history it belongs to, and ``_ctx_prior_attempts`` renders nothing
        # without at least one ended run.
        kb.claim_task(conn, tid, claimer="h:w")
        kb._end_run(conn, tid, outcome="crashed", error="pid 70005 not alive")

        ctx = kb.build_worker_context(conn, tid)

        resume_idx = ctx.find("Resume note from the previous run")
        assert resume_idx != -1, "worker context must carry a resume-note section"
        assert "note two from run 2" in ctx          # newest wins
        prior_idx = ctx.find("## Prior attempts on this task")
        assert prior_idx != -1
        assert resume_idx < prior_idx                # near the top, before history
        # Verbatim, not rewrapped: the exact body text appears intact.
        assert "note one from run 1" not in ctx or "note two" in ctx


def test_worker_context_without_notes_unchanged(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ctx clean", assignee="a")
        kb.add_comment(conn, tid, "worker", "ordinary worker comment")

        ctx = kb.build_worker_context(conn, tid)

        assert "Resume note from the previous run" not in ctx
        assert "ordinary worker comment" in ctx
