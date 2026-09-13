"""``hermes goals`` subcommand parser + handler.

Manages persistent-goal recovery operations that don't need a live chat
session. First route: ``hermes goals rejudge <session_id>`` — re-run the
goal judge for one exact session on its original evidence (see
:func:`hermes_cli.goals.rejudge_goal`).
"""

from __future__ import annotations

import argparse
from typing import Callable, Optional


def build_goals_parser(subparsers, *, cmd_goals: Optional[Callable]) -> None:
    """Attach the ``goals`` subcommand to ``subparsers``."""
    goals_parser = subparsers.add_parser(
        "goals",
        help="Manage persistent goals across sessions (rejudge)",
        description=(
            "Operations on persistent /goal state that run outside a chat "
            "session. 'rejudge' re-runs the goal judge for one exact "
            "session on the evidence already stored in that session — "
            "the recovery path for a goal that paused because the judge "
            "API was unreachable (expired key, outage) while the work "
            "itself may already have been complete."
        ),
    )
    goals_subparsers = goals_parser.add_subparsers(dest="goals_command")

    rejudge = goals_subparsers.add_parser(
        "rejudge",
        help="Re-run the goal judge for one session on its stored evidence",
        description=(
            "Scoped goal re-judge: loads the goal bound to exactly this "
            "session, shows it and the last stored assistant response, "
            "then asks the goal judge whether the goal is satisfied. A "
            "genuine done verdict (and passing quality gates) transitions "
            "the goal to done; anything else reports an explicit "
            "incomplete/unreachable result. No agent turns are run and "
            "no budget is reset."
        ),
    )
    rejudge.add_argument(
        "session_id",
        metavar="SESSION_ID",
        help="Exact session id whose goal should be re-judged",
    )
    rejudge.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Judge the stored evidence and report, but run nothing and "
            "write nothing: no done transition, and quality gates are "
            "deferred entirely (not executed)"
        ),
    )
    rejudge.set_defaults(goals_command="rejudge")

    goals_parser.set_defaults(func=cmd_goals)


def cmd_goals(args) -> int:
    """Handle ``hermes goals`` dispatch. Returns a process exit code."""
    from hermes_cli.goals import rejudge_goal

    command = getattr(args, "goals_command", None) or "rejudge"
    if command != "rejudge":
        print(f"Unknown goals command: {command}")
        return 2

    sid = (args.session_id or "").strip()
    dry = bool(getattr(args, "dry_run", False))

    # Show the existing goal BEFORE acting, so the operator sees the
    # pre-transition state (paused/active/...) rather than the result.
    from hermes_cli.goals import load_goal

    state = load_goal(sid) if sid else None
    if state is not None:
        print(f"Goal rejudge — session {sid}")
        print(f"  Goal: {state.goal}")
        print(f"  Status: {state.status} ({state.turns_used}/{state.max_turns} turns)")

    result = rejudge_goal(
        args.session_id,
        dry_run=dry,
    )

    outcome = result["outcome"]

    evidence = result.get("evidence")
    if evidence:
        snippet = evidence if len(evidence) <= 400 else evidence[:400] + " …"
        print(f"  Evidence (last assistant response): {snippet}")

    if outcome == "done":
        if dry or result.get("dry_run"):
            print(
                f"  ✓ Dry-run: judge says done — {result['reason']}\n"
                "  (no state written; re-run without --dry-run to transition)"
            )
        else:
            print(f"  ✓ Goal achieved: {result['reason']}")
            print("  Goal transitioned to done.")
    elif outcome == "gates_pending":
        print(f"  ◑ Preview (outcome=gates_pending) — judge says done: {result['reason']}")
        print(
            "  Quality gates were NOT evaluated under --dry-run: no gate\n"
            "  command was run and nothing was written. Re-run without\n"
            "  --dry-run to execute the gates and transition."
        )
    elif outcome == "already_done":
        print(f"  ✓ {result['reason']}")
    elif outcome == "incomplete":
        print(f"  ✗ Incomplete — judge verdict: {result.get('verdict')}")
        print(f"    {result['reason']}")
        if result.get("changed") and result.get("status_after") == "paused":
            print(
                "    ⏸ Goal paused — the failing quality gate exhausted its\n"
                "    retries. Fix it or /goal gate remove it, then /goal resume."
            )
    elif outcome == "unreachable":
        print(f"  ✗ Judge unreachable — {result['reason']}")
        print("    Fix the goal_judge provider/key, then re-run this command.")
    elif outcome == "unparseable":
        print(f"  ✗ Judge reply unusable — {result['reason']}")
        print("    Route the judge to a stricter model (auxiliary.goal_judge.model).")
    elif outcome in (
        "no_session",
        "no_goal",
        "cleared",
        "no_evidence",
        "stale_evidence",
        "goal_changed",
    ):
        print(f"  ✗ {result['reason']}")

    return int(result.get("exit_code", 0))
