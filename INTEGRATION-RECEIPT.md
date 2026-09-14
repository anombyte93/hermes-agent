# Kanban Runtime Readiness — Integration Receipt

Candidate: branch `fix/kanban-runtime-readiness-20260914`; **accepted code head `81f4d47b18a436ddb3d3321fd406317860e91dd6`** (this receipt commit sits directly on top; the branch tip at read time is the receipt itself)
Base: upstream main **956967fbdcd4cd75c6174f30b1a599c08eadd882** (= installed release; fork `anombyte93/hermes-agent`, upstream `NousResearch/hermes-agent`)
Worktree: `/home/anombyte/atlas/work/kanban-cortex-120b-20260914/hermes-runtime-candidate`
Date: 2026-09-14 (AWST). Card: t_ddb56311.

## 0. Installed-base identity (proven, not assumed)

- Live entrypoint `~/.local/bin/hermes` execs the venv python against release dir
  `hermes-agent-956967fbdcd.../hermes` — verified by reading the wrapper script.
- Release dir `.git` HEAD = `956967fb...` = source checkout main = card's stated installed base.
- Release tree clean apart from `package-lock.json` (M) and `plugins/kanban/dashboard/dist/dist/` (untracked).
- **ZERO trust/{1..10} content is installed.** No trust branches exist in the release git; tree diff
  vs main shows only pycache noise. Every earlier "done" card claim of deployment was false.

## 1. Per-fix table (source head → scope → test evidence → installed status)

| # | Fix | Source (trust branch @ head) | Commits carried | Test evidence | Installed? |
|---|-----|------------------------------|-----------------|---------------|------------|
| 1 | block_reason durable state + read paths + CLI guard | trust/1 @ ffb1578abbd | 4 (red suite → fix → surface → notify fixture) | 15 red on base (behavioural: schema/assert, no ImportError); green after; 140 tests across 6 files | NO (this candidate only) |
| 2 | Real exit kinds: signaled:N / protocol_violation / output_limit_reached + all consumers | trust/2 @ 84d234292f1 | 4 (red → classify → red → consumers) | 30 tests (classification + consumers) + notifier/diagnostics/TUI suites 14 tests | NO |
| 3-guard | Kill/reclaim authorization: board scope + claimer liveness (NOT hostname alone); dispatch_once(on_locked="raise") | trust/3 @ 95252786519 (+ f861e3c1aee pythonpath, 78530a66bda docs) | 3 | 12 tests w/ real sleeper pids, two boards; **controlled before/after repro below**; 130 surrounding tests green | NO |
| 4 | Dispatcher resume note on every non-completed run | trust/4 @ 8dbf1932f07 | 2 (WIP snapshot carried the real code; tip reinstated setup-hermes.sh) | 11 tests | NO |
| 7 | Model-truth: record/surface the model+provider that actually answered each run (fallback attribution) | trust/7 @ f19fa2b466c | 1 | 27 tests + turn_usage/account_usage suites (7) | NO |
| 9 | Dispatcher harvests workspace evidence (commits/diffstat/tests) at every run end | trust/9 @ 9b367566be3 | 5 (+ restore commit for setup-hermes.sh) | 23 tests | NO |
| 6 | Completion contract: validator + enforce (warn/strict/off) + docs + read-only sweep | trust/6 @ 3ee6e8b4491 | 7 | 87 tests + cross-branch fixes below | NO |

**Deferred (not carried):**
- trust/3 load-shaping features (per-provider cap, 429 backoff, --per-tick, spawn stagger): dispatcher-stuck warning and starvation tuning are rollout concerns, not correctness for local-120B reliability. The conflict hunks referencing their params were resolved to keep only `on_locked`.
- trust/5 (`kanban_wave.py` integrate CLI): extracts command strings from model-written metadata and executes with `shell=True` (`_default_test_runner`). Untrusted-input execution; needs its own approval-boundary design (allowlist runner, no shell) before it can be safe. The safe skill body at `/home/anombyte/atlas/work/kanban-wave-canonical` is the separately-owned deliverable.
- trust/8 (update origin resolution): unrelated to kanban runtime readiness.
- trust/10 (per-board systemd units): rollout infrastructure; parent owns rollout.

**Rejected outright:** none beyond the deferrals; every carried fix proved out.

## 2. Mass-crash claim audit (the card's required controlled reproduction)

**Board evidence** (`~/.hermes/kanban/boards/hermes-kanban-trust-20260913/kanban.db`, read-only):
- `outcome` histogram: 67 crashed, 12 completed, 4 signaled:9, plus timeouts/reclaims.
- The 4 `signaled:9` rows + "probe"/"lane A replay" tasks carry `pid 222999, claimer Archie:mock` —
  **test/dogfood fixtures**, not real kills: the trust/2 dogfood scripts pinned tmp `HERMES_HOME`
  but inherited `HERMES_KANBAN_DB` from the dispatched-worker env, landing their scratch-board
  writes on the live board. (pytest itself is safe: conftest has scrubbed `HERMES_KANBAN_DB` since
  commit 861ce7c0b67, which IS in the installed base.)
- The REAL mass kills: runs 46-52 and 57-63 — seven genuine worker pids, claimer `Archie:3574176`
  (the daemon), all ending the same second, three separate times (23:33:58, 23:36:16, 23:49:01),
  matching trust/3's field evidence. A sibling process's tick reclaimed+killed every host-local row.

