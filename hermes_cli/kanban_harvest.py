"""Workspace evidence harvest: derive what a worker's run actually produced.

The dispatcher calls :func:`harvest_workspace` when a run ends (completed,
crashed, timed out, output-limit, reclaimed) so the board records facts the
worker may have been unable to report — Lane A of the 2026-09-13 Align wave
died on output length with six commits on disk and zero metadata. Everything
here is PURE (no DB imports, no writes); git facts come from bounded
subprocess calls and test counts from regex extraction of the worker log's
LAST recognisable runner summaries. A non-git or missing workspace yields a
partial harvest; the functions never raise for a bad workspace.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "harvest_workspace",
    "parse_test_counts",
    "compare_harvest_to_self_report",
    "mismatch_fields",
    "format_mismatch_comment",
]


# --- git plumbing -----------------------------------------------------------

_GIT_TIMEOUT_SECONDS = 15


def _git(cwd: Path, *args: str) -> "subprocess.CompletedProcess[str] | None":
    """``git -C cwd args`` or ``None`` on any failure (missing git, timeout...)."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_ok(cwd: Path, *args: str) -> "str | None":
    proc = _git(cwd, *args)
    if proc is None or proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


# --- test-summary extraction ------------------------------------------------

# Ordered bucket rules: (bucket, one compiled pattern). The FIRST rule that
# matches anywhere on a line classifies that line (a pytest summary line must
# not also feed the generic rule). One occurrence yields one (bucket, count).
_TEST_SUMMARY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # pytest terminal summary: "12 passed, 1 failed in 3.45s" / "===== 5 passed ====="
    ("pytest", re.compile(
        r"((?:\d+\s+passed(?:\s*,\s*\d+\s+(?:failed|skipped|deselected|xfailed|xpassed|error(?:s)?|warning(?:s)?))*))\s+(?:in\s+)?[\d.]+m?s(?![\w])",
    )),
    # vitest summary row: "     Tests  64 passed (64)"
    ("vitest", re.compile(r"(?<![\w])Tests\s+(\d+)\s+passed(?:\s*\((\d+)\))?(?![\w])")),
    # jest summary row: "Tests:       8 passed, 8 total"
    ("jest", re.compile(r"(?<![\w])Tests:\s+(\d+)\s+passed(?![\w])")),
    # playwright list line: "  320 passed (5m)"
    ("playwright", re.compile(r"(?<![\w/])(\d+)\s+passed\s*\(\d+[smhd]w?\)(?![\w])")),
    # N/M fraction form: "320/320 passed", "e2e: 2/2 passed"
    ("e2e", re.compile(r"(?<![\w/])(\d+)/(\d+)\s+passed(?![\w])")),
    # Generic prose summary — the dominant real shape in worker logs:
    # "Unit: 483 passed across 53 files", "485 passed (52 files)",
    # "489 tests passed.".
    ("unit", re.compile(r"(?<![\w/])(\d+)(?:\s+tests?)?\s+passed(?![\w])")),
)

# Narration/label words that disqualify a "N passed" occurrence from being a
# runner summary (matched on the preceding ~14 chars).
_NON_SUMMARY_LEFT = re.compile(r"(?:failed|flak(?:y|ing)|baseline|same\s+as)\s*[/ ]?\s*$", re.I)
# vitest's file-count row ("Test Files  4 passed (4)") counts FILES, not tests.
_FILES_ROW_LEFT = re.compile(r"Files\s+$")
# Trailing statuses that disqualify (pytest's own mixed line is handled by its
# structured rule; this guards narration).
_DISQUALIFY_RIGHT = re.compile(r"^\s*,?\s*\d+\s+(?:failed|error)", re.I)

_UNIT_WORDS = re.compile(r"\b(unit|units)\b", re.I)
_E2E_WORDS = re.compile(r"\b(e2e|e2e:|chromium|playwright|isolated|native-weekly|native_weekly|lane|acceptance)\b", re.I)
_NATIVE_WORDS = re.compile(r"\b(native(?:-weekly|_weekly)?|signing)\b", re.I)


