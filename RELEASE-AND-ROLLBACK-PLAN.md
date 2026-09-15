# RELEASE-AND-ROLLBACK PLAN — Kanban runtime candidate (t_296420ae)

Date: 2026-09-15. Author: delivery card t_296420ae run 103.

## What is being installed

The verified runtime-readiness candidate (`fix/kanban-runtime-readiness-20260914`,
code head 81f4d47b18a + receipt 6dfd41a659 + #113 fix 26dfc6928) as a NEW
release directory `~/.hermes/releases/hermes-agent-<candidate-sha>/`,
following the established release-dir convention (clone of the local
checkout, pinned at the exact commit), with the venv editable install
re-pointed at it and services restarted through their own supervisors.

## Why it is safe to stage BEFORE approval

Staging creates the release dir only. Nothing imports it until the venv
re-point + service restart, which are the gated steps. Staging is
byte-verified against the accepted candidate commit.

## The rollback problem (measured, parent ROLLBACK-PROOF.md)

The candidate's schema adds `block_reason` + guard triggers to every board it
touches. The installed 956 writer does NOT set block_reason. Rolling the CODE
back to 956 while boards carry the new schema makes every kanban_block fail
with IntegrityError('block reason is required'). A code rollback alone is
therefore NOT a data-preserving rollback.

## Data-preserving rollback (the artifact, pre-built)

`scripts/rollback_unblock_guard.py` (this repo, this commit) is a reviewed,
tested, data-preserving rollback artifact that:

1. Per board (or one named board with --board): snapshots the DB file
   (sidecar copy + schema dump) BEFORE touching it;
2. Drops ONLY the three guard triggers (trg_tasks_block_reason_insert/
   update/clear) — never a table, row, or column; block_reason data is
   preserved (956 simply ignores the column);
3. Records every action to a JSONL audit log;
4. Is idempotent and dry-runnable (--dry-run lists boards and triggers).

Rollback procedure (exact steps, in order):
  a. `systemctl --user stop hermes-kanban-daemon.service`
     `systemctl --user stop hermes-gateway-astra.service`
     (drain-first: both are restartable units)
  b. Re-point venv: `~/.hermes/hermes-agent/venv/bin/python -m pip install
     -e ~/.hermes/releases/hermes-agent-956967fbdcd4cd75c6174f30b1a599c08eadd882 --no-deps`
  c. `python3 scripts/rollback_unblock_guard.py --all-boards` (drops guards;
     data untouched)
  d. Restart both units; verify with a block/unblock cycle on a scratch task
     AND `PRAGMA integrity_check` on the trust board.

Rollback was tested: isolated-board API probe (parent) + this script's
dry-run/live-drop on a scratch copy below.

## Service blast radius (hermes update --plan, live)

- gateway [astra] pid 2923716 (systemd) — drain-first SIGUSR1, restart
- serve [default] pids 3244381, 3244815 (desktop) — app respawns its backend
- hermes-kanban-daemon.service pid 2928456 (systemd)
No other profile runs services. Active worker sessions keep running: their
PYTHON paths were resolved at spawn; they finish their turns on 956 bytes.
