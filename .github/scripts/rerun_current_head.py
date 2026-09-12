#!/usr/bin/env python3
"""Rerun failed jobs on a completed CI run only when the PR head is unchanged.

The ``label-rerun`` workflow captures the PR head OID when ``ci-reviewed``
lands, then waits for that run to complete. A new commit can arrive during
the wait; rerunning the old SHA would grab the ``ci-<ref>`` concurrency
group and cancel CI for the newer commit. Before rerunning, this helper
re-reads the live PR head and refuses to rerun a stale one.

Exit codes:
  0  rerun issued
  1  API error (live head unreadable) or the rerun call itself failed
  2  superseded (the PR head changed while waiting; nothing rerun)
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def fetch_live_head(repo: str, pr: str) -> str | None:
    """Return the live PR head OID, or None when gh cannot read it."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr, "--repo", repo,
             "--json", "headRefOid", "--jq", ".headRefOid"],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError:
        return None
    return proc.stdout.strip() or None


def rerun_failed(repo: str, run_id: str) -> bool:
    """Rerun the failed jobs of ``run_id``; True when the call succeeds."""
    try:
        subprocess.run(
            ["gh", "run", "rerun", run_id, "--repo", repo, "--failed"],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError:
        return False
    return True


def rerun_if_head_unchanged(
    repo: str, pr: str, captured_head: str, run_id: str,
) -> tuple[str, str | None]:
    """Recheck the live head and rerun only when it still equals captured_head.

    Returns ``(outcome, live_head)`` where outcome is one of ``rerun``,
    ``superseded``, ``api-error`` or ``rerun-failed``.
    """
    live = fetch_live_head(repo, pr)
    if live is None:
        return "api-error", None
    if live != captured_head:
        return "superseded", live
    if not rerun_failed(repo, run_id):
        return "rerun-failed", live
    return "rerun", live


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True)
    parser.add_argument("--captured-head", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    outcome, live = rerun_if_head_unchanged(
        args.repo, args.pr, args.captured_head, args.run_id
    )
    if outcome == "rerun":
        print(f"PR head unchanged ({live}); reran failed jobs on run {args.run_id}.")
        return 0
    if outcome == "superseded":
        print(
            f"PR head changed while waiting: captured {args.captured_head}, "
            f"now {live}. Not rerunning stale run {args.run_id}."
        )
        return 2
    if outcome == "rerun-failed":
        print(
            f"Rerun call for run {args.run_id} failed; the stale run was not rerun.",
            file=sys.stderr,
        )
        return 1
    print(
        "Could not read the live PR head; refusing to rerun blind.", file=sys.stderr
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