def _classify_bucket(line: str, rule_bucket: str) -> str:
    """Refine a rule's bucket with the line's own vocabulary."""
    if rule_bucket in ("pytest", "vitest", "jest"):
        if _UNIT_WORDS.search(line):
            return "unit"
        if _E2E_WORDS.search(line):
            return "e2e"
        if _NATIVE_WORDS.search(line):
            return "native"
        return "unit" if rule_bucket in ("vitest", "jest") else rule_bucket
    if rule_bucket == "playwright":
        if _NATIVE_WORDS.search(line):
            return "native"
        if _UNIT_WORDS.search(line) and not _E2E_WORDS.search(line):
            return "unit"
        return "e2e"
    # e2e fraction
    if _UNIT_WORDS.search(line) and not _E2E_WORDS.search(line):
        return "unit"
    if _NATIVE_WORDS.search(line) and not _E2E_WORDS.search(line):
        return "native"
    return "e2e"


def parse_test_counts(text: str) -> dict[str, int]:
    """Extract ``{bucket: passed_count}`` from worker-log text.

    Scans line by line (TUI renders wrap long lines, so a runner summary can
    be split); the LAST occurrence per bucket wins — an earlier partial count
    superseded by a final full-suite run must not be reported. Narration
    (``47 failed / 57 passed``) and mixed failed lines are not summaries.
    """
    if not text:
        return {}
    buckets: dict[str, tuple[int, int]] = {}  # bucket -> (count, line_no)
    for line_no, line in enumerate(text.splitlines()):
        if "passed" not in line:
            continue
        for rule_bucket, pattern in _TEST_SUMMARY_RULES:
            for m in pattern.finditer(line):
                if m.start() > 0 and _NON_SUMMARY_LEFT.search(line[: m.start()][-14:]):
                    continue
                tail = line[m.end():]
                if _DISQUALIFY_RIGHT.match(tail):
                    continue
                # e2e fraction: passed numerator must equal denominator (a
                # "2/5 passed" partial run is not a green summary).
                groups = m.groups()
                if rule_bucket == "e2e" and groups and groups[0] != groups[1]:
                    continue
                # pytest rule group(1) is the full status list; count "passed".
                if rule_bucket == "pytest":
                    count = _passed_from_status_list(groups[0] or "")
                    if count is None:
                        continue
                else:
                    count = int(groups[0])
                bucket = _classify_bucket(line, rule_bucket)
                # Same bucket twice on one line (e.g. "Test Files" + "Tests"
                # rows collapse when wrapped): keep the larger count.
                prev = buckets.get(bucket)
                if prev is None or count > prev[0]:
                    buckets[bucket] = (count, line_no)
    return {bucket: count for bucket, (count, _line_no) in sorted(buckets.items())}


def _passed_from_status_list(status_list: str) -> "int | None":
    """``passed`` count from pytest's ``N passed, M failed, ...`` fragment."""
    m = re.search(r"(\d+)\s+passed", status_list)
    return int(m.group(1)) if m else None


# --- self-report comparison ---------------------------------------------------

def _digits_in(text: str) -> "int | None":
    m = re.search(r"\d+", str(text))
    return int(m.group(0)) if m else None


def _self_report_counts(self_report: "dict | None") -> "dict[str, int] | None":
    """Normalize a worker's ``tests_run`` metadata into ``{bucket: passed}``.

    Accepts the shapes actually seen on boards: ``{"unit": 483}``,
    ``{"unit": "53 files, 483 tests passed"}``, or a bare ``483``.
    """
    if not self_report:
        return None
    counts: dict[str, int] = {}
    if isinstance(self_report, dict):
        for key, value in self_report.items():
            if not isinstance(key, str):
                continue
            if key in ("typecheck", "build", "typecheck_status", "build_status"):
                continue  # status strings, not counts
            n = _digits_in(value) if not isinstance(value, bool) else None
            if n is not None:
                counts[key.lower()] = n
    else:
        n = _digits_in(self_report)
        if n is not None:
            counts["tests"] = n
    return counts or None


def compare_harvest_to_self_report(
    harvest: dict, self_report: "dict | None",
) -> dict:
    """``{field: match|mismatch|worker_silent}`` for the comparable fields.

    ``head_sha`` compares the worker's recorded head against the harvested
    one (equality, not reachability: a worker listing the same sha it stands
    on). ``tests_run`` compares per-bucket passed counts extracted from the
    worker's own ``tests_run`` metadata against the harvested log counts.
    """
    agreement: dict[str, str] = {}
    for field in ("head_sha", "tests_run"):
        harvested = harvest.get("head_sha") if field == "head_sha" else harvest.get("test_counts")
        reported = (self_report or {}).get(field)
        if field == "tests_run":
            reported = _self_report_counts(reported)
            harvested = harvested or None
        if reported in (None, {}, ""):
            agreement[field] = "worker_silent"
        elif harvested in (None, {}, ""):
            agreement[field] = "worker_silent"
        elif field == "head_sha":
            agreement[field] = "match" if str(reported).strip() == str(harvested).strip() else "mismatch"
        else:
            agreement[field] = "match" if _counts_agree(reported, harvested) else "mismatch"
    return agreement


