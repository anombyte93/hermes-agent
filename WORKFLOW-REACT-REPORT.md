# WORKFLOW-REACT-REPORT

Task t_8e77a64e: React Kanban readiness + verification workflow UI (K2/K5/K6/K7/K8).
Branch feat/kanban-react-workflow-20260913. Physical EVO, evo/deepseek-v4-pro.

## Result summary

Finished the assigned React surface and its tests. All local checks green:
Vitest 53/53, `tsc -p . --noEmit` clean, ESLint clean on the four touched files.
Two commits landed on the branch (not pushed): `7a53ee0` (workflow panels +
mounted tests) and `d358504` (non-authoritative baseline fix). The previous
run's two commits `6f1896b`/`b191f5c` remain intact underneath.

## Exact commands and outcomes

- `vitest run --project ui src/plugins/kanban/workflow.test.tsx src/plugins/kanban/completion-notify.test.ts`
  -> PASS. 2 files, 53 tests passed (workflow.test.tsx 15, completion-notify.test.ts 38).
- `tsc -p . --noEmit` (renderer project, apps/desktop) -> PASS, no diagnostics.
- `eslint src/plugins/kanban/{workflow.tsx,workflow.test.tsx,completion-notify.ts,completion-notify.test.ts}`
  -> PASS after fixes (0 errors, 0 warnings).

## The two parent-reported FAILs (fixed, not weakened)

Both were the case-sensitive matcher `/readiness fail/` against the component's
rendered text `Readiness fail: ...` (capital R). Changed to `/readiness fail/i`
at workflow.test.tsx:264 and :290. The assertions still require the FAIL
readiness surface to be present; only the case sensitivity was corrected.

## Load-bearing gaps closed (parent feedback on b191f5c)

- continueCard sends the EXACT accepted draft commission (workspace/profile/
  provider/model/creator/title/max_runtime_minutes) plus the accepted
  passed_checks/remaining_checks/verification_note. Implemented via
  `continueBodyFromDraft` reading `draft.evidence`; tested by asserting the
  full continueCard args object, not a helper predicate.
- Readiness checks render even when the outer envelope is FAIL/UNKNOWN and
  `evidence` carries checks (board_permission read-only, model UNKNOWN).
  Tested at "renders checks even when the envelope is FAIL/UNKNOWN".
- Card/board change resets every mutation/draft datum and drops late mutation
  successes via a `genRef` generation token (a request token, not an atom
  mirror; documented with an eslint-disable per the repo rule's stated
  exception).
- Any user edit to a draft input invalidates the accepted draft + continue
  result (`invalidateDraft`), so a stale fingerprint cannot be re-submitted.
- Attention queue accumulates pages in state and resets by slug on board
  switch; `pages` is consumed (was previously unused).
- Changes section retains its cursor across pages; no repeated null
  (baseline-now) read. Timeline is bounded with Load more + gap/incomplete
  notices.
- No generic `::` source-parser UX: remaining checks are three separate
  check/evidence/acceptance inputs; an incomplete row warns instead of being
  silently dropped.

## completion-notify baseline defect (fixed)

`ensureBaseline` previously did `typeof baselineId === 'number' ? baselineId : 0`,
so a FAIL/UNKNOWN/malformed HTTP 200 became baseline 0 and would notify
historical events. Now it only accepts `state === 'PASS'` AND the exact
`board === slug` AND a nonnegative finite integer `evidence.baseline_id`
(confirmed the schema location against WORKFLOW-API-CONTRACT.md, GET
/evidence/changes). On anything else the board stays unknown, so notifications
are suppressed and a later frame recovers with a fresh read; no full /board
poll is ever reintroduced.

Tests added: FAIL/UNKNOWN 200 suppression + recovery, malformed baseline_id
(missing/string/negative/float/NaN) + recovery, board-mismatched envelope
rejected, review_requested/changes_requested toasts carry board+card through
the real `$openCard` atom, and an explicit no-/board assertion. Existing
duplicate/replay no-second-notification and board-isolation tests remain and
now assert the strict envelope.

## What remains parent-owned (honest handoff)

- Final TaskDrawer insertion of `CardWorkflowPanel`: explicitly left to the
  parent once drawer worker t_871a1265 returns. `CardWorkflowPanel` is exported
  for that; I did not edit drawer.tsx.
- Live API integration and release: parent-owned; no live mutation was
  exercised here (fixture-only).
- `/evidence/changes` baseline is validated strictly and fails closed when the
  route is unavailable or unverified (EVO-only caveat the parent raised): local
  notifications are preserved through the same bounded changes route, not by a
  full /board poll and not by silent baseline-0. The React side is correct for
  whatever the API worker's route actually returns; the parent's live
  integration is what proves the route's availability per board.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

- ACTUALLY_USED: WORKFLOW-API-CONTRACT.md envelope + baseline_id location;
  ADAPTER-LIVE.json/ADAPTER-SERVER.py shape references via workflow-api.ts;
  the real `$openCard` atom from completion-notify; real SDK components
  (Button/Input/Textarea/Loader/Codicon) and React Query; the mounted
  KanbanBoardPage.
- DISCOVERED: the repo eslint bans atom->ref useEffect mirroring but documents
  request-token/generation-counter refs as a legitimate exception; used that.
  vitest bin lives at repo-root node_modules/.bin (symlinked) and runs from
  apps/desktop with `--project ui`.
- UNTOUCHED: drawer.tsx, evidence.tsx, types.ts, all Python/IIFE/SDK, board.tsx
  (the workflow panel wiring already landed in a prior commit; exact-card open
  through $openCard is already consumed by KanbanBoardPage). No installs or
  config changes; no push/merge; no child cards.
- MISSING: none blocking this surface. A live authenticated API is parent-owned.
- FRICTION: the case-sensitive readiness assertion from the prior run cost the
  parent two FAILs; corrected without weakening. The `makeRest` test fixture
  had to gain the state+board envelope now that baseline validation is strict.
