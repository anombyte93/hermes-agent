#!/usr/bin/env python3
"""Evaluate the CI aggregate gate from the GitHub ``needs`` context.

Reads ``${{ toJSON(needs) }}`` on stdin and:
  - writes ``needs-json={job_name: result}`` to ``$GITHUB_OUTPUT`` (append),
    the compact dict the review-comment poller consumes;
  - prints one ``<icon> <job>: <result>`` line per job, sorted by name;
  - exits 1 when any job failed OR was cancelled; 0 otherwise
    (``success`` and ``skipped`` pass — a skipped job is an intentional
    path-filter skip, not a defect).

The prior gate only failed on ``failure``. A cancelled required job (for
example the ``ci-<ref>`` concurrency group cancelling a superseded run)
read as success, so a merge could be authorised on work that never ran.
"""

from __future__ import annotations

import json
import os
import sys

# A required job in either of these states must block the merge.
BLOCKING_RESULTS = ("failure", "cancelled")


def evaluate(needs: dict) -> tuple[dict[str, str], list[str]]:
    """Return (compact, blocking) from a ``needs`` dict.

    ``compact`` maps job name to its ``result``; ``blocking`` lists the job
    names whose result is failure or cancelled.
    """
    compact = {name: info["result"] for name, info in needs.items()}
    blocking = [
        name for name, info in needs.items()
        if info["result"] in BLOCKING_RESULTS
    ]
    return compact, blocking


def render(needs: dict) -> str:
    """Render the sorted per-job result lines."""
    lines = []
    for name in sorted(needs):
        result = needs[name]["result"]
        icon = "✅" if result in ("success", "skipped") else "❌"
        lines.append(f"{icon} {name}: {result}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    needs = json.load(sys.stdin)
    compact, blocking = evaluate(needs)

    print(f"needs-json={json.dumps(compact)}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"needs-json={json.dumps(compact)}\n")

    print(render(needs))
    if blocking:
        print(
            f"::error::{len(blocking)} job(s) failed or were cancelled: "
            f"{', '.join(blocking)}"
        )
        return 1
    print("All checks passed (or were skipped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
