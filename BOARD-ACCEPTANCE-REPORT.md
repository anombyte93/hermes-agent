# BOARD-ACCEPTANCE-REPORT.md

Board journey for t_ce22a1d1: connect the React grid to the bounded snapshot
cards, with a real Load more, and mounted network-boundary proof.

## What landed

- `apps/desktop/src/plugins/kanban/evidence.tsx`
  - `useBoardEvidence()` rewritten as a phase-typed result:
    `resolving` | `unaligned` | `context-error` | `aligned`.
  - The aligned grid, counts, and every card's worker badge come from the
    bounded snapshot only. No local /board read while alignment is unresolved
    or aligned.
  - `viewsToBoardColumns` fixed: it now folds `KanbanTask[]` directly (the WIP
    alias `KanbanCardView` plus its `view.task` indirection were removed).
  - Real Load more: `loadMore()` fetches the next page under the snapshot's
    stable `next_cursor` and appends fresh cards (duplicate ids dropped). Pages
    are stamped with the first page's `observed_at`; a refresh or board switch
    advances the stamp and the stale pages are ignored, so a late response can
    never relabel the current view.
  - Worker badges: a positive observation (state=PASS + process_present +
    workspace_matches + run_start_matches) maps to Running; anything else,
    including a MISSING observation, maps to Unknown (never the card's local
    status).
- `apps/desktop/src/plugins/kanban/board.tsx`
  - The aligned grid renders from `snapshotBoard(evidence)`; the local /board
    query is `enabled` only when the board is proven unaligned (or the archived
    view is on). The archived toggle keeps the native local view and never
    claims evidence for it.
  - Visible states: `context-error` and helper FAIL both render an error (never
    a silent local fallback); `unaligned` renders the local grid plus a
    "Choose the EVO connection" banner.
  - Header badge and a footer Load more bar (with the omitted count) render
    from the aligned evidence.
- `apps/desktop/src/plugins/kanban/api.ts`
  - Evidence invalidation rides the existing events-socket callback
    (`['kanban','evidence']`), no second poller or notifier.
- `apps/desktop/src/plugins/kanban/evidence.test.tsx`
  - Stopped is now proven to require `aggregate.stopped > 0`; a
    completion_records-only aggregate is Unknown (bookkeeping), not Stopped.
- `apps/desktop/src/plugins/kanban/board.test.tsx` (new)
  - Mounted `KanbanBoardPage` with the real SDK, real components, a real React
    Query client, and ONLY the network transport (`./api`) mocked.

## Verification (real execution)

- `vitest run --project ui src/plugins/kanban`: 4 files, 65 tests, all pass.
  - 7 mounted `KanbanBoardPage` tests prove: snapshot card titles with no /board
    poll, Running vs Unknown badges (missing observation is Unknown), Load more
    appends and the button disappears at exhaustion, the unaligned local grid
    plus choose-EVO remedy, visible context-error with no /board, visible
    helper-unavailable with no /board, and a board-switch that drops the late
    response from the previous board.
- `tsc -p . --noEmit`: exit 0.
- `eslint` on the touched source: clean after `--fix`.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

- ACTUALLY_USED: `snapshot.cards`, `snapshot.counts` (by_status/total/omitted),
  `snapshot.next_cursor`/`has_more`, `snapshot.worker_observations`,
  `/boards.current` (via `useResolvedBoardSlug`), the existing websocket
  invalidation callback, the real TanStack query layer and nanostores atoms.
- DISCOVERED: the released snapshot has no archived cards (`status_filter:
  'all'`), so the archived toggle must stay on the native local board; worker
  `observations` cover running records only, so a missing observation is
  Unknown, not Stopped.
- UNTOUCHED (deliberately out of scope, parent-owned): drawer.tsx, all of
  components/plugin_api/electron/SDK, the binary IPC transport, attachment
  download UX, K6-K8.
- MISSING: no archived evidence is claimed anywhere (correct per the snapshot
  contract); the aligned grid has no per-column assignee/tenant filter roster
  because the snapshot carries cards only, not board metadata.
- FRICTION: none blocking. `npx vitest` was refused by the package scanner in
  this single-query session, so tests were run via the repo-local
  `node_modules/.bin/vitest` directly.

## Limits

- The drawer journey, attachment transport, and binary IPC are separate
  parent-owned follow-ons; this slice keeps the board independent of the IPC
  gap.
- Optimistic drag/delete on the aligned grid still writes through the local
  REST door (correct when aligned, since it is the same DB) and refreshes via
  the existing socket invalidation; no optimistic paint on the snapshot grid
  itself.