def _counts_agree(reported: dict, harvested: dict) -> bool:
    """True when every reported bucket count appears in the harvest counts."""
    harvested_counts = set(harvested.values())
    return all(count in harvested_counts for count in reported.values())


def mismatch_fields(agreement: dict) -> list[str]:
    """Field names whose agreement verdict is ``mismatch``."""
    return [field for field, verdict in agreement.items() if verdict == "mismatch"]


def format_mismatch_comment(task_id: str, run_id: "int | None", harvest: dict, agreement: dict) -> str:
    """Dispatcher comment body for a mismatch between worker claim and facts."""
    diff = harvest.get("diffstat") or {}
    counts = harvest.get("test_counts") or {}
    counts_txt = ", ".join(f"{b} {n}" for b, n in sorted(counts.items())) or "-"
    lines = [
        f"evidence mismatch on run {'#' + str(run_id) if run_id else '(unknown)'}:",
        f"  head: {harvest.get('head_sha') or '-'} ({(harvest.get('commits') or []).__len__()} commits)",
        f"  diff: {diff.get('files', 0)} files, +{diff.get('insertions', 0)}/-{diff.get('deletions', 0)}, dirty {harvest.get('dirty', 0)}",
        f"  tests: {counts_txt}",
    ]
    for field in mismatch_fields(agreement):
        lines.append(f"  mismatch: {field}")
    return "\n".join(lines)


# --- the harvest itself -------------------------------------------------------

def harvest_workspace(
    workspace_path: "str | Path",
    base_sha: "str | None",
    *,
    log_path: "str | Path | None" = None,
    max_commits: int = 50,
) -> dict[str, Any]:
    """Derive the evidence a run leaves behind. Pure; never raises for a bad
    workspace — a missing or non-git workspace yields a partial harvest.

    ``commits`` are listed from ``base_sha..HEAD`` (newest first, capped at
    ``max_commits``); ``diffstat`` is the aggregate numstat over the same
    range; ``dirty`` counts modified + untracked paths; ``test_counts`` comes
    from :func:`parse_test_counts` over the worker log's text when given.
    """
    ws = Path(workspace_path).expanduser() if workspace_path else None
    out: dict[str, Any] = {
        "commits": [],
        "diffstat": {"files": 0, "insertions": 0, "deletions": 0},
        "dirty": 0,
        "head_sha": None,
        "branch": None,
        "test_counts": {},
    }
    if ws is None or not ws.is_dir():
        return out
    if _git_ok(ws, "rev-parse", "--git-dir") is not None:
        head = _git_ok(ws, "rev-parse", "HEAD")
        out["head_sha"] = head
        branch = _git_ok(ws, "rev-parse", "--abbrev-ref", "HEAD")
        out["branch"] = branch
        base = (base_sha or "").strip() or None
        if base:
            range_arg = f"{base}..HEAD"
            commits: list[dict[str, str]] = []
            log_proc = _git(ws, "log", range_arg, "--pretty=%H%x1f%s", "--", ".")
            if log_proc is not None and log_proc.returncode == 0:
                for line in (log_proc.stdout or "").splitlines():
                    if not line.strip() or len(commits) >= max_commits:
                        break
                    sha, _, subject = line.partition("\x1f")
                    commits.append({"sha": sha.strip(), "subject": subject.strip()})
            out["commits"] = commits
            numstat = _git(ws, "diff", "--numstat", range_arg)
            if numstat is not None and numstat.returncode == 0:
                files = insertions = deletions = 0
                for line in (numstat.stdout or "").splitlines():
                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue
                    add, delete = parts[0], parts[1]
                    files += 1
                    insertions += 0 if add == "-" else int(add)
                    deletions += 0 if delete == "-" else int(delete)
                out["diffstat"] = {"files": files, "insertions": insertions, "deletions": deletions}
        status = _git(ws, "status", "--porcelain")
        if status is not None and status.returncode == 0:
            out["dirty"] = sum(1 for line in (status.stdout or "").splitlines() if line.strip())
    else:
        out["git"] = "absent"
    if log_path is not None:
        log = Path(log_path)
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        out["test_counts"] = parse_test_counts(text)
    return out
