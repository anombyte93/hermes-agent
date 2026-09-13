# REPORT-FIX-DESKTOP.md

Same-family DeepSeek-v4-Pro repair of the next-ten Desktop acceptance gaps,
after independent GLM-5.3 review. Worker: evo (physical host). Frozen
implementation 8557130ff811b4f87a79cb69b95435978fac3cb6 on base
19e70e911fb1067cb096df8a287f6d6035e30475. This is a bounded repair of the
explicitly unmet acceptance requirements (R1, R2, R4, R5, R7, R8, R9, R10);
R3 and R6 were already PASS and were not re-opened.

Scope honored: only `apps/desktop/src/plugins/kanban/**`, the narrow Electron
version bridge (`apps/desktop/electron/main.ts`) and its shared renderer typing
(`apps/desktop/src/global.d.ts`), plus this report. No Python, no browser
IIFE, no package manifests/locks, no shared app routes, no original report
rewrites. No push/merge/release, no live board/store writes, no
service/systemd/tmux/process-kill, no credentials/provider/operator-session
access, no global config, no installs, no child cards/subagents, no guard or
test weakening.

## Repairs by requirement

R1 (actual renderer identity). `hermes:version` in electron/main.ts now also
returns `rendererCommit` / `rendererStampSource` / `rendererBuiltAt` /
`rendererDirty`, all read from the packaged install-stamp.json loaded ONCE at
process startup (INSTALL_STAMP). The support panel's identity row is now
"Renderer build" and shows that stamp commit; it is DISTINCT from the backend
REST identity and the adapter identity, and never derives the renderer SHA from
`appVersion` (the backend tree version) or `bundleCommitsBehind`. A missing
stamp is UNKNOWN (never invented). `bundleOutOfSync`/`dirty` render as a hint,
not as the identity.

R2 (401/403/login-HTML/malformed classification). The support panel now
classifies a browser-readiness failure with distinct visible remedies through
the existing REST seam: a 401/403 transport rejection (the electron REST bridge
throws `Error("40X: {...}")`) renders "unauthorized"/"forbidden" with a
sign-in/board-access remedy; a login-HTML/redirect reason renders "login"; a
malformed/parse reason renders "malformed"; reachability failures render
"unreachable". Auth is never inferred from reachability or a 200, and no
success is invented.

R4 (actual check origin). `AcceptanceCheck.source` (parent | machine | unknown)
is now typed and rendered per check as "parent attestation", "machine
validation", or "unknown origin", using the adapter's actual
`checks[].source` shape. The compare envelope still reports
reverified|regressed|new|unproved with honest UNKNOWN on missing receipt.

R5 (fingerprint includes reason/remedy/run). The intervention fingerprint is
now `kind + reason/remedy + run_id` (previously kind+reason only). A worker
that re-blocks with an identical reason under a NEW run id, with no claim/spawn
frame reaching the renderer in between (socket gap), is a NEW intervention and
notifies. Recovery (unblock/claim/spawn) still clears the fingerprint; board
isolation and replay remain (cursor + fingerprint maps are both board+task
keyed). Completion notifications are still never deduped.

R7 (measured coverage, UNKNOWN vs zero). Timing is now read from the ACTUAL
adapter evidence (`evidence.timing.query_seconds` / `collection_seconds`),
never the invented `envelope.helper_roundtrip_ms` / `collection_ms`. A MISSING
`worker_observations` renders "checked unknown" (never coerced to zero); a
PRESENT array (even empty) is a measured count, so a valid measured zero
renders as zero. Missing `worker_observation_cap` / `worker_observations_capped`
are UNKNOWN (not zero).

R8 (distinguish the four outcomes). Attachment provenance now distinguishes:
failed lookup (FAIL transport, wrong board/card or no such attachment), absent
receipt (UNKNOWN transport, no guarded receipt exists), rejected acceptance
(PASS transport but acceptance_state FAIL, never "accepted run"), and accepted
run (PASS + acceptance_state PASS + accepted_run_id). The original
board/card/reason is preserved; there is no unproved run link.

R9 (stale selection guard + held-cap omissions). The readiness batch result is
now guarded by a generation token bumped on any selection or board change, so a
late batch response for a stale selection is discarded, never painted under the
current selection. The held-card list (cap 100) now exposes the omitted tail
("+N held cards omitted") and pages the rest via the snapshot's own cursor
("Load more held cards"); the selected batch remains capped at 10. Mixed
FAIL/UNKNOWN stays visible; zero release/dispatch side effect.

R10 (explicit changed-snapshot/restart affordance). `useBoardEvidence` now
surfaces `snapshotChanged` when the first page's observed_at advances while
later pages are loaded (a refresh/board-switch dropped them). The board renders
an explicit "Snapshot changed, paging restarted" note; the dropped pages are
never blended into the new snapshot. State ownership is retained (one
observedAt-stamped extra-page set), no mixed generations.

## Test commands and results (exact)

Toolchain node v22.22.0 from
/home/hayden/atlas/work/trajectory-20260912/toolchain/node-v22.22.0-linux-x64/bin.

Renderer JS tests (repository vitest, ui project):

    node ../../node_modules/vitest/vitest.mjs run --project ui src/plugins/kanban/

