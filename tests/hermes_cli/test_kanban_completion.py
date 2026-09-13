"""Completion-contract validator (hermes_cli/kanban_completion.py).

A worker's handoff metadata is a claim, not truth: ``kanban_complete`` validates
it against the workspace (``head_sha`` reachable, ``commits``/``changed_files``
exist, ``tests_run`` carries counts) before the board records the task done.
Levels come from ``kanban.completion_contract`` (off|warn|strict, default warn).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli.kanban_completion import (
    CONTRACT_LEVELS,
    Problem,
    resolve_completion_contract,
    validate_completion,
)

# --- Real handoff from the Align board (run 11, task t_46dd8fdd) — PASS fixture ---
LANE_C_METADATA = {
    "branch": "feat/apple-feel-review-cards-20260913",
    "head_sha": "9662e5439592aa24e08b19f91f67098da1c89e5e",
    "base_sha": "a0929f6",
    "pushed": False,
    "git_log": [
        "9662e54 feat(dashboard): add status ticks and submit haptic",
        "57c1102 feat(ui): add swipe-to-correct Health cards",
        "94a174b feat(ui): add per-day Health review acceptance",
        "2424e50 feat(ui): replace Health table with day cards",
    ],
    "changed_files": [
        "src/lib/format.ts",
        "src/lib/haptics.ts",
        "src/styles.css",
        "src/ui/Dashboard.tsx",
        "src/ui/components/HealthReview.tsx",
        "src/ui/components/health-review-acceptance.ts",
        "src/ui/dashboard-submit.ts",
        "tests/native-weekly/account.spec.ts",
        "tests/unit/dashboard-submit.test.ts",
        "tests/unit/health-review-acceptance.test.ts",
    ],
    "tests_run": {
        "typecheck": "pnpm run typecheck — passed",
        "unit": "pnpm run test — 53 files, 483 tests passed",
        "build": "pnpm run build — passed (422 modules transformed)",
        "e2e": "NODE_ENV=test pnpm run test:e2e:all — expected 320, executed 320, passed 320, failed 0, unknown 0",
        "native_weekly": "NODE_ENV=test pnpm run test:native-weekly — 19 passed",
    },
    "verification_notes": [
        "A direct combined `pnpm run test:e2e` invocation was also attempted; "
        "255 passed, 65 rate-limited failures.",
        "git diff --check a0929f6..HEAD passed",
    ],
    "worker_session_id": "20260913_193010_57ed17",
}


def _init_git_repo(repo: Path) -> str:
    """One-commit git repo; returns the commit sha."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"],
                   check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _commit_file(repo: Path, name: str, text: str, message: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", message],
                   check=True, capture_output=True, text=True)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_contract_levels_and_default():
    assert CONTRACT_LEVELS == ("off", "warn", "strict")
    assert resolve_completion_contract(None) == "warn"          # default
    assert resolve_completion_contract({}) == "warn"
    assert resolve_completion_contract({"completion_contract": "strict"}) == "strict"
    assert resolve_completion_contract({"completion_contract": "off"}) == "off"
    # Unknown / invalid values fall back to the safe default, never to strict.
    assert resolve_completion_contract({"completion_contract": "bogus"}) == "warn"
    assert resolve_completion_contract({"completion_contract": None}) == "warn"
    assert resolve_completion_contract({"completion_contract": 7}) == "warn"


# ---------------------------------------------------------------------------
# off skips everything; non-git skips git checks (and says so)
# ---------------------------------------------------------------------------


def test_off_returns_no_problems_even_for_forged_metadata(tmp_path):
    problems = validate_completion(
        {"head_sha": "f" * 40, "changed_files": ["nope.ts"], "tests_run": {"unit": "pytest"}},
        str(tmp_path), "off")
    assert problems == []


def test_non_git_workspace_skips_git_checks_with_info_note(tmp_path):
    problems = validate_completion({"head_sha": "f" * 40}, str(tmp_path), "warn")
    codes = [p.code for p in problems]
    assert codes == ["non_git_workspace"]
    assert problems[0].severity == "info"
    # Info-only is not a warning: the handoff is accepted.
    assert not any(p.severity == "warning" for p in problems)


