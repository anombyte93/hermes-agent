"""Unit tests for the pure workspace-evidence harvester (:mod:`hermes_cli.kanban_harvest`).

Log excerpts below are COPIED verbatim from real Align-board worker logs
(``~/.hermes/kanban/boards/align-apple-feel-20260913/logs/t_46dd8fdd.log`` and
``t_1708fc1d.log``, 2026-09-13) so the regex extraction is exercised against
the actual byte shapes the dispatcher will see, not invented fixtures.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.kanban_harvest import (
    harvest_workspace,
    parse_test_counts,
    compare_harvest_to_self_report,
)


# ---------------------------------------------------------------------------
# Verbatim excerpts (Align board, lane C log t_46dd8fdd.log — the worker's own
# final summary lines; the log is a TUI render so prose lines carry the counts).
# ---------------------------------------------------------------------------
LANE_C_TAIL = """\
- Typecheck: passed
- Unit: 483 passed across 53 files
- Build: passed, 422 modules transformed
- Native-weekly: 19 passed
- Isolated Chromium e2e: 320/320 passed
- Git diff check: passed
"""

# Lane A log t_1708fc1d.log — narration mid-run, LAST occurrence is the
# authoritative one (485, not any earlier partial count).
LANE_A_TAIL = """\
Unit tests: 485 passed (52 files), all green. Now run typecheck and build quickly
Unit: 485 passed / 52 files. Now typecheck + build, then the long e2e acceptance run:
"""


# --- parse_test_counts -----------------------------------------------------

def test_parse_counts_extracts_lane_c_real_log_tail():
    counts = parse_test_counts(LANE_C_TAIL)
    assert counts == {
        "unit": 483,
        "e2e": 320,
        "native": 19,
    }


def test_parse_counts_last_occurrence_wins_from_lane_a_excerpt():
    counts = parse_test_counts(LANE_A_TAIL)
    assert counts["unit"] == 485


def test_parse_counts_pytest_short_summary_line():
    """The canonical pytest summary line ``N passed, M failed in X.XXs``."""
    counts = parse_test_counts("=== 12 passed, 1 failed, 2 skipped in 3.45s ===")
    assert counts == {"pytest": 13}


def test_parse_counts_vitest_row():
    counts = parse_test_counts("Test Files  4 passed (4)\n     Tests  64 passed (64)")
    assert counts == {"vitest": 64}


def test_parse_counts_jest_row():
    counts = parse_test_counts("Tests:       8 passed, 8 total\nSnapshots:   0 total")
    assert counts == {"jest": 8}


def test_parse_counts_empty_and_negative_text():
    assert parse_test_counts("") == {}
    assert parse_test_counts("no test output here") == {}
    # Failure narration is not a passing-run summary; must not be harvested.
    assert parse_test_counts("47 failed / 57 passed") == {}
    assert parse_test_counts("First run: 256 passed, 66 unrelated tests failed") == {}


# --- harvest_workspace -----------------------------------------------------

def _git(path: Path, *args: str):
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A base commit + two lane commits + one dirty file, like an Align lane."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    base = r / "base.txt"
    base.write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base commit")
    base_sha = subprocess.run(
        ["git", "-C", str(r), "rev-parse", "HEAD"], capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    (r / "lane.txt").write_text("one\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feat: first lane commit")
    (r / "lane.txt").write_text("one\ntwo\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feat: second lane commit")
    # A second lane file so the range diffstat genuinely spans 2 files
    # (numstat lists lane.txt once for the whole range, not once per commit).
    (r / "other.txt").write_text("a\nb\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "feat: third lane commit")
    (r / "dirty.txt").write_text("uncommitted\n")
    (r / "untracked.txt").write_text("never added\n")
    (r / ".git").joinpath("base_marker").write_text(base_sha + "\n")
    return r


def _base_sha(repo: Path) -> str:
    return (repo / ".git" / "base_marker").read_text().strip()


def test_harvest_workspace_full_git(repo: Path):
    h = harvest_workspace(repo, _base_sha(repo))
    assert h["head_sha"] == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert [c["subject"] for c in h["commits"]] == [
        "feat: third lane commit", "feat: second lane commit", "feat: first lane commit",
    ]
    assert all(len(c["sha"]) == 40 for c in h["commits"])
    # Range diffstat: lane.txt (+2 across two commits) and other.txt (+2),
    # aggregated per path = 2 files, +4 lines.
    assert h["diffstat"]["files"] == 2
    assert h["diffstat"]["insertions"] == 4
    assert h["diffstat"]["deletions"] == 0
    # dirty.txt modified + untracked.txt untracked.
    assert h["dirty"] == 2
    assert h["branch"]
    assert h["test_counts"] == {}


def test_harvest_workspace_with_log(repo: Path, tmp_path: Path):
    log = tmp_path / "t_xy.log"
    log.write_text(LANE_C_TAIL)
    h = harvest_workspace(repo, _base_sha(repo), log_path=log)
    assert h["test_counts"] == {"unit": 483, "e2e": 320, "native": 19}


def test_harvest_workspace_missing_dir_is_partial(tmp_path: Path):
    h = harvest_workspace(tmp_path / "nope", None)
    assert h["head_sha"] is None
    assert h["commits"] == []
    assert h["dirty"] == 0
    assert h["diffstat"] == {"files": 0, "insertions": 0, "deletions": 0}
    assert h["branch"] is None


def test_harvest_workspace_non_git_is_partial(tmp_path: Path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "f.txt").write_text("x")
    h = harvest_workspace(d, None)
    assert h["head_sha"] is None
    assert h["commits"] == []
    assert h["git"] == "absent"


def test_harvest_workspace_never_throws_on_bad_sha(repo: Path):
    h = harvest_workspace(repo, "0" * 40)
    assert h["head_sha"]  # git facts still present
    assert h["commits"] == []  # but the range is unusable


# --- compare_harvest_to_self_report ---------------------------------------

def test_agreement_match_on_lane_c_facts():
    harvest = {"head_sha": "9662e5439592aa24e08b19f91f67098da1c89e5e",
               "test_counts": {"unit": 483, "e2e": 320}}
    self_report = {"head_sha": "9662e5439592aa24e08b19f91f67098da1c89e5e",
                   "tests_run": {"unit": "53 files, 483 tests passed",
                                 "e2e": "executed 320, passed 320, failed 0"}}
    agreement = compare_harvest_to_self_report(harvest, self_report)
    assert agreement == {"head_sha": "match", "tests_run": "match"}


def test_agreement_mismatch_on_head_sha():
    harvest = {"head_sha": "aaa", "test_counts": {}}
    self_report = {"head_sha": "bbb"}
    agreement = compare_harvest_to_self_report(harvest, self_report)
    assert agreement == {"head_sha": "mismatch"}


def test_agreement_mismatch_on_unit_count():
    harvest = {"head_sha": "aaa", "test_counts": {"unit": 483}}
    self_report = {"head_sha": "aaa", "tests_run": {"unit": "490 passed"}}
    agreement = compare_harvest_to_self_report(harvest, self_report)
    assert agreement == {"head_sha": "match", "tests_run": "mismatch"}


def test_agreement_worker_silent():
    """Lane A died before writing any metadata: nothing to compare."""
    harvest = {"head_sha": "aaa", "test_counts": {"unit": 485}}
    agreement = compare_harvest_to_self_report(harvest, None)
    assert agreement == {"head_sha": "worker_silent", "tests_run": "worker_silent"}


def test_agreement_partial_self_report():
    harvest = {"head_sha": "aaa", "test_counts": {}}
    self_report = {"tests_run": {"unit": "485 passed"}}
    agreement = compare_harvest_to_self_report(harvest, self_report)
    assert agreement == {"head_sha": "worker_silent", "tests_run": "worker_silent"}