**Controlled before/after reproduction** (identical scenario, live `sleep 60` sleeper, live foreign
dispatcher pid, scratch board under tmp `HERMES_HOME`, no live state touched):
- State: task running, claim `Archie:<live-foreign-pid>@<board-db>` (scoped, live claimer),
  `worker_pid` = sleeper, claim expired 1h, heartbeat stale 2h.
- **main-956967fb:** sleeper state `Z`, returncode `-15` (SIGTERM'd), `released=1` — the foreign
  tick kills the live claimer's worker. This IS the mass-kill class.
- **candidate 81f4d47b18a:** sleeper state `S` (ALIVE), `released=0` — `claim_authorizes_host_action`
  returns `claimer_alive` → deferred to the owning dispatcher. No signal, no release.
- Guard liveness check: `pid_alive_fn` verified live pids correctly; the operator force-paths
  (`reclaim_task`/`archive_task` with `operator=True`) keep explicit-intent same-host signalling,
  and a dead claimer remains reclaimable (`claimer_dead` → authorized), so the board cannot wedge.

**Correction to earlier claims:** the repeated "crash" notifications were TWO overlapping phenomena —
fixture rows polluting the board (dogfood scripts) AND genuine sibling mass-kills (the defect the
guard fixes). Earlier reports conflated them. Neither pytest-inherited env nor "cross-board kills"
alone describes the whole.

## 3. Cross-branch interactions found while integrating (fixed in-candidate)

1. `d1c8d6ad252` — trust/9 harvest stamps `harvest`/`agreement` metadata on closed runs; trust/6-era
   exact-`run.metadata ==` asserts broke. Relaxed to key checks (2 sites).
2. `bca87734991` — same interaction in review-surfaces + review-lifecycle (3 more sites).
3. `d34d1027fca` — **latent trust/6 bug, red on trust/6's own branch**: the completion-contract
   stamp makes `metadata` truthy, suppressing the never-claimed review synthesis
   (`_REVIEW_APPROVED_NOTE` + manual-approval keys). Now merges both. Proven by running the
   upstream review-lifecycle suite against the trust/6 worktree (1 failed) then the candidate (green).
4. `1e5846ba634` — trust/1's trigger rejects raw `UPDATE ... SET status='blocked'` without a
   reason; reconcile-orphans fixture now uses `block_task` (same pattern trust/1 used elsewhere).
5. `81f4d47b18a` — dropped trust/2's scratch `run-trust2-tests.sh` (bypasses run_tests.sh).

## 4. Verification summary (scripts/run_tests.sh, real exit codes)

- Full kanban surface: **87 files, 659 tests, 0 failed, 1 skipped** (tests/hermes_cli/test_kanban*,
  tests/gateway/test_kanban*, tests/tui_gateway/test_kanban*, tests/cron/*kanban*,
  tests/tools/test_kanban_tools.py, session-reclaim-notify, turn-usage, account-usage).
- Red control: trust/1 suite 15/15 red on base (behavioural), green after. trust/6 latent bug red
  on its own branch (1 failed), green after fix.
- Compile: all 16 touched modules `py_compile` OK.
- Import provenance: worktree tests import the WORKTREE's `hermes_cli` (verified directly; the
  editable finder defers to sys.path).
- **Schema compatibility (live two-step probe):** board created under MAIN (no `block_reason`),
  reopened under CANDIDATE → column added, legacy blocked row backfilled
  `'Legacy block reason unknown'`, status preserved, `unblock_task` works. Old boards upgrade clean.
- setup-hermes.sh byte-identical to main (diff = 0) after two restore commits.

## 5. Rollout plan (parent owns execution)

1. **Acceptance:** parent reviews this receipt; next-stage proof child runs the candidate in
   isolation (fresh board, controlled HERMES_HOME) — normal / retry-resume / blocked scenarios:
   create → dispatch → tool work → complete (contract warn) / block with reason / crash+resume note.
2. **Install path (non-destructive):** standard release flow — new release dir from this commit,
  wrapper flip is one line, rollback = repoint wrapper to `hermes-agent-956967fb...` (existing dirs
  kept). No DB migration needed (additive column, auto-migrate on connect, proven above).
   Do NOT install from this card.
3. **Order:** candidate first, then (optionally, separately reviewed) trust/5 wave CLI with a
   non-shell allowlisted runner; trust/3 load-shaping and trust/10 units only if starvation shows.
4. **Rollback:** wrapper repoint; boards already touched by the new code remain valid under old code
   (extra column is ignored; scoped claim locks fall back to legacy hostname rule + liveness).
5. **Local-120B task path:** the deferred-failure loop that killed long local-model runs
   (output_limit_reached release-to-ready, resume notes, model-truth attribution) is exactly what
   this candidate fixes; the proof child should run a bounded analysis task on gpt-oss-120b under a
   fresh board with the candidate code, recording responding model + tools invoked + artifact.

## 6. Limits / honest gaps

- `detect_stale_running` and `enforce_max_runtime` guard paths are covered by the trust/3 suite and
  compile checks, not separately re-proven by a live before/after (the release_stale_claims repro
  covers the shared authorisation function they all call).
- The completion-contract validator trusts self-reported `tests_run` counts from worker logs unless
  harvest disagrees; harvest (trust/9) is the evidence floor, not a full audit.
- No live-120B inference was run from this card (parent/proof-child scope).
- Scratch probes are archived at `/tmp/kanban-candidate-scratch-archive/` (masskill repro,
  guard-why, term probe, schema-compat) — reproducible on demand.
