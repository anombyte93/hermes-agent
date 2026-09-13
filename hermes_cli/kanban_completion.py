"""Completion-contract validation (``kanban.completion_contract``).

A worker handoff is evidence to evaluate, not truth. This module checks the
``metadata`` a worker passes to ``kanban_complete`` against the workspace it
claims to have worked in, before the board records the task ``done``:

- ``head_sha`` must exist in the repo and be reachable from the workspace HEAD
  (not just any object in the database);
- ``commits`` (when given) must each exist — entries may carry a subject
  suffix (``"<sha> subject"``) as workers naturally write them;
- ``changed_files`` must each exist in the tree at ``head_sha``, or be declared
  deleted (``deleted_files`` key or an inline ``"(deleted)"`` marker);
- ``tests_run`` values that look like a command must carry a number (a count)
  or an explicit ``passed``/``failed`` verdict.

The validator is pure (no DB, no config load) so it can be tested directly and
run read-only over any board's stored runs. Callers pick a level:

- ``off``    — no validation at all;
- ``warn``   (default) — problems are recorded (``completion_warnings``
               comment + event) but the completion is accepted;
- ``strict`` — problems refuse the completion: the task stays ``running`` and
               the worker is told exactly what to fix.

Non-git workspaces (or no workspace at all) skip the git checks; an ``info``
problem says so, and info never blocks.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Optional, Union

__all__ = [
    "CONTRACT_LEVELS", "Problem", "resolve_completion_contract",
    "validate_completion", "warnings_from",
]

CONTRACT_LEVELS = ("off", "warn", "strict")
DEFAULT_CONTRACT = "warn"

# Commands we look for in tests_run values: a runner invocation whose output
# must be summarised with a count or verdict, not just named.
_COMMAND_RE = re.compile(
    r"(?:^|[/\s=])(?:pytest|py\.test|tox|nox|make|npx|pnpm|yarn|npm|bun|deno test|"
    r"go test|cargo test|mvn|gradle[^\s]*|sbt|bazel test|jest|vitest|playwright|"
    r"python[^-\s]*\s+-m\s+pytest)(?:\s|$)",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(r"\d")
_VERDICT_RE = re.compile(r"\b(?:pass(?:ed|es|ing)?|fail(?:ed|s|ing)?|ok|green|red|skip(?:ped)?)\b", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{6,64}$", re.IGNORECASE)
_DELETED_MARKER_RE = re.compile(r"\(\s*deleted\s*\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Problem:
    """One verifiable-contract violation. ``warning`` problems gate ``strict``
    mode; ``info`` problems never gate anything."""

    code: str
    severity: str  # "warning" | "info"
    field: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - presentation only
        return f"{self.field}: {self.message}"


def resolve_completion_contract(config: Optional[dict]) -> str:
    """``kanban:`` config section → contract level; invalid values fall back to
    the default (never silently escalate to strict)."""
    if not isinstance(config, dict):
        return DEFAULT_CONTRACT
    value = config.get("completion_contract")
    if isinstance(value, str) and value.strip().lower() in CONTRACT_LEVELS:
        return value.strip().lower()
    return DEFAULT_CONTRACT


def warnings_from(problems: list[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == "warning"]


# --- git helpers (single subprocess calls, bounded output) ---


def _git(repo: str, *args: str) -> Optional[str]:
    """Run one git query; None on any failure (missing git, not a repo, ...)."""
    try:
        out = subprocess.run(
            ("git", "-C", repo, *args),
            check=True, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    return out.stdout


def _is_git_workspace(workspace: Optional[str]) -> bool:
    if not workspace:
        return False
    return _git(workspace, "rev-parse", "--git-dir") is not None


def _norm_sha(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value.strip()):
        return None
    return value.strip().lower()


def _commit_exists(repo: str, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}") is not None


def _commit_reachable(repo: str, sha: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", sha, "HEAD") is not None


def _files_at_head(repo: str, head_sha: str) -> Optional[set[str]]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", "-z", head_sha)
    if listing is None:
        return None
    return {name for name in listing.split("\0") if name}


def _strip_deleted_marker(path: str) -> Optional[tuple[str, bool]]:
    """``"src/a.ts (deleted)"`` → ``("src/a.ts", True)``; ``"src/a.ts"`` →
    ``("src/a.ts", False)``; non-strings are rejected (None)."""
    if not isinstance(path, str) or not path.strip():
        return None
    if _DELETED_MARKER_RE.search(path):
        return _DELETED_MARKER_RE.sub("", path).strip(), True
    return path.strip(), False


def _sha_entry(entry: Any) -> Optional[str]:
    """First whitespace-separated token when ``entry`` is a non-empty string."""
    if isinstance(entry, str) and entry.strip():
        return entry.split()[0]
    return None


def _looks_like_command(value: str) -> bool:
    return bool(_COMMAND_RE.search(value))


def _has_count_or_verdict(value: str) -> bool:
    return bool(_COUNT_RE.search(value) or _VERDICT_RE.search(value))


def validate_completion(
    metadata: Optional[dict],
    workspace_path: Optional[str],
    contract: str,
) -> list[Problem]:
    """Validate one completion handoff. Pure: git reads only, no DB writes.

    Returns every Problem found (warnings + info); callers gate on
    :func:`warnings_from`. Never raises on malformed metadata — a bad shape is
    itself reportable, not a crash.
    """
    if contract == "off":
        return []
    problems: list[Problem] = []
    metadata = metadata if isinstance(metadata, dict) else {}

    # tests_run checks run regardless of workspace type.
    problems.extend(_check_tests_run(metadata.get("tests_run")))

    if not _is_git_workspace(workspace_path):
        problems.append(Problem(
            "non_git_workspace", "info", "workspace",
            "non-git workspace: git evidence checks skipped "
            f"(workspace={workspace_path!r})",
        ))
        return problems

    assert workspace_path is not None
    head = metadata.get("head_sha")
    if head in (None, ""):
        problems.append(Problem(
            "missing_head_sha", "warning", "head_sha",
            "no head_sha recorded for a git workspace — the board cannot verify "
            "which commit the handoff describes",
        ))
        head_sha = None
    else:
        head_sha = _norm_sha(head)
        if head_sha is None:
            problems.append(Problem(
                "malformed_sha", "warning", "head_sha",
                f"head_sha {head!r} is not a commit sha",
            ))
        elif not _commit_exists(workspace_path, head_sha):
            problems.append(Problem(
                "unknown_commit", "warning", "head_sha",
                f"head_sha {head_sha} does not exist in {workspace_path}",
            ))
        elif not _commit_reachable(workspace_path, head_sha):
            problems.append(Problem(
                "unreachable_head", "warning", "head_sha",
                f"head_sha {head_sha} exists but is not reachable from the "
                f"workspace HEAD",
            ))

    # commits: each entry must exist; "<sha> subject" entries are accepted.
    commits = metadata.get("commits")
    if commits not in (None, ""):
        if isinstance(commits, (list, tuple)):
            for entry in commits:
                sha = _norm_sha(entry.split()[0]) if _sha_entry(entry) else None
                if sha is None or not _commit_exists(workspace_path, sha):
                    problems.append(Problem(
                        "unknown_commit", "warning", "commits",
                        f"commit {entry!r} does not exist in {workspace_path}",
                    ))
        else:
            problems.append(Problem(
                "malformed_commits", "warning", "commits",
                f"commits must be a list of shas (got {type(commits).__name__})",
            ))

    # changed_files: must exist in the tree at head_sha unless declared deleted.
    if head_sha is not None:
        declared_deleted = {
            p.strip() for p in (metadata.get("deleted_files") or [])
            if isinstance(p, str) and p.strip()
        }
        files_at_head = _files_at_head(workspace_path, head_sha)
        if files_at_head is not None:
            for raw in metadata.get("changed_files") or []:
                if not isinstance(raw, str) or not raw.strip():
                    continue
                path, marked_deleted = _strip_deleted_marker(raw)
                if path in files_at_head:
                    continue
                if marked_deleted or path in declared_deleted:
                    continue
                problems.append(Problem(
                    "missing_changed_file", "warning", "changed_files",
                    f"{path} is not in the tree at head_sha {head_sha} and is "
                    f"not declared deleted (deleted_files key or "
                    f"\"(deleted)\" marker)",
                ))

    return problems


def _check_tests_run(tests_run: Any) -> list[Problem]:
    if tests_run in (None, {}, []):
        return []
    if not isinstance(tests_run, dict):
        return [Problem(
            "malformed_tests_run", "warning", "tests_run",
            f"tests_run must map names to counts/verdict strings "
            f"(got {type(tests_run).__name__})",
        )]
    problems: list[Problem] = []
    for name, value in tests_run.items():
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if _looks_like_command(text) and not _has_count_or_verdict(text):
            problems.append(Problem(
                "no_verdict", "warning", "tests_run",
                f"{name}: command {text!r} reports no count or passed/failed "
                f"verdict",
            ))
    return problems
