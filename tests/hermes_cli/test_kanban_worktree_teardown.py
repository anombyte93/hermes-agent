"""Tests for worktree workspace teardown at task completion/archive.

Covers the ownership gap where kanban ``worktree`` workspaces were never
reaped by anything: ``_cleanup_workspace`` preserved them by design, the CLI
startup pruner explicitly skips ``t_*`` worktrees ("dispatcher-driven
lifecycle"), and ``kanban gc`` only swept scratch. A completed or archived
task's linked worktree is now removed when — and only when — it provably
holds no work: clean working tree and every commit reachable from a
remote-tracking ref. Any doubt preserves the worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_clean_pushed_worktree_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    kbw._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert not wt.exists()
    # auto-generated task branch goes with it
    assert not _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


def test_custom_branch_survives_worktree_removal(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    kbw._cleanup_worktree_workspace("t_dddd4444", str(wt), "feature/custom")
    assert not wt.exists()
    # only auto-generated wt/* branches are deleted
    assert _branch_exists(repo, "feature/custom")


def test_main_checkout_never_removed(repo: Path) -> None:
    kbw._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kbw._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: a tree that becomes dirty after the pre-check is NOT removed.

    Simulates the race by making the pre-check see a clean tree while the
    tree is actually dirty when ``git worktree remove`` runs. Without
    ``--force``, git's own dirty guard re-verifies at removal time and the
    removal fails safe.
    """
    import cli

    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    from hermes_cli import worktree_ops

    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda _p: False)
    kbw._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert not wt.exists()


def test_parent_worktree_deferred_until_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, summary="parent done")
        # child still active -> parent worktree must survive for handoff
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # last child terminal -> deferred parent worktree reaped
    assert not parent_wt.exists()


@pytest.mark.parametrize("shared_worker", [False, True])
@pytest.mark.parametrize("route", ["archive-gc", "child-complete"])
@pytest.mark.parametrize("kind", ["worktree", "scratch"])
def test_external_owner_workspace_survives_every_cleanup_route(kanban_home, repo, route, kind, shared_worker):
    from argparse import Namespace
    from hermes_cli import kanban_db_external as external
    from hermes_cli.kanban_ops import _cmd_gc

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="session owns workspace", execution_kind="external_session")
        if kind == "worktree":
            owned = _make_worktree(repo, tid)
        else:
            owned = kb.workspaces_root() / tid
            owned.mkdir(parents=True)
            (owned / "evidence.txt").write_text("owner's evidence")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind=?,workspace_path=?,branch_name=? WHERE id=?",
                         (kind, str(owned), f"wt/{tid}", tid))
        active = external.external_claim(conn, tid, board="default", owner_host="fixture-host",
            incarnation=dict(session="owner", id="$7", created="1000", pane="%9", pid="424242",
                             backend="claude", conversation="fixture-conversation"),
            worktree=str(owned), reservation_id="cleanup-reservation", reservation_token="a" * 64)
        target = tid
        if shared_worker:
            target = kb.create_task(conn, title="worker sharing the session path", workspace_kind=kind,
                                    workspace_path=str(owned))
        child = kb.create_task(conn, title="worker child", parents=[target], workspace_kind="dir",
                               workspace_path=str(repo))
        external.external_complete(conn, tid, active["run_id"], reservation_token="a" * 64)
        if shared_worker:
            assert kb.complete_task(conn, target)
        assert owned.is_dir()
        if route == "archive-gc":
            assert kb.archive_task(conn, target)
            assert _cmd_gc(Namespace(event_retention_days=30, log_retention_days=30)) == 0
        else:
            assert kb.complete_task(conn, child)
        assert owned.is_dir(), "board removed the external session's workspace"
        assert external.external_status(conn, tid)["binding"]["state"] == "closed"
        if kind == "worktree":
            assert _branch_exists(repo, f"wt/{tid}")
            assert (owned / "README.md").read_text() == "hello\n"
        else:
            assert (owned / "evidence.txt").read_text() == "owner's evidence"