def test_no_workspace_path_skips_git_checks():
    problems = validate_completion({"head_sha": "f" * 40}, None, "warn")
    assert [p.code for p in problems] == ["non_git_workspace"]
    assert problems[0].severity == "info"


# ---------------------------------------------------------------------------
# PASS fixture: the real Lane C metadata has verifiable shape
# ---------------------------------------------------------------------------


def test_lane_c_metadata_passes_without_workspace():
    """Real Align handoff (run 11): every tests_run value carries a count or a
    passed/failed verdict; no workspace to check git facts against."""
    problems = validate_completion(LANE_C_METADATA, None, "warn")
    assert [p.code for p in problems] == ["non_git_workspace"]
    assert not any(p.severity == "warning" for p in problems)


def test_lane_c_metadata_against_real_repo_passes(tmp_path):
    head = _init_git_repo(tmp_path / "repo")
    md = dict(LANE_C_METADATA)
    md["head_sha"] = head
    md["changed_files"] = ["README.md"]
    md["commits"] = [head]
    problems = validate_completion(md, str(tmp_path / "repo"), "warn")
    assert problems == []


# ---------------------------------------------------------------------------
# head_sha: missing / forged / unreachable
# ---------------------------------------------------------------------------


def test_missing_head_sha_on_git_workspace_is_a_problem(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    problems = validate_completion({"tests_run": {"unit": 3}}, str(repo), "warn")
    assert [p.code for p in problems] == ["missing_head_sha"]
    assert problems[0].severity == "warning"


def test_forged_head_sha_fails(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    problems = validate_completion({"head_sha": "f" * 40}, str(repo), "warn")
    assert [p.code for p in problems] == ["unknown_commit"]
    assert problems[0].severity == "warning"


def test_malformed_head_sha_fails_cleanly(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    problems = validate_completion({"head_sha": "not-a-sha"}, str(repo), "warn")
    assert [p.code for p in problems] == ["malformed_sha"]
    problems = validate_completion({"head_sha": 12345}, str(repo), "warn")
    assert [p.code for p in problems] == ["malformed_sha"]


def test_unreachable_head_sha_fails(tmp_path):
    """A commit object that exists in the repo but is not an ancestor of the
    workspace HEAD (e.g. a dangling commit-tree) is not verifiable evidence."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    dangler = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", "HEAD^{tree}", "-m", "dangler"],
        check=True, capture_output=True, text=True).stdout.strip()
    problems = validate_completion({"head_sha": dangler}, str(repo), "warn")
    assert [p.code for p in problems] == ["unreachable_head"]


# ---------------------------------------------------------------------------
# commits / changed_files
# ---------------------------------------------------------------------------


def test_commits_must_exist(tmp_path):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    problems = validate_completion(
        {"head_sha": head, "commits": [head, "e" * 40]}, str(repo), "warn")
    assert [p.code for p in problems] == ["unknown_commit"]
    assert "commits" in problems[0].field


def test_commits_entries_may_carry_subjects(tmp_path):
    repo = tmp_path / "repo"
    head = _init_git_repo(repo)
    problems = validate_completion(
        {"head_sha": head, "commits": [f"{head[:12]} feat(ui): the change"]},
        str(repo), "warn")
    assert problems == []


def test_changed_files_checked_at_head_sha(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    head = _commit_file(repo, "src/a.ts", "export {}\n", "add a")
    ok = validate_completion({"head_sha": head, "changed_files": ["README.md", "src/a.ts"]},
                             str(repo), "warn")
    assert ok == []
    bad = validate_completion({"head_sha": head, "changed_files": ["src/gone.ts"]},
                              str(repo), "warn")
    assert [p.code for p in bad] == ["missing_changed_file"]


def test_changed_file_deleted_between_commits_needs_declaration(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_file(repo, "old.txt", "x\n", "add old")
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "old.txt"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "rm old"],
                   check=True, capture_output=True, text=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    bad = validate_completion({"head_sha": head, "changed_files": ["old.txt"]}, str(repo), "warn")
    assert [p.code for p in bad] == ["missing_changed_file"]
    # Declared deleted — either via the sibling key or an inline marker — is fine.
    via_key = validate_completion(
        {"head_sha": head, "changed_files": ["old.txt"], "deleted_files": ["old.txt"]},
        str(repo), "warn")
    assert via_key == []
    via_marker = validate_completion(
        {"head_sha": head, "changed_files": ["old.txt (deleted)"]}, str(repo), "warn")
    assert via_marker == []


# ---------------------------------------------------------------------------
# tests_run: command values need a count or an explicit verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,ok", [
    ("pnpm run test — 53 files, 483 tests passed", True),   # command + count + verdict
    ("pnpm run typecheck — passed", True),                  # command + verdict
    ("expected 320, executed 320, passed 320, failed 0", True),
    ("NODE_ENV=test pnpm run test:native-weekly — 19 passed", True),
    (483, True),                       # bare count
    ("483", True),                     # numeric string
    ("passed", True),                  # verdict only, not a command
    ("skipped (no js)", True),         # prose note, not a command
    ("pnpm run test", False),          # command, no count/verdict
    ("pytest", False),                 # bare runner name is still a command
    ("make test", False),
])
def test_tests_run_values_need_count_or_verdict(tmp_path, value, ok):
    problems = validate_completion({"tests_run": {"t": value}}, None, "warn")
    codes = [p.code for p in problems if p.severity == "warning"]
    assert ("no_verdict" in codes) is (not ok)


def test_tests_run_wrong_shape_is_flagged():
    # non_git_workspace is info-only; filter to warnings like callers do.
    problems = validate_completion({"tests_run": "5 tests ran fine"}, None, "warn")
    assert [p.code for p in problems if p.severity == "warning"] == ["malformed_tests_run"]


def test_tests_run_empty_values_skipped():
    problems = validate_completion({"tests_run": {"a": None, "b": "", "c": 0}}, None, "warn")
    assert [p.code for p in problems if p.severity == "warning"] == []


# ---------------------------------------------------------------------------
# Random fixtures never crash and only emit known codes
# ---------------------------------------------------------------------------


def _random_metadata(rng):
    import random

    def rand_sha():
        alphabet = "0123456789abcdefz"  # 'z' makes some shas malformed
        return "".join(rng.choice(alphabet) for _ in range(rng.choice([7, 40, 41])))

    return {
        "head_sha": rng.choice([None, rand_sha(), rng.randint(0, 99), "", "HEAD"]),
        "commits": rng.choice([None, [], [rand_sha()], [f"{rand_sha()} subject"], [rng.random()], "abc"]),
        "changed_files": rng.choice([None, ["README.md"], ["no/such/file.ts"], [7], ["a (deleted)"], "x"]),
        "deleted_files": rng.choice([None, ["no/such/file.ts"], "x"]),
        "tests_run": rng.choice([None, {}, {"u": rand_sha()}, {"u": rng.randint(0, 50)},
                                 {"u": f"pytest {rand_sha()}"}, "nope", [1, 2]]),
    }


@pytest.mark.parametrize("seed", range(12))
def test_random_metadata_never_crashes_and_uses_known_codes(tmp_path, seed):
    import random

    rng = random.Random(seed)
    repo = tmp_path / f"repo{seed}"
    if seed % 2:
        _init_git_repo(repo)  # odd seeds: real repo, even seeds: no git dir
    known = {
        "non_git_workspace", "missing_head_sha", "unknown_commit", "malformed_sha",
        "unreachable_head", "missing_changed_file", "no_verdict", "malformed_tests_run",
        "malformed_commits",
    }
    for _ in range(8):
        md = _random_metadata(rng)
        for level in CONTRACT_LEVELS:
            problems = validate_completion(md, str(repo) if repo.exists() else None, level)
            for p in problems:
                assert isinstance(p, Problem)
                assert p.code in known, p
                assert p.severity in ("warning", "info")
                assert p.field and p.message
