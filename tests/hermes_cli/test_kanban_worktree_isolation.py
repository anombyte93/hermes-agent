"""Per-task worktree isolation for decompose siblings.

Decompose children used to inherit the root's literal ``workspace_path``,
so every sibling of a worktree-kind root pointed at the SAME checkout —
and ``_resolve_worktree_workspace``'s existing-checkout shortcut reused it
on whatever branch was there, letting sibling workers run concurrently in
one directory on one branch (cross-task provenance corruption, no lock).

Two-part fix under test:
- ``decompose_triage_task`` leaves worktree children's ``workspace_path``
  unset so each child materializes its own ``<repo>/.worktrees/<child-id>``.
- ``_resolve_worktree_workspace`` falls back to a fresh per-task worktree
  when the requested path is occupied by another task's branch (heals
  pre-existing rows that still carry a shared path).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_worktree(repo: Path, target: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", str(target), "-b", branch, "HEAD")
    return target


def _current_branch(worktree: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        check=True, capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def _checkout_snapshot(worktree: Path) -> tuple[str, bytes, dict[str, bytes]]:
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        check=True, capture_output=True,
    ).stdout
    files = {
        str(path.relative_to(worktree)): path.read_bytes()
        for path in sorted(worktree.rglob("*"))
        if path.is_file()
    }
    return _current_branch(worktree), status, files


def test_decompose_worktree_children_get_own_workspace(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="build the feature", triage=True)
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', "
            "workspace_path='/repo/.worktrees/root' WHERE id = ?",
            (root,),
        )
        conn.commit()

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "spec it", "assignee": "alice", "parents": []},
                {"title": "implement it", "assignee": "bob", "parents": [0]},
            ],
            author="decomposer",
        )
        assert child_ids is not None and len(child_ids) == 2

        for cid in child_ids:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (cid,),
            ).fetchone()
            assert row["workspace_kind"] == "worktree"
            # Each child resolves its own <repo>/.worktrees/<child-id> at
            # dispatch; the root's literal path must never be shared.
            assert row["workspace_path"] is None




def test_resolve_worktree_falls_back_when_path_occupied(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    occupied = _add_worktree(repo, repo / ".worktrees" / "sibling", "wt/sibling")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="second sibling",
            workspace_kind="worktree",
            workspace_path=str(occupied),  # inherited shared/stale path
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert (occupied / "README.md").exists()
    head = subprocess.run(
        ["git", "-C", str(occupied), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "wt/sibling"


def test_repo_root_materializes_requested_branch(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="requested branch",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feature/requested",
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == repo / ".worktrees" / tid
    assert branch == "feature/requested"
    assert _current_branch(workspace) == "feature/requested"


@pytest.mark.parametrize("dirty", [False, True], ids=["clean", "dirty"])
def test_linked_checkout_with_different_requested_branch_is_not_reused(
    kanban_home, tmp_path, dirty
):
    repo = _make_repo(tmp_path)
    linked = _add_worktree(repo, tmp_path / "linked-source", "feature/occupied")
    if dirty:
        (linked / "README.md").write_text("dirty tracked file\n", encoding="utf-8")
        (linked / "untracked.txt").write_bytes(b"dirty untracked file\n")
    before = _checkout_snapshot(linked)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="must isolate branch",
            workspace_kind="worktree",
            workspace_path=str(linked),
            branch_name="feature/requested",
        )
        task = kb.get_task(conn, tid)

    workspace, branch = kb._resolve_worktree_workspace(task)

    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert workspace != linked.resolve()
    assert branch == "feature/requested"
    assert _current_branch(workspace) == "feature/requested"
    assert _checkout_snapshot(linked) == before


def test_external_linked_checkout_on_requested_branch_is_not_adopted(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    linked = _add_worktree(repo, tmp_path / "external checkout", "feature/requested")
    before = _checkout_snapshot(linked)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="must own workspace",
            workspace_kind="worktree",
            workspace_path=str(linked),
            branch_name="feature/requested",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    with pytest.raises(RuntimeError, match="git worktree add failed"):
        kb._resolve_worktree_workspace(task)

    assert _checkout_snapshot(linked) == before
    assert not (repo / ".worktrees" / tid).exists()


def test_cleanup_preserves_external_same_branch_checkout(kanban_home, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    linked = _add_worktree(repo, tmp_path / "external checkout", "feature/requested")
    before = _checkout_snapshot(linked)

    kb._cleanup_worktree_workspace("t_not_owner", str(linked), "feature/requested")

    assert linked.is_dir()
    assert _checkout_snapshot(linked) == before
    assert _branch_exists(repo, "feature/requested")


def test_repo_root_rejects_canonical_symlink_to_external_checkout(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    linked = _add_worktree(repo, tmp_path / "external checkout", "feature/requested")
    before = _checkout_snapshot(linked)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="must not follow canonical symlink",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feature/requested",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    managed = repo / ".worktrees" / tid
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.symlink_to(linked, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        kb._resolve_worktree_workspace(task)

    assert managed.is_symlink()
    assert linked.is_dir()
    assert _checkout_snapshot(linked) == before


def test_persisted_canonical_symlink_is_not_adopted_or_cleaned(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    linked = _add_worktree(repo, tmp_path / "external checkout", "feature/requested")
    before = _checkout_snapshot(linked)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="retry must not adopt symlink target",
            workspace_kind="worktree",
            workspace_path=str(repo / ".worktrees" / "placeholder"),
            branch_name="feature/requested",
        )
        managed = repo / ".worktrees" / tid
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.symlink_to(linked, target_is_directory=True)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(managed), tid),
        )
        conn.commit()
        task = kb.get_task(conn, tid)

    assert task is not None
    with pytest.raises(RuntimeError, match="symlink"):
        kb._resolve_worktree_workspace(task)

    kb._cleanup_worktree_workspace(tid, str(managed), "feature/requested")

    assert managed.is_symlink()
    assert linked.is_dir()
    assert _checkout_snapshot(linked) == before
    assert _branch_exists(repo, "feature/requested")


def test_repo_root_rejects_symlinked_canonical_worktrees_parent(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    external_parent = tmp_path / "external worktrees"

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="must not follow canonical parent symlink",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feature/requested",
        )
        task = kb.get_task(conn, tid)

    assert task is not None
    linked = _add_worktree(repo, external_parent / tid, "feature/requested")
    before = _checkout_snapshot(linked)
    (repo / ".worktrees").symlink_to(external_parent, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        kb._resolve_worktree_workspace(task)

    assert linked.is_dir()
    assert _checkout_snapshot(linked) == before


def test_cleanup_preserves_checkout_behind_symlinked_canonical_parent(
    kanban_home, tmp_path
):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    external_parent = tmp_path / "external worktrees"
    tid = "t_parent_alias"
    linked = _add_worktree(repo, external_parent / tid, "feature/requested")
    _git(linked, "push", "-u", "origin", "feature/requested")
    before = _checkout_snapshot(linked)
    (repo / ".worktrees").symlink_to(external_parent, target_is_directory=True)

    kb._cleanup_worktree_workspace(
        tid, str(repo / ".worktrees" / tid), "feature/requested"
    )

    assert linked.is_dir()
    assert _checkout_snapshot(linked) == before
    assert _branch_exists(repo, "feature/requested")


@pytest.mark.parametrize("branch_preexists", [False, True], ids=["created", "pre-existing"])
def test_post_add_validation_failure_rolls_back_only_created_artifacts(
    kanban_home, tmp_path, monkeypatch, branch_preexists
):
    repo = _make_repo(tmp_path)
    target = repo / ".worktrees" / "t_rollback"
    branch = "feature/rollback"
    if branch_preexists:
        _git(repo, "branch", branch)

    def reject_created_worktree(path: Path, requested_branch: str) -> None:
        assert path == target
        assert requested_branch == branch
        raise RuntimeError("forced post-add validation failure")

    monkeypatch.setattr(kb, "_require_worktree_branch", reject_created_worktree)

    with pytest.raises(
        RuntimeError, match="forced post-add validation failure"
    ) as exc_info:
        kb._ensure_git_worktree(repo, target, branch)

    assert "worktree rollback failed" not in str(exc_info.value)
    assert not target.exists()
    assert _branch_exists(repo, branch) is branch_preexists


def test_dir_workspace_reuses_existing_path_without_branch(kanban_home, tmp_path):
    workspace = tmp_path / "shared-dir"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_bytes(b"untouched\n")

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="explicit reuse",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.get_task(conn, tid)

    assert task.branch_name is None
    assert kb.resolve_workspace(task) == workspace
    assert sentinel.read_bytes() == b"untouched\n"


def test_branch_mismatch_fails_before_spawn_and_preserves_linked_checkout(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="fail closed",
            assignee="default",
            workspace_kind="worktree",
            workspace_path=str(repo),
            branch_name="feature/requested",
        )
        occupied = _add_worktree(
            repo, repo / ".worktrees" / tid, "feature/observed"
        )
        (occupied / "README.md").write_text("dirty tracked file\n", encoding="utf-8")
        (occupied / "untracked.txt").write_bytes(b"dirty untracked file\n")
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(occupied), tid),
        )
        conn.commit()
        before = _checkout_snapshot(occupied)
        spawn_calls = []

        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawn_calls.append((args, kwargs)),
            reconcile_orphans=False,
        )
        task = kb.get_task(conn, tid)

    assert result.spawned == []
    assert spawn_calls == []
    assert task.branch_name == "feature/requested"
    assert "requested branch 'feature/requested'" in task.last_failure_error
    assert "observed 'feature/observed'" in task.last_failure_error
    assert _checkout_snapshot(occupied) == before


def test_cleanup_preserves_clean_canonical_checkout_on_branch_mismatch(
    kanban_home, tmp_path
):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        check=True, capture_output=True, text=True,
    )
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    tid = "t_branch_mismatch"
    stored_branch = f"wt/{tid}"
    _git(repo, "branch", stored_branch)
    occupied = _add_worktree(
        repo, repo / ".worktrees" / tid, "feature/observed"
    )
    _git(occupied, "push", "-u", "origin", "feature/observed")
    before = _checkout_snapshot(occupied)

    kb._cleanup_worktree_workspace(tid, str(occupied), stored_branch)

    assert occupied.is_dir()
    assert _checkout_snapshot(occupied) == before
    assert _branch_exists(repo, "feature/observed")
    assert _branch_exists(repo, stored_branch)