@pytest.mark.parametrize("route", ["archive", "complete", "gc"])
@pytest.mark.parametrize("relation", ["equal", "binding-inside", "workspace-inside", "tilde", "symlink"])
def test_direct_shared_workspace_preserved(kanban_home, repo, monkeypatch, route, relation):
    from argparse import Namespace
    from hermes_cli import kanban_db_external as external
    from hermes_cli.kanban_ops import _cmd_gc

    with kbc.connect_closing() as conn:
        worker, wt = _worktree_task(conn, repo)
        bound = wt
        spelling = str(wt)
        if relation == "binding-inside":
            # An empty directory is a valid cwd and does not dirty a git tree.
            bound = wt / "sub"
            bound.mkdir()
        elif relation == "workspace-inside":
            bound = wt.parent
        elif relation == "tilde":
            spelling = "~/" + str(wt.relative_to(Path.home()))
        elif relation == "symlink":
            alias = Path.home() / "alias"
            alias.symlink_to(wt)
            bound = alias
        tid = kb.create_task(conn, title="owner", execution_kind="external_session")
        active = external.external_claim(conn, tid, board="default", owner_host="fixture-host",
            incarnation=dict(session="owner", id="$7", created="1000", pane="%9", pid="424242",
                             backend="claude", conversation="fixture"),
            worktree=str(bound), reservation_id="shared", reservation_token="a" * 64)
        if route != "archive":
            external.external_complete(conn, tid, active["run_id"], reservation_token="a" * 64)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (spelling, worker))
            if route == "gc":
                conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (worker,))
        if route == "gc":
            assert _cmd_gc(Namespace(event_retention_days=30, log_retention_days=30)) == 0
        else:
            assert getattr(kb, route + "_task")(conn, worker)
        assert wt.is_dir()


@pytest.mark.parametrize("other", ["binding", "corrupt", "legacy"])
def test_other_board_ownership_reads_are_nonmutating(kanban_home, repo, other):
    import sqlite3
    from hermes_cli import kanban_db_external as external

    with kbc.connect_closing() as conn:
        worker, wt = _worktree_task(conn, repo)
        kb.create_board("other")
        path = kb.board_dir("other") / "kanban.db"
        if other == "binding":
            with kbc.connect_closing(board="other") as remote:
                tid = kb.create_task(remote, title="owner", execution_kind="external_session")
                external.external_claim(remote, tid, board="other", owner_host="fixture-host",
                    incarnation=dict(session="owner", id="$7", created="1000", pane="%9", pid="424242",
                                     backend="claude", conversation="fixture"),
                    worktree=str(wt), reservation_id="other", reservation_token="a" * 64)
        elif other == "corrupt":
            path.write_bytes(b"not a database")
        else:
            with sqlite3.connect(path) as legacy:
                legacy.execute("DROP TABLE task_external_bindings")
        # SQLite's read-only WAL reader may initialise locking sidecars, never
        # durable board content. Preserve the DB, metadata and any existing WAL.
        before = {p.name: p.read_bytes() for p in path.parent.iterdir()
                  if p.is_file() and not p.name.endswith(("-shm", "-wal"))}
        wal = Path(str(path) + "-wal")
        wal_before = wal.read_bytes() if wal.exists() else b""
        assert kb.archive_task(conn, worker)
        assert wt.is_dir() == (other != "legacy")
        assert {p.name: p.read_bytes() for p in path.parent.iterdir()
                if p.is_file() and not p.name.endswith(("-shm", "-wal"))} == before
        assert (wal.read_bytes() if wal.exists() else b"") == wal_before


