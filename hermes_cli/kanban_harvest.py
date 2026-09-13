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

import json
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
# Any "N ... failed" AFTER the matched count: a passing-run summary never
# carries a later failure clause ("256 passed, 66 unrelated tests failed" is
# narration about a partial run, not a green summary).
_FAILURE_TAIL = re.compile(r"\b\d+\s+(?:\w+\s+){0,3}(?:failed|errors?)\b", re.I)

_UNIT_WORDS = re.compile(r"\b(unit|units)\b", re.I)
_E2E_WORDS = re.compile(r"\b(e2e|chromium|playwright|isolated|lane|acceptance)\b", re.I)
_NATIVE_WORDS = re.compile(r"\b(native(?:-weekly|_weekly)?|signing)\b", re.I)


def _classify_bucket(line: str, rule_bucket: str) -> str:
    """Refine a rule's bucket with the line's own vocabulary.

    Runner-identity buckets (``pytest``/``vitest``/``jest``) are kept unless
    the line's own words clearly say otherwise — the runner name is the more
    precise fact than a generic ``unit`` guess.
    """
    if rule_bucket in ("pytest", "vitest", "jest"):
        if _E2E_WORDS.search(line):
            return "e2e"
        if _NATIVE_WORDS.search(line):
            return "native"
        return rule_bucket
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
            matched_line = False
            for m in pattern.finditer(line):
                if m.start() > 0 and _NON_SUMMARY_LEFT.search(line[: m.start()][-14:]):
                    continue
                if _FILES_ROW_LEFT.search(line[max(0, m.start() - 14):m.start()]):
                    continue
                tail = line[m.end():]
                if _DISQUALIFY_RIGHT.match(tail) or _FAILURE_TAIL.search(tail):
                    continue
                # e2e fraction: passed numerator must equal denominator (a
                # "2/5 passed" partial run is not a green summary).
                groups = m.groups()
                if rule_bucket == "e2e" and groups and groups[0] != groups[1]:
                    continue
                # pytest rule group(1) is the full status list; total = every
                # status count summed (a "12 passed, 1 failed" line reports
                # 13 tests, not 12 green ones).
                if rule_bucket == "pytest":
                    count = _total_from_status_list(groups[0] or "")
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
                matched_line = True
            if matched_line:
                # First matching rule wins for the WHOLE line: a pytest
                # summary must not also feed the generic rule.
                break
    return {bucket: count for bucket, (count, _line_no) in sorted(buckets.items())}


def _total_from_status_list(status_list: str) -> "int | None":
    """Test total from pytest's status fragment: ``passed + failed + errors``
    (skips/deselected/xfailed are not run outcomes; a "12 passed, 1 failed,
    2 skipped" line reports 13 run tests)."""
    total = 0
    seen = False
    for m in re.finditer(r"(\d+)\s+(passed|failed|errors?)", status_list):
        total += int(m.group(1))
        seen = True
    return total if seen else None


# --- self-report comparison ---------------------------------------------------

def _digits_in(text: str) -> "int | None":
    """Best passed-count from free text: prefer the number beside the word
    ``passed``; else the LAST integer after dropping failure-count fragments
    (``executed 320, passed 320, failed 0`` -> 320; ``53 files, 483 tests``
    -> 483). Never the first, which is usually a file or attempt number."""
    text = str(text)
    # "N passed" with any short separator (space/comma/dash, incl. Unicode):
    # playwright-style reports pack "expected 320, executed 320, passed 320".
    beside = re.findall(r"(\d+)\W{0,3}passed", text, re.I)
    if beside:
        return int(beside[-1])
    cleaned = re.sub(r"\b(?:failed|errors?)\s*:?\s*\d+", " ", text, flags=re.I)
    cleaned = re.sub(r"\b\d+\s*(?:failed|errors?)\b", " ", cleaned, flags=re.I)
    numbers = re.findall(r"\d+", cleaned)
    return int(numbers[-1]) if numbers else None


