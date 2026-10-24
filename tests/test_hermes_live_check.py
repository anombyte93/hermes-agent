"""Tests for scripts/hermes-live-check (G8-3).

Known-red controls, one per behaviour the card demands:

(a) a commit on an unmerged side branch is NOT-LIVE even though it exists
    in the repo (a check that only tests "sha exists" would pass it);
(b) a launcher whose exec line names no hermes-agent-<sha> release path is
    UNKNOWN, never LIVE;
(c) a side-branch commit merged --no-ff into a main that moved on is LIVE:
    a STRICT ancestor (side != release), so an equality-only rule fails it.

Plus the UNKNOWN edges: a query sha the repo does not know, an unreadable
launcher, a launcher naming a release dir that is missing, and a release
sha the repo has no commit for.

Review r1 controls: the launcher's last line is read exactly as
hermes-release.sh status reads it (tail -n1, trailing blank lines
included); a git error is UNKNOWN, never NOT-LIVE; there is no --fetch and
nothing under .git is written; an unreadable launcher is UNKNOWN.
"""

from __future__ import annotations

import os
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


def run_check(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env
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
    """(c) once S is merged into the release line, it is LIVE.

    S must be a STRICT ancestor of the release (side != release), on a real
    branch that diverged from main, so an equality-only rule cannot pass.
    """
    _commit(repo, "base2")
    _git(repo, "checkout", "-q", "-b", "feature")
    side = _commit(repo, "side S")
    _git(repo, "checkout", "-q", "main")
    main_moved = _commit(repo, "main moves on")  # feature is now a real branch
    # Before the merge, S is not in main's history: NOT-LIVE.
    pre_launcher = _launcher(tmp_path / "pre", main_moved)
    pre = run_check(side, "--launcher", str(pre_launcher), "--repo", str(repo))
    assert pre.returncode == 1, pre.stdout + pre.stderr
    _git(repo, "merge", "--no-ff", "--no-edit", "feature")
    release = _git(repo, "rev-parse", "HEAD")
    assert side != release
    assert release not in (side, main_moved)
    assert _git(repo, "rev-parse", "HEAD^2") == side  # a true merge commit
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


# --- Review r1 finding 1: the launcher's last line, read exactly as
# hermes-release.sh live_release() reads it (`tail -n1 | grep -oE`).

RELEASE_RE_SH = r"(/[A-Za-z0-9._@+-]+)+/releases/hermes-agent-[0-9a-f]{7,40}"


def _status_release_paths(launcher: Path) -> list[str]:
    """What hermes-release.sh status sees: tail -n1 | grep -oE | sort -u."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"tail -n1 \"$1\" | grep -oE '{RELEASE_RE_SH}' | sort -u || true",
            "_",
            str(launcher),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.split("\n") if line]


def test_trailing_blank_line_launcher_is_unknown(tmp_path: Path, repo: Path) -> None:
    """tail -n1 of 'exec R\\n\\n' is the empty line: status says UNKNOWN, so must we."""
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    launcher.write_text(launcher.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert _status_release_paths(launcher) == []  # the control: status is UNKNOWN
    proc = run_check(release, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")


@pytest.mark.parametrize(
    ("tail_text", "status_live"),
    [
        ("\n", True),  # exactly one terminal newline (the normal wrapper)
        ("", True),  # no terminal newline: tail -n1 returns the partial line
        ("\n\n", False),  # one trailing blank line
        ("\n\n\n", False),  # several trailing blank lines
        ("\n   \n", False),  # a trailing whitespace-only line
        ("\r\n", True),  # CRLF: the CR stays on the line, the path matches
        ("\rjunk\n", True),  # a bare CR is not a line break for tail
    ],
)
def test_last_line_matches_release_status(
    tmp_path: Path, repo: Path, tail_text: str, status_live: bool
) -> None:
    """LIVE exactly when hermes-release.sh status reads one release path."""
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    body = launcher.read_text(encoding="utf-8").removesuffix("\n")
    launcher.write_bytes((body + tail_text).encode("utf-8"))
    # The control: the real tail -n1 | grep -oE pipeline gives the expected view.
    assert (len(_status_release_paths(launcher)) == 1) is status_live
    proc = run_check(release, "--launcher", str(launcher), "--repo", str(repo))
    if status_live:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.strip() == f"LIVE {release[:7]} in release {release[:7]}"
    else:
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert proc.stdout.startswith("UNKNOWN:")


# --- Review r1 finding 2: a git error is UNKNOWN/2, never NOT-LIVE/1.


def test_ancestry_git_error_is_unknown(tmp_path: Path, repo: Path) -> None:
    """merge-base exits 128 on an unreadable parent object: UNKNOWN, not NOT-LIVE."""
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "side S")
    _git(repo, "checkout", "-q", "main")
    lost = _commit(repo, "parent X")
    release = _commit(repo, "release R")
    (repo / ".git" / "objects" / lost[:2] / lost[2:]).unlink()
    # The control: both shas still resolve, but the ancestry walk errors (128).
    probe = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", side, release],
        capture_output=True,
        text=True,
    )
    assert probe.returncode not in (0, 1), probe
    launcher = _launcher(tmp_path, release)
    proc = run_check(side, "--launcher", str(launcher), "--repo", str(repo))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
    assert "NOT-LIVE" not in proc.stdout


def test_git_missing_is_unknown(tmp_path: Path, repo: Path) -> None:
    """No git on PATH is UNKNOWN/2, not a traceback with exit 1 (= NOT-LIVE)."""
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    proc = run_check(
        release,
        "--launcher",
        str(launcher),
        "--repo",
        str(repo),
        env={"PATH": str(empty_bin), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")


# --- Review r1 finding 3: the checker never writes; there is no --fetch.


def _git_dir_state(repo: Path) -> dict[str, tuple[int, int]]:
    """Every file under .git with its size and mtime: any write shows up."""
    git_dir = repo / ".git"
    return {
        str(path.relative_to(git_dir)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in git_dir.rglob("*")
        if path.is_file()
    }


def test_fetch_option_is_refused_and_writes_nothing(tmp_path: Path, repo: Path) -> None:
    """--fetch would write objects, refs and FETCH_HEAD: it must not exist."""
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    release = _git(clone, "rev-parse", "HEAD")
    tracking_before = _git(clone, "rev-parse", "refs/remotes/origin/main")
    upstream_new = _commit(repo, "upstream moves on")  # something a fetch would pull
    launcher = _launcher(tmp_path, release)
    before = _git_dir_state(clone)
    proc = run_check(release, "--launcher", str(launcher), "--repo", str(clone), "--fetch")
    assert not (clone / ".git" / "FETCH_HEAD").exists()
    assert _git(clone, "rev-parse", "refs/remotes/origin/main") == tracking_before
    assert _git_dir_state(clone) == before
    probe = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", f"{upstream_new}^{{commit}}"],
        capture_output=True,
    )
    assert probe.returncode != 0, "the upstream commit must not have been fetched"
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")


def test_ordinary_check_writes_nothing(tmp_path: Path, repo: Path) -> None:
    """A normal LIVE/NOT-LIVE answer leaves the repo's .git untouched."""
    release = _commit(repo, "release R")
    _git(repo, "checkout", "-q", "-b", "side")
    side = _commit(repo, "side S")
    launcher = _launcher(tmp_path, release)
    before = _git_dir_state(repo)
    for sha, code in ((release, 0), (side, 1)):
        proc = run_check(sha, "--launcher", str(launcher), "--repo", str(repo))
        assert proc.returncode == code, proc.stdout + proc.stderr
    assert _git_dir_state(repo) == before


# --- Review r1 finding 4: an unreadable launcher is UNKNOWN/2, not a traceback.


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_unreadable_launcher_is_unknown(tmp_path: Path, repo: Path) -> None:
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    launcher.chmod(0)
    try:
        # The control: the file exists and really cannot be read.
        assert launcher.is_file()
        with pytest.raises(PermissionError):
            launcher.read_bytes()
        proc = run_check(release, "--launcher", str(launcher), "--repo", str(repo))
    finally:
        launcher.chmod(0o644)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
    assert "Traceback" not in proc.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a mode-000 dir")
def test_launcher_in_untraversable_dir_is_unknown(tmp_path: Path, repo: Path) -> None:
    release = _commit(repo, "only")
    launcher = _launcher(tmp_path, release)
    locked = tmp_path / "locked"
    locked.mkdir()
    hidden = locked / "hermes"
    hidden.write_bytes(launcher.read_bytes())
    locked.chmod(0)
    try:
        proc = run_check(release, "--launcher", str(hidden), "--repo", str(repo))
    finally:
        locked.chmod(0o755)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("UNKNOWN:")
    assert "Traceback" not in proc.stderr
