# REPORT-NEXT-DESKTOP.md

Next-ten Kanban extension, desktop React user paths (R1-R10). Worker: evo (physical host),
model deepseek-v4-pro. Base commit 19e70e911fb1067cb096df8a287f6d6035e30475.

Scope honored: only `apps/desktop/src/plugins/kanban/**` was edited, plus this report.
No Python, no browser IIFE, no package manifests/locks, no shared app routes. No dispatch,
no release, no live-board mutation, no credentials, no browser sessions, no service/systemd/
tmux/process-kill commands.

## Changes by requirement

R1 release identity. New `support.tsx` SupportPanel (board header "Support" toggle) reads
GET /evidence/releases (adapter + backend identity via `fetchEvidenceReleases`) and adds the
FRONTEND stamp locally from the loaded build metadata `window.hermesDesktop.getVersion()`.
appVersion, never a hardcoded SHA. Absent/invalid identity renders UNKNOWN.

R2 browser readiness. SupportPanel reads GET /evidence/browser-readiness and renders
reachable / authenticated / board_readable as THREE separate facts. Auth is never inferred
from reachability or a 200; a FAIL/UNKNOWN envelope surfaces its reason/remedy, and a 401/403/
login-HTML/malformed classification arrives in reason/remedy (backend-side) rather than being
guessed here.

R3 next-repair preview. `workflow.tsx` ReadinessChecks now renders `repair_preview` from the
readiness receipt as a TEXT preview ("preview - not applied"), derived only from failed/unknown
checks; never executable, never triggered.

R4 acceptance comparison. New `acceptance.tsx` AcceptanceCompareSection (drawer, aligned only):
two explicit run ids in, GET /evidence/acceptance-compare out, per-check change verdict
(reverified|regressed|new|unproved) with previous -> current, honest UNKNOWN on missing receipt.

R5 meaningful-intervention dedup. `completion-notify.ts`: blocked / block_loop_detected /
changes_requested are fingerprint-deduped per (board, task). An unchanged intervention is
quiet; a changed reason notifies; recovery (unblock/reclaim) or a new run (claim/spawn) clears
the fingerprint so a later identical block notifies. Completion notifications are NEVER
fingerprint-deduped. Board cursors remain isolated (key includes board+task).

R6 reviewer-packet export. ReviewerPacketSection downloads GET /evidence/reviewer-packet as a
REAL JSON Blob through the OS download boundary (triggerDownload), never clipboard text.

R7 refresh timing + checked/skipped. SupportPanel "Refresh & coverage" reads the SAME bounded
snapshot the board renders (shared query key): observed_at age, helper/collection ms from the
envelope timing, and checked vs skipped process checks (worker_observation_cap /
worker_observations_capped).

R8 attachment provenance. `drawer-evidence.tsx` AttachmentRow now reverse-links each attachment
to its card and its accepted run via GET /evidence/attachment-provenance, or shows explicit
"acceptance unknown" when there is no guarded receipt.

R9 readiness batch. `workflow.tsx` ReadinessBatchSection (board workflow panel): lists held
(blocked) cards from a bounded status=blocked snapshot read, selects up to 10, and POSTs
/workflow/readiness-batch. Preview only; never releases/dispatches. Per-card state + repair
preview and "no mutation performed" are shown.

R10 long-board paging under concurrent updates. `board-concurrency.test.tsx` exercises the
real board with the real evidence hook: overlapping page-2 ids deduplicate by id, a card whose
status changed between pages keeps one identity, a late page from a previous board is dropped
(via the observed_at stamp), exhaustion shows no Load more (restart affordance).

## Test commands and results (exact)

Toolchain node: /home/hayden/atlas/work/trajectory-20260912/toolchain/node-v22.22.0-linux-x64/bin/node (v22.22.0).

JS tests (repository vitest, ui project):

    node ../../node_modules/vitest/vitest.mjs run --project ui src/plugins/kanban/

Result: 9 test files passed, 125 tests passed.

Type check (renderer project):

    node ../../node_modules/typescript/bin/tsc -p tsconfig.json --noEmit

Result: exit 0, no diagnostics.

Lint (kanban files):

    node ../../node_modules/eslint/bin/eslint.js --fix src/plugins/kanban/*.{ts,tsx}

Result: exit 0 after fixing import order + one unused import.

## Red controls (negative cases exercised)

- R1: absent build metadata and absent adapter identity both render UNKNOWN (never invented).
- R2: reachable=true with authenticated=false renders "yes"/"no" separately; a UNKNOWN envelope
  surfaces its reason and never an inferred pass.
- R3: repair preview rendered as text with "preview - not applied"; nudgeDispatcher never fired.
- R4: compare only fires after an explicit click with two distinct numeric run ids.
- R5: unchanged blocked reason is quiet; changed reason / spawned / unblocked refire; completion
  always fires; fingerprints are board-isolated.
- R6: export downloads a real application/json Blob with the packet, not clipboard text.
- R8: accepted_run_id null renders "acceptance unknown", never acceptance.
- R9: FAIL batch envelope surfaces its reason (no all-clear); nudgeDispatcher never fired.
- R10: a late page from a previous board is dropped after a board switch.

## Limitations

- All new read doors are consumed through the existing plugin REST door (`ctx.rest`); the
  backend endpoints (/evidence/releases, /evidence/browser-readiness, /evidence/acceptance-compare,
  /evidence/reviewer-packet, /evidence/attachment-provenance, /workflow/readiness-batch) are
  consumed by exact contract and were not implemented here (backend is the parent's slice).
- The support panel labels and the new drawer sections are English-only (same convention as the
  existing workflow panel sections). Japanese localization was not added for these strings.
- R4 compare takes two explicit run ids rather than a picker over the drawer's paged runs; the
  run ids are already visible in the drawer's Runs section, so this is honest but manual.
- R5 intervention dedup is in-memory per renderer (same lifetime as the existing event cursor);
  it resets on a renderer reload, which re-baselines from the server high-water id.
- The frontend build stamp reads `window.hermesDesktop.getVersion()`; on a host without that
  bridge the stamp renders UNKNOWN (never hardcoded).

## FRICTION

- The prepared node_modules cache was missing @rolldown/plugin-babel@0.2.3 (declared in
  apps/desktop/package.json but absent from the read-only cache). The parent (atlas-relay)
  repaired this separately with a clean npm ci from the base package-lock. Before the repair,
  vitest could not load vite.config.ts. This worker did not edit node_modules or weaken any
  vite/test config, per instruction.
- The editor LSP reported stale "cannot find module" and "implicit any" diagnostics during the
  work (it was still resolving against the incomplete cache); the real tsc typecheck was clean
  (exit 0). This was treated as tool noise, not a real error.

## Tools used and misses

Used: node v22.22.0 (toolchain), vitest (repo), tsc (repo typescript), eslint (repo). All ran
against the parent-repaired read-only node_modules cache.

Misses: none that blocked delivery. The only environment gap was the @rolldown/plugin-babel
dependency, escalated to and resolved by the parent. No backend endpoints were exercised live
(this is the frontend slice; the parent integrates against the real adapter).