Result: 9 test files passed, 138 tests passed (125 pre-existing + 13 new
negative/positive controls for the repaired gaps).

Renderer typecheck:

    node ../../node_modules/typescript/bin/tsc -p tsconfig.json --noEmit

Result: exit 0, no diagnostics.

Electron bridge typecheck (covers the main.ts + global.d.ts bridge change):

    node ../../node_modules/typescript/bin/tsc -p tsconfig.electron.json --noEmit

Result: exit 0, no diagnostics.

Lint (kanban files, no --fix for verification, --fix applied for the two
cosmetic padding warnings the review already noted):

    node ../../node_modules/eslint/bin/eslint.js src/plugins/kanban/*.ts src/plugins/kanban/*.tsx

Result: 0 errors, 0 warnings after --fix (the pre-existing padding warnings at
completion-notify.test.ts 840/845 and two new test-file padding warnings were
fixed; no guard or test semantics changed).

## New controls (13 tests)

- R1: renderer build commit rendered as its own identity; backend appVersion is
  NOT painted as the renderer SHA; missing stamp → UNKNOWN while backend/adapter
  still show.
- R2: 401 → "unauthorized" remedy, 403 → "forbidden" remedy, login-HTML →
  "login" remedy; reachable-without-auth never infers a pass.
- R4: check `source` renders "parent attestation" / "machine validation" /
  "unknown origin".
- R5: a re-block under a NEW run id (missed claim frame) notifies; same run id
  + same reason stays quiet; recovery/board isolation preserved by the existing
  tests.
- R7: evidence.timing.query_seconds/collection_seconds rendered; present empty
  observations = valid measured zero; missing observations = UNKNOWN (no "0
  checked" invented).
- R8: absent receipt (UNKNOWN) / rejected acceptance (FAIL) / failed lookup
  (FAIL transport) are each labelled distinctly; PASS+FAIL never says
  "accepted run".
- R9: held 100-cap omission exposed with a cursor path to the rest; a late
  batch response for a stale selection is discarded.
- R10: an explicit "snapshot changed, paging restarted" note when a refresh
  drops loaded pages; the dropped page never blends into the new snapshot.

## Limitations / remaining visual gap

- No headless-browser screenshots at desktop/390px were produced within the
  window: the render proofs here are DOM-level (real components, real React,
  real React Query, real Blob bytes in jsdom), which does not paint pixels.
  The exact remaining visual item is pixel-level proof of the support panel,
  drawer provenance lines, and batch controls at desktop and 390px widths, which
  is unchanged from the prior review's open item. The styling itself is unchanged
  (Tailwind utility classes only; no CSS file edits).
- The backend REST doors (/evidence/releases, /evidence/browser-readiness,
  /evidence/acceptance-compare, /evidence/attachment-provenance,
  /workflow/readiness-batch) are consumed by exact contract but were not
  exercised live here; the browser/REST slice has a separate live repair owner.
- R2 classification is renderer-side: it classifies the transport rejection and
  the envelope reason/remedy the backend emits; it does not re-implement auth
  and never invents success.

## ACTUALLY_USED

- node v22.22.0 toolchain PATH pin (parent-provided path).
- Repository vitest 4.1.10 (--project ui), typescript tsc (renderer +
  electron projects), eslint (verification + --fix for cosmetic padding).
- Read-only git plumbing (diff/status) on the frozen checkout.
- The supplied adapter reference sources (adapter-remote-reference.py,
  adapter-server-reference.py) to pin the ACTUAL shapes: checks[].source,
  evidence.timing.query_seconds/collection_seconds, worker_observation_cap /
  worker_observations_capped, and the attachment-provenance
  FAIL/UNKNOWN/PASS+verdict states.

## DISCOVERED

- The /events socket frame already carries `run_id` (plugins/kanban/dashboard/
  plugin_api.py `_event_dict`), but the desktop CompletionEvent type omitted it;
  adding it is what makes the R5 run-aware fingerprint possible without any
  backend change.
- INSTALL_STAMP is read once at process startup (main.ts module scope), so the
  renderer build identity is naturally fixed across an in-place `hermes update`
  while resolveHermesVersion()/appVersion tracks the update root, exactly the
  R1 "update-root changes while loaded stamp stays fixed" invariant.

## UNTOUCHED

- All Python, gateway, browser IIFE, package manifests/locks, node_modules
  (read-only), every file outside the scope listed above.
- No services, processes, boards, credentials, config, push/merge, child cards.
- The original REPORT-NEXT-DESKTOP.md and REVIEW-DESKTOP.md were not rewritten.

## MISSING

- Headless screenshots at desktop/390px (see Limitations).
- No live adapter/REST exercise (backend doors are the parent's integration
  slice); all endpoint behaviour verified by contract-shaped fixtures.

## FRICTION

- The @hermes/plugin-sdk useQuery surface and the no-restricted-syntax lint rule
  (refs-synced-from-atoms) needed the same eslint-disable the existing
  CardWorkflowPanel already carries for its generation ref; applied identically
  for the R9 generation guard.
- None blocking: the prepared node_modules cache worked as-is this run (no
  dependency repair needed).

Finish: held for parent verification. No self-approval, no request-review.