@pytest.mark.parametrize("route", ["archive", "complete", "gc", "parent"])
def test_never_bound_external_workspace_survives_kind_routes(kanban_home, repo, monkeypatch, route):
    from argparse import Namespace
    from unittest.mock import Mock
    from hermes_cli.kanban_ops import _cmd_gc

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="never bound", execution_kind="external_session")
        wt = _make_worktree(repo, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='worktree',workspace_path=? WHERE id=?", (str(wt), tid))
        # Spy at the removal seam: a second ownership layer must not mask kind coverage.
        removal = Mock()
        monkeypatch.setattr(kbw, "_cleanup_worktree_workspace", removal)
        if route == "parent":
            child = kb.create_task(conn, title="child", parents=[tid])
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            kb.recompute_ready(conn)
            assert kb.complete_task(conn, child)
        elif route == "gc":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
            assert _cmd_gc(Namespace(event_retention_days=30, log_retention_days=30)) == 0
        elif route == "complete":
            kbw._cleanup_workspace(conn, tid)
        else:
            assert kb.archive_task(conn, tid)
        assert not removal.called
        assert wt.is_dir()


@pytest.mark.parametrize("route", ["kernel", "tool", "dashboard"])
def test_tilde_creation_routes_preserve_bound_worktree(kanban_home, repo, monkeypatch, route):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    from tools.kanban_tools import _handle_create
    from hermes_cli import kanban_db_external as external

    wt = _make_worktree(repo, "owner")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="owner", execution_kind="external_session")
        external.external_claim(conn, tid, board="default", owner_host="fixture-host",
            incarnation=dict(session="owner", id="$7", created="1000", pane="%9", pid="424242",
                             backend="claude", conversation="fixture"),
            worktree=str(wt), reservation_id="tilde", reservation_token="a" * 64)
        fields = dict(title="sharing worker", assignee="fixture", workspace_kind="worktree",
                      workspace_path="~/" + str(wt.relative_to(Path.home())))
        if route == "kernel":
            worker = kb.create_task(conn, **fields)
        elif route == "tool":
            worker = json.loads(_handle_create(fields))["task_id"]
        else:
            app = FastAPI()
            app.include_router(router)
            response = TestClient(app).post("/tasks", json=fields)
            assert response.status_code == 200, response.text
            worker = response.json()["task"]["id"]
        assert kb.archive_task(conn, worker)
        assert wt.is_dir()


@pytest.mark.parametrize("source", ["current", "archived", "pin"])
def test_active_scratch_owner_survives_archive_from_every_store(kanban_home, tmp_path, monkeypatch, source):
    from hermes_cli import kanban_db_external as external
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *args: None)
    with kbc.connect_closing() as worker_conn:
        worker = kb.create_task(worker_conn, title="managed scratch worker")
        owned = kb.workspaces_root() / worker
        owned.mkdir(parents=True)
        marker = owned / "evidence.txt"
        marker.write_bytes(b"session owned bytes")
        worker_conn.execute("UPDATE tasks SET workspace_kind='scratch',workspace_path=? WHERE id=?", (str(owned), worker))
        if source == "archived":
            kb.create_board("owner-board")
            owner_path = kb.board_dir("owner-board") / "kanban.db"
        elif source == "pin":
            owner_path = tmp_path / "custom-owner.db"
        else:
            owner_path = Path(worker_conn.execute("PRAGMA database_list").fetchone()[2])
        with kbc.connect_closing(owner_path) as owner:
            tid = kb.create_task(owner, title="external", execution_kind="external_session")
            active = external.external_claim(owner, tid, board="owner-board" if source == "archived" else "default",
                owner_host="fixture", incarnation=dict(session="owner", id="$1", created="1", pane="%1", pid="123",
                    backend="claude", conversation="fixture"), worktree=str(owned),
                reservation_id="scratch-owner", reservation_token="a" * 64)
            if source == "archived":
                external.external_complete(owner, tid, active["run_id"], reservation_token="a" * 64)
        if source == "archived":
            assert kb.remove_board("owner-board", archive=True)["action"] == "archived"
        if source == "pin":
            monkeypatch.setenv("HERMES_KANBAN_DB", str(owner_path))
        assert kb.archive_task(worker_conn, worker)
        assert owned.is_dir()
        assert marker.read_bytes() == b"session owned bytes"


def test_custom_connection_worktree_owner_is_retained(kanban_home, repo, tmp_path, monkeypatch):
    from hermes_cli import kanban_db_external as external
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kbw, "_cleanup_worker_tmux", lambda *args: None)
    with kbc.connect_closing(tmp_path / "custom.db") as conn:
        worker, wt = _worktree_task(conn, repo)
        tid = kb.create_task(conn, title="external", execution_kind="external_session")
        external.external_claim(conn, tid, board="default", owner_host="fixture",
            incarnation=dict(session="owner", id="$1", created="1", pane="%1", pid="123", backend="claude", conversation="fixture"),
            worktree=str(wt), reservation_id="custom", reservation_token="a" * 64)
        assert kb.archive_task(conn, worker)
        assert wt.is_dir()
        assert (wt / "README.md").read_text() == "hello\n"


def test_never_claimed_card_protects_shared_path(kanban_home, repo):
    with kbc.connect_closing() as conn:
        worker, wt = _worktree_task(conn, repo)
        kb.create_task(conn, title="reserved workspace", execution_kind="external_session",
                       workspace_kind="dir", workspace_path=str(wt))
        assert kb.archive_task(conn, worker)
        assert wt.is_dir()
