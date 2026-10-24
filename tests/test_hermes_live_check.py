"""Tests for scripts/hermes-live-check (G8-3).

Known-red controls, one per behaviour the card demands:

(a) a commit on an unmerged side branch is NOT-LIVE even though it exists
    in the repo (a check that only tests "sha exists" would pass it);
(b) a launcher whose exec line names no hermes-agent-<sha> release path is
    UNKNOWN, never LIVE;
(c) the same side-branch commit, after being merged into the release line,
    is LIVE.

Plus the UNKNOWN edges: a query sha the repo does not know, an unreadable
launcher, a launcher naming a release dir that is missing, and a release
sha the repo has no commit for.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "hermes-live-check"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "hermes-live-check@example.com")
    _git(repo, "config", "user.name", "hermes-live-check test")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "base")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "--allow-empty", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _launcher(tmp_path: Path, release_sha: str, *, make_dir: bool = True) -> Path:
    """A fake launcher wrapper: last line execs <releases>/hermes-agent-<sha>/hermes."""
    release_dir = (
        tmp_path / "home" / ".hermes" / "releases" / f"hermes-agent-{release_sha}"
    )
    if make_dir:
        release_dir.mkdir(parents=True)
    launcher = tmp_path / "hermes"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        f'exec "{release_dir}/hermes" "$@"\n',
        encoding="utf-8",
    )
    return launcher


def run_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True
    )


def test_unmerged_side_branch_commit_is_not_live(tmp_path: Path, repo: Path) -> None:
    """(a) S exists in the repo but is no ancestor of the live release."""
    release = _commit(repo, "release R")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "side S")
    launcher = _launcher(tmp_path, release)
    proc = run_check(side[:7], "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"NOT-LIVE {side[:7]} not in release {release[:7]}"


def test_launcher_without_release_sha_is_unknown(tmp_path: Path, repo: Path) -> None:
    """(b) an exec line with no hermes-agent-<sha> path is UNKNOWN, never LIVE."""
    sha = _commit(repo, "whatever")
    launcher = tmp_path / "hermes"
    launcher.write_text(
        '#!/usr/bin/env bash\nexec "/usr/local/bin/hermes" "$@"\n', encoding="utf-8"
    )
    proc = run_check(sha, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")


def test_merged_commit_is_live(tmp_path: Path, repo: Path) -> None:
    """(c) once S is merged into the release line, it is LIVE."""
    _commit(repo, "base2")
    _git(repo, "checkout", "-q", "-b", "feature")
    side = _commit(repo, "side S")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-edit", "feature")
    release = _git(repo, "rev-parse", "HEAD")
    launcher = _launcher(tmp_path, release)
    proc = run_check(side, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"LIVE {side[:7]} in release {release[:7]}"


def test_release_sha_itself_is_live(tmp_path: Path, repo: Path) -> None:
    """The release commit queried by a short prefix resolves and is LIVE."""
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    proc = run_check(release[:9], "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"LIVE {release[:7]} in release {release[:7]}"


def test_sha_unknown_to_repo_is_unknown(tmp_path: Path, repo: Path) -> None:
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    proc = run_check("deadbee", "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
    assert "deadbee" in proc.stdout


def test_missing_launcher_is_unknown(tmp_path: Path, repo: Path) -> None:
    sha = _commit(repo, "x")
    proc = run_check(
        sha, "--launcher", str(tmp_path / "no-such-launcher"), "--repo", str(repo)
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")


def test_release_dir_missing_is_unknown(tmp_path: Path, repo: Path) -> None:
    """A launcher naming a release dir that is not on disk answers UNKNOWN."""
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release, make_dir=False)
    proc = run_check(release, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
    assert "release" in proc.stdout


def test_release_sha_unknown_to_repo_is_unknown(tmp_path: Path, repo: Path) -> None:
    """The launcher's release sha has no commit in the repo: never LIVE."""
    _commit(repo, "only")
    head = _git(repo, "rev-parse", "HEAD")
    launcher = _launcher(tmp_path, "f" * 40)
    proc = run_check(head, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