def _self_report_counts(self_report: "dict | None") -> "dict[str, int] | None":
    """Normalize a worker's ``tests_run`` metadata into ``{bucket: passed}``.

    Accepts the shapes actually seen on boards: ``{"unit": 483}``,
    ``{"unit": "53 files, 483 tests passed"}``, nested per-bucket dicts
    (``{"e2e": {"passed": 320, "failed": 0}}``), or a bare ``483``.
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
            if isinstance(value, bool):
                continue
            n: "int | None"
            if isinstance(value, dict):
                # Nested runner report: prefer an explicit passed/total field.
                n = None
                for field in ("passed", "total", "numPassedTests"):
                    if isinstance(value.get(field), int):
                        n = int(value[field])
                        break
                if n is None:
                    n = _digits_in(json.dumps(value))
            elif isinstance(value, int):
                n = int(value)
            elif isinstance(value, str):
                n = _digits_in(value)
            else:
                n = None
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

    Verdicts per field: ``match`` / ``mismatch`` when both sides have the
    fact; ``worker_silent`` when the worker gave no self-report at all (the
    Lane A shape — died before reporting, harvest is the only evidence), or
    for ``head_sha`` when the report omits it (a required handoff fact whose
    absence is itself notable). ``tests_run`` is optional, so a report that
    never mentions tests simply omits the field.
    """
    if not self_report:
        return {"head_sha": "worker_silent", "tests_run": "worker_silent"}
    agreement: dict[str, str] = {}
    for field in ("head_sha", "tests_run"):
        harvested = harvest.get("head_sha") if field == "head_sha" else harvest.get("test_counts")
        reported = (self_report or {}).get(field)
        if field == "tests_run":
            reported = _self_report_counts(reported)
            harvested = harvested or None
            if reported in (None, {}, ""):
                continue  # optional field the worker never mentioned
        if reported in (None, {}, ""):
            agreement[field] = "worker_silent"
        elif harvested in (None, {}, ""):
            agreement[field] = (
                "worker_silent" if field == "tests_run"
                else "mismatch"  # git is ground truth; the claimed head is wrong
            )
        elif field == "head_sha":
            agreement[field] = "match" if str(reported).strip() == str(harvested).strip() else "mismatch"
        else:
            agreement[field] = "match" if _counts_agree(reported, harvested) else "mismatch"
    return agreement


def _counts_agree(reported: dict, harvested: dict) -> bool:
    """True when every reported bucket count is confirmed by the harvest.

    Subset semantics: the log's LAST-occurrence extraction may see MORE
    buckets than a worker listed (the worker reports what it ran; the log
    carries every runner it invoked), so only fields the worker actually
    claimed are checked. A claimed count that appears nowhere in the harvest
    is a mismatch.
    """
    harvested_counts = {int(v) for v in harvested.values()}
    return all(int(count) in harvested_counts for count in reported.values())


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

def workspace_head(workspace_path: "str | Path") -> "str | None":
    """Current HEAD sha of a git workspace (``None`` for non-git/missing)."""
    ws = Path(workspace_path).expanduser() if workspace_path else None
    if ws is None or not ws.is_dir():
        return None
    return _git_ok(ws, "rev-parse", "HEAD")


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
                # Aggregate PER PATH across the range: a file touched by 3
                # commits counts once in "files changed" (git's own
                # --shortstat semantics) even though numstat lists it 3 times.
                per_path: dict[str, tuple[int, int]] = {}
                for line in (numstat.stdout or "").splitlines():
                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue
                    add, delete, path = parts[0], parts[1], parts[-1]
                    ins = 0 if add == "-" else int(add)
                    dels = 0 if delete == "-" else int(delete)
                    prev_ins, prev_dels = per_path.get(path, (0, 0))
                    per_path[path] = (prev_ins + ins, prev_dels + dels)
                out["diffstat"] = {
                    "files": len(per_path),
                    "insertions": sum(v[0] for v in per_path.values()),
                    "deletions": sum(v[1] for v in per_path.values()),
                }
        else:
            # No base sha (first attempt on a repo the dispatcher never
            # stamped): list the most recent commits as a partial record —
            # honest facts, capped — but no diffstat (no honest range).
            log_proc = _git(ws, "log", "-n", str(max_commits), "--pretty=%H%x1f%s")
            if log_proc is not None and log_proc.returncode == 0:
                for line in (log_proc.stdout or "").splitlines():
                    if not line.strip():
                        continue
                    sha, _, subject = line.partition("\x1f")
                    out["commits"].append({"sha": sha.strip(), "subject": subject.strip()})
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
