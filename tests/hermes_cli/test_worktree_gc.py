"""Behavior contracts for hermes_cli.worktree_gc (attended reclaim).

Each guard gets its own contract against a REAL git repo fixture (no mocks —
the entire value of these tests is exercising actual git verdicts):

- clean + fully merged tree        → reap
- untracked-only dirt              → reap-archive (files archived, then removed)
- tracked modifications            → keep, any age
- unique unpushed commits          → keep
- patch-equivalent commits (rebase/squash-merge leak) → reap
- live-locked tree                 → keep
- kanban t_<hex> tree              → keep (owned by kanban gc)
- branch GC: merged branch deleted, unique-commit branch kept,
  checked-out branch kept, protected names kept
- reclaim operates ONLY on the frozen audit list (concurrent-session trap)
"""

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import worktree_gc


def _git(args, cwd, env=None):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    if env:
        e.update(env)
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(cwd), env=e,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """origin (bare) + clone with .worktrees/, HOME redirected for archives."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare", "-b", "main"], origin)

    clone = tmp_path / "repo"
    _git(["clone", str(origin), str(clone)], tmp_path)
    (clone / "README.md").write_text("hello\n")
    _git(["add", "."], clone)
    _git(["commit", "-m", "init"], clone)
    _git(["push", "origin", "main"], clone)
    # origin/HEAD so upstream resolution works like a real clone.
    _git(["remote", "set-head", "origin", "main"], clone)
    (clone / ".worktrees").mkdir()
    return clone


def _add_worktree(repo_path, name, branch=None):
    tree = repo_path / ".worktrees" / name
    branch = branch or f"hermes/{name}"
    _git(["worktree", "add", str(tree), "-b", branch], repo_path)
    return tree, branch


def _verdict(records, name):
    match = [record for record in records if record.name == name]
    assert match, f"no record for {name}"
    return match[0]


class TestAuditVerdicts:
    def test_clean_merged_tree_reaps(self, repo):
        _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-clean").verdict == "reap"

    def test_tracked_modifications_keep(self, repo):
        tree, _ = _add_worktree(repo, "hermes-dirty")
        (tree / "README.md").write_text("edited\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-dirty")
        assert record.verdict == "keep"
        assert "tracked" in record.reason

    def test_untracked_only_is_reap_archive(self, repo):
        tree, _ = _add_worktree(repo, "hermes-scratch")
        (tree / "PR_BODY_DRAFT.md").write_text("draft\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-scratch")
        assert record.verdict == "reap-archive"
        assert record.untracked == ["PR_BODY_DRAFT.md"]

    def test_unique_unpushed_commits_keep(self, repo):
        tree, _ = _add_worktree(repo, "hermes-work")
        (tree / "new.py").write_text("x = 1\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "unique work"], tree)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-work")
        assert record.verdict == "keep"
        assert "unpushed" in record.reason

    def test_patch_equivalent_commits_reap(self, repo):
        """The squash/rebase-merge leak: local commit unreachable from any
        remote ref but patch-equivalent to an upstream commit → merged work."""
        tree, _ = _add_worktree(repo, "hermes-merged")
        (tree / "feat.py").write_text("y = 2\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "feat"], tree)
        sha = _git(["rev-parse", "HEAD"], tree)
        # "Merge" it to main with a DIFFERENT committer so the cherry-pick
        # produces a distinct sha (same-second identical-committer cherry
        # picks can produce the identical sha — pitfall from the skill).
        _git(["cherry-pick", sha], repo,
             env={"GIT_COMMITTER_NAME": "other", "GIT_COMMITTER_EMAIL": "o@o"})
        _git(["push", "origin", "main"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-merged").verdict == "reap"

    def test_live_locked_tree_keeps(self, repo):
        tree, _ = _add_worktree(repo, "hermes-live")
        _git(["worktree", "lock", str(tree),
              "--reason", f"hermes pid={os.getpid()}"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "hermes-live")
        assert record.verdict == "keep"
        assert "in use" in record.reason

    def test_kanban_tree_untouched(self, repo):
        _add_worktree(repo, "t_deadbeef", branch="kanban/t_deadbeef")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        record = _verdict(records, "t_deadbeef")
        assert record.verdict == "keep"
        assert "kanban" in record.reason


class TestReclaim:
    def test_reap_removes_tree_and_branch(self, repo):
        tree, branch = _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        actions = worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert any("removed hermes-clean" in a for a in actions)
        assert not tree.exists()
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert probe.returncode != 0, "branch should be gone with its tree"

    def test_untracked_files_archived_under_the_active_profile_home(self, repo, tmp_path, monkeypatch):
        """The archive follows the active Hermes home (a named profile here), never ~/.hermes."""
        native_home = tmp_path / "native"
        profile_home = tmp_path / "root" / "profiles" / "work"
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: native_home)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        tree, _ = _add_worktree(repo, "hermes-scratch")
        (tree / "NOTES.md").write_text("important scribbles\n")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert not tree.exists()
        archived = list((profile_home / "archive" / "worktree-prune").rglob("NOTES.md"))
        assert archived, "untracked file must be archived under the profile home, not destroyed"
        assert archived[0].read_text() == "important scribbles\n"
        assert not (native_home / ".hermes").exists()

    def test_dry_run_changes_nothing(self, repo):
        tree, _ = _add_worktree(repo, "hermes-clean")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        actions = worktree_gc.reclaim_worktrees(
            str(repo), dry_run=True, records=records
        )
        assert any("would remove" in a for a in actions)
        assert tree.exists()

    def test_frozen_list_ignores_trees_created_after_audit(self, repo):
        """Concurrent-session trap: a tree created between audit and reclaim
        must be out of scope by construction."""
        _add_worktree(repo, "hermes-old")
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        late_tree, _ = _add_worktree(repo, "hermes-late")
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert late_tree.exists(), "tree created after the audit must survive"

    def test_dead_locked_tree_is_unlocked_and_reaped(self, repo):
        tree, _ = _add_worktree(repo, "hermes-zombie")
        _git(["worktree", "lock", str(tree),
              "--reason", "hermes pid=999999999"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        assert _verdict(records, "hermes-zombie").verdict == "reap"
        worktree_gc.reclaim_worktrees(str(repo), records=records)
        assert not tree.exists()


class TestBranchGC:
    def test_merged_branch_deleted_any_name(self, repo):
        """Branch GC is content-gated, not name-gated: any fully-merged local
        branch is safe to delete regardless of prefix."""
        _git(["branch", "salv-12345", "main"], repo)
        _git(["branch", "feat/some-old-thing", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["salv-12345"].verdict == "delete"
        assert by_name["feat/some-old-thing"].verdict == "delete"
        worktree_gc.reclaim_branches(str(repo), records=records)
        out = _git(["branch", "--format=%(refname:short)"], repo)
        assert "salv-12345" not in out
        assert "feat/some-old-thing" not in out

    def test_unique_commit_branch_kept(self, repo):
        _git(["checkout", "-b", "feat/real-work"], repo)
        (repo / "wip.py").write_text("z = 3\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "wip"], repo)
        _git(["checkout", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["feat/real-work"].verdict == "keep"
        assert "unique" in by_name["feat/real-work"].reason

    def test_patch_equivalent_branch_deleted(self, repo):
        """Rebase-merged PR branch: SHAs differ from main but every commit is
        patch-equivalent — the dominant branch leak."""
        _git(["checkout", "-b", "fix/landed"], repo)
        (repo / "fix.py").write_text("a = 4\n")
        _git(["add", "."], repo)
        _git(["commit", "-m", "fix"], repo)
        sha = _git(["rev-parse", "HEAD"], repo)
        _git(["checkout", "main"], repo)
        _git(["cherry-pick", sha], repo,
             env={"GIT_COMMITTER_NAME": "other", "GIT_COMMITTER_EMAIL": "o@o"})
        _git(["push", "origin", "main"], repo)
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["fix/landed"].verdict == "delete"
        assert "patch-equivalent" in by_name["fix/landed"].reason

    def test_checked_out_and_protected_kept(self, repo):
        _tree, branch = _add_worktree(repo, "hermes-active")
        records = worktree_gc.audit_branches(str(repo))
        by_name = {record.name: record for record in records}
        assert by_name["main"].verdict == "keep"
        assert by_name[branch].verdict == "keep"


# --- An externally-owned worktree is never reclaimed (gate (b)) ---

class TestReclaimRespectsExternalSessionOwnership:
    """``reclaim_worktrees`` removes linked worktrees that the git-tier gates
    (clean, pushed, merged) consider reaping. A kanban external session may own
    such a tree through an active binding or a never-claimed card — recorded in
    a kanban store, invisible to git. Before removing path P the route must ask
    ``_externally_owned_workspace(None, P)`` (gate (b)); True or unanswerable
    means keep, with the reason reported."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        h = tmp_path / ".hermes"
        h.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(h))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                    "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT"):
            monkeypatch.delenv(key, raising=False)
        from hermes_cli import kanban_db as kb
        kb.init_db()
        return tmp_path

    @staticmethod
    def _own(worktree: Path) -> None:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli import kanban_db_external as ext
        with kbc.connect_closing() as c:
            tid = kb.create_task(c, title="live session",
                                 execution_kind="external_session")
            ext.external_claim(
                c, tid, board="default", owner_host="test-host",
                incarnation=dict(session="prune", id="$7", created="1", pane="%7",
                                 pid="0777", backend="claude", conversation="prune-c"),
                worktree=str(worktree), reservation_id="prune-res",
                reservation_token="e" * 64)

    def test_owned_clean_pushed_tree_is_kept(self, repo, home):
        """The review's exact input: a clean, pushed linked worktree
        ``.worktrees/moll-e-session`` owned by an active external_claim."""
        tree, branch = _add_worktree(repo, "moll-e-session")
        (tree / "session.txt").write_text("session work\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "session work"], tree)
        _git(["push", "origin", f"{branch}:{branch}"], repo)
        self._own(tree)
        from hermes_cli import kanban_db_workspace as kbw
        assert kbw._externally_owned_workspace(None, str(tree)) is True, (
            "precondition: gate (b) says the tree is owned")
        actions = worktree_gc.reclaim_worktrees(str(repo))
        assert tree.is_dir(), (
            f"reclaim removed the live session's owned worktree: {actions}")
        assert any("moll-e-session" in a and "external" in a for a in actions), actions

    def test_unowned_clean_pushed_tree_is_still_removed(self, repo, home):
        """Control: same shape, no owner — the route still reclaims."""
        tree, branch = _add_worktree(repo, "hermes-unowned")
        (tree / "session.txt").write_text("session work\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "session work"], tree)
        _git(["push", "origin", f"{branch}:{branch}"], repo)
        actions = worktree_gc.reclaim_worktrees(str(repo))
        assert not tree.exists(), actions

    def test_kanban_t_hex_name_is_still_kept(self, repo, home):
        """Control: the t_<hex> name exclusion is untouched."""
        _add_worktree(repo, "t_abc123", branch="kanban/t_abc123")
        actions = worktree_gc.reclaim_worktrees(str(repo))
        assert (repo / ".worktrees" / "t_abc123").is_dir(), actions

    def test_unreadable_ownership_keeps_the_tree(self, repo, home, monkeypatch):
        """Fail closed: when the ownership question cannot be answered, keep."""
        tree, branch = _add_worktree(repo, "hermes-answerless")
        (tree / "session.txt").write_text("session work\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "session work"], tree)
        _git(["push", "origin", f"{branch}:{branch}"], repo)
        from hermes_cli import kanban_db_workspace as kbw
        monkeypatch.setattr(kbw, "_externally_owned_workspace",
                            lambda conn, path: (_ for _ in ()).throw(
                                RuntimeError("ownership unanswerable")))
        actions = worktree_gc.reclaim_worktrees(str(repo))
        assert tree.is_dir(), actions
        assert any("hermes-answerless" in a and "could not be read" in a for a in actions), actions

    def test_kept_reason_says_ownership_could_not_be_read_not_owned(
            self, repo, home, monkeypatch):
        """Defect 5: when the ownership question cannot be ANSWERED (import
        failure or unreadable store), the kept reason must say ownership
        could not be read — not claim 'external session owns the worktree',
        which sends the user hunting for a binding that may not exist."""
        tree, branch = _add_worktree(repo, "hermes-answerless")
        (tree / "session.txt").write_text("session work\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "session work"], tree)
        _git(["push", "origin", f"{branch}:{branch}"], repo)
        from hermes_cli import kanban_db_workspace as kbw
        monkeypatch.setattr(kbw, "_externally_owned_workspace",
                            lambda conn, path: (_ for _ in ()).throw(
                                RuntimeError("ownership unanswerable")))
        actions = worktree_gc.reclaim_worktrees(str(repo))
        assert tree.is_dir(), actions
        kept = [a for a in actions if "hermes-answerless" in a]
        assert kept, actions
        assert "could not be read" in kept[0], kept[0]
        assert "owns the worktree" not in kept[0], kept[0]

    def test_unreadable_store_kept_reason_names_the_store_not_an_owner(
            self, repo, home):
        """Defect 5, the unreadable-store path through the REAL gate (no
        monkeypatch): a board whose kanban.db is mode 000 cannot be opened,
        so ownership is unanswerable and the tree is kept — but the kept
        line must say ownership could not be read, not that a session owns
        the worktree."""
        import os
        from hermes_cli import kanban_db as kb
        tree, branch = _add_worktree(repo, "hermes-answerless")
        (tree / "session.txt").write_text("session work\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "session work"], tree)
        _git(["push", "origin", f"{branch}:{branch}"], repo)
        kb.create_board("broken-store")
        broken = kb.board_dir("broken-store") / "kanban.db"
        os.chmod(broken, 0)
        try:
            actions = worktree_gc.reclaim_worktrees(str(repo))
        finally:
            os.chmod(broken, 0o644)
        assert tree.is_dir(), actions
        kept = [a for a in actions if "hermes-answerless" in a]
        assert kept, actions
        assert "could not be read" in kept[0], kept[0]
        assert "owns the worktree" not in kept[0], kept[0]
