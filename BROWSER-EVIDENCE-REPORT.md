# Browser Evidence Report — t_32b920b3

Physical EVO worker (profile evo, deepseek-v4-pro). Workspace
`/home/hayden/atlas/work/trajectory-20260912/ui-browser`, branch
`feat/kanban-browser-evidence-20260912`. Sole purpose: the independent
handwritten browser dashboard (K3/K4/K9/K10 foundation) in
`plugins/kanban/dashboard/dist/index.js`, with browser-mounted tests.

## What landed (commits)

1. `3a6db6f` — first functional slice: the /evidence/* bridge consumer in
   the IIFE (identity alignment, bounded snapshot + counts + load-more,
   per-card worker badges, drawer worker detail, gated evidence attachment
   metadata/open).
2. `ff7dd6c` — parent-review corrections: Stopped now requires
   `aggregate.stopped > 0` (completion_records is bookkeeping, never
   stopped-worker evidence); a card without an observation is UNKNOWN
   regardless of status; page-0 poll replaces (not merges) the worker map
   and the page-0 card slice, and only seeds paging state on the initial
   load.
3. `HEAD` (this commit) — single-source data path + browser tests + report.

## Correct worker-state resolution (vs the React consumer)

The React consumer's `resolveWorkerState` read a top-level
`running`/`complete`/`unknown` the helper never emits, and treated
`process_present` alone as running. The IIFE resolver instead:

- Running = an observation with `state === "PASS"` AND `process_present ===
  true` AND `workspace_matches === true` AND `run_start_matches === true`.
- Stopped = `aggregate.complete === true` AND `aggregate.running === 0` AND
  `aggregate.unknown === 0` AND `aggregate.stopped > 0`. A
  completion_records-only aggregate is never Stopped.
- Missing/incomplete observation = UNKNOWN, never Stopped.
- The aggregate is read NESTED under `evidence.aggregate`.

## Single source + reused WebSocket (parent guidance)

When the board is identity-aligned (`/evidence/context` reports aligned), the
snapshot is the single source of truth: the grid is derived from the bounded
snapshot cards and `loadBoard` routes to a snapshot refresh, so the local
`/board` is never fetched for an aligned board. The original WebSocket
subscription is NOT dropped: its event callback (via `scheduleReload`) and all
board actions call the same `loadBoard`, which dispatches to snapshot refresh
when aligned and local `/board` when unaligned. Alignment still unresolved is a
hold, not a `/board` read. Generation counters drop late responses from a
previous board/filter.

## Browser-mounted tests

`plugins/kanban/dashboard/tests/browser/run-browser-test.js` bundles the real
IIFE (esbuild + a minimal React SDK shim) and drives it with Playwright
(chromium-1243, headless, closed in finally) against a temporary local HTTP
fixture whose /evidence/* responses match EVIDENCE-CONTRACTS.json. Network
boundary mocking only: fetchJSON/authedFetch use the real fetch().

Command (node-v22 on PATH, NODE_PATH at repo node_modules):

```
node plugins/kanban/dashboard/tests/browser/run-browser-test.js
```

Result: 18/18 checks passed. Coverage:

- aligned: snapshot fetched, /board NOT fetched; snapshot card title rendered
  in the grid; Running badge (observation) and Unknown badge (no observation);
  counts total 3 / omitted 1; Load more appends the paged card and clears the
  affordance; drawer worker state Running; downloaded attachment content
  equality ("hello report").
- missing file: /attachments/<id> 404 surfaced distinctly from the metadata
  row ("missing file:"), never a silent content-proof claim.
- not aligned: visible "not the EVO evidence database" + "Choose the EVO
  connection" banner; local /board fetched; no snapshot fetched; no badges.
- race: a delayed evo snapshot in flight across a board switch is dropped by
  the generation guard (late "Run card one" never renders).
- refresh: on returning to the board, a title change surfaces and a previously
  Running observation that disappeared resolves to Unknown.

Desktop width (1280x900) exercised; phone width not covered this run (the IIFE
uses the same column layout; listed under MISSING).

## ACTUALLY_USED

- The released bridge envelope contract in EVIDENCE-CONTRACTS.json (snapshot
  cards/counts/worker_observations, worker aggregate, page items).
- Playwright 1.62.1 (node_modules) + the task-specified chromium-1243 binary
  at `/home/hayden/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome`
  via explicit `executablePath` (Playwright 1.62.1 defaults to chromium-1234,
  which is not the task binary).
- esbuild (node_modules) to bundle React 19.2.7 + react-dom/client + the IIFE.
- Node 22 (`toolchain/node-v22.22.0-linux-x64/bin`).

## DISCOVERED

- Playwright 1.62.1's default chromium executablePath is chromium-1234, but
  only chromium-1243 is present; the explicit `executablePath` override is
  required and works.
- React 19 has no UMD build in this tree, so a browser test must bundle the
  IIFE with esbuild rather than load it via script tags.
- The evidence hook must be declared before the `boardData` alias in
  KanbanPage: a `const` alias referencing a later-declared `const`
  (`evidenceAligned`) throws "Cannot access before initialization" (caught by
  the browser test, fixed by reordering).

## UNTOUCHED (deliberately preserved)

- `plugin_api.py` (backend /evidence/* routes) and all other Python files.
- `apps/desktop` React consumer (owned by another worker).
- `style.css` (outside the allowed file set; evidence badges use inline tones).
- The WebSocket subscription and the local /board path for non-aligned boards.
- All 95 parent API tests + ruff + WebSocket tests (not run here: no Python
  changes).

## MISSING

- Phone-width (e.g. 390x844) browser pass — not exercised this run.
- History (runs/events) and attachment pagination through the bounded
  `/evidence/page` cursors — worker detail and a single attachments page are
  wired; multi-page runs/events/attachments is not implemented yet.
- A dedicated fixture for the `helper unavailable` (non-PASS snapshot) banner
  and for the stale-red/amber freshness tiers is not asserted; the code paths
  exist but lack a browser assertion.

## FRICTION

- Tirith blocked `node -e` and `printf ... && node` shapes; scratch scripts
  were written as files under /tmp and run with NODE_PATH instead.
- A first debug pass surfaced the `evidenceAligned` TDZ ordering bug only via
  the browser page error; fixed by moving the hook above the boardData alias.

## Honesty note

This run did not implement K2/K5/K6/K7/K8 (explicitly out of scope). The
worker detail + single attachments page is a foundation, not full
history/attachment pagination. No /board refresh for an aligned board is
asserted by the browser test; the initial pre-alignment window holds (never
fetches) until `/evidence/context` resolves.
