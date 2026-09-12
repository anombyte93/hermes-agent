# Drawer Acceptance Report — React TaskDrawer history paging + download

Task: t_871a1265 (parent atlas-relay). Issue: https://github.com/anombyte93/hermes-agent/issues/54

## Repair round (parent acceptance, 2026-09-13)

Parent reproduced three failures on the first slice (commit `5cdcd2e`) and returned
them for same-profile repair. Root causes and fixes, all inside the allowed paths:

1. **Identity-lookup failure issued the legacy `fetchTask`.** The detail query was
   gated on `!contextQuery.isLoading`, so when `/evidence/context` *errored* (not
   loading), the drawer fell through to `aligned === false` and fired the legacy
   full-history `fetchTask` — the exact behaviour the aligned path is meant to
   suppress. Fix: the detail query is now gated on `contextQuery.isSuccess`, and the
   body render branches on `contextQuery.isError` / `isLoading` *before* any detail,
   so a failed or unresolved identity check visibly withholds detail (including any
   cached copy) and only a **positive** `aligned:false` may fetch legacy history.

2. **A pending resource rendered "No runs."** `usePagedResource` had no loading
   branch, so a still-in-flight first page fell through to the empty state and
   claimed an empty history from a pending response. Fix: both `EvidenceSection`
   and `EvidenceAttachmentsSection` now render an explicit "Loading …" state when
   `query.data` is absent, ahead of the empty state.

3. **A refresh retained the old row under the same id.** Accumulation was append-only
   with dedup-by-id, so a refetched page 1 that changed an existing row was skipped
   (the id was already present). Fix: page 1 (`cursor === null`) now **replaces** the
   accumulated rows; later pages still append (deduped by id). A genuine refresh
   (an `invalidateQueries` over the resource's page key) resets the cursor back to
   page 1 via a `QueryCache.subscribe`, so accumulated later pages are discarded and
   replaced rather than re-appended.

Two further controls were added (both proven RED against the pre-fix source, then
GREEN):

- **Cached-detail transition:** an unaligned drawer caches legacy detail (with a run
  row); when the identity check later fails, that cached legacy detail is withheld
  and "Could not verify this board" is shown instead.
- **Later-page refresh:** after loading page 2, a refresh resets the accumulated
  pages (page-2 row discarded) and replaces the changed page-1 row.

## What was implemented

Two new files plus a bounded edit to the existing drawer, all inside
`apps/desktop/src/plugins/kanban/`:

- `drawer-evidence.tsx` (new) — `DrawerEvidence`, `useResolvedBoardSlug`,
  `base64ToBlob`, `triggerDownload`, and the paged runs/events/attachments
  sections. Runs/events/attachments come from the read-only `/evidence/page`
  bridge (`fetchEvidencePage`, limit 50, stable cursor), one bounded page per
  resource with "Load more" that appends rows, a stated omission count, an
  exhaustion note ("All <resource> loaded."), and visible error/`FAIL` remedies
  (never an infinite spinner, never a green empty state from a failed response).
  Accumulation resets whenever the board or card identity changes, so a late
  response from a previous card can never append under a new card.
- `drawer-evidence.test.tsx` (new) — mounts the ACTUAL `TaskDrawer` with the
  real SDK and real React Query, mocking only the `./api` network boundary and
  faking only the OS download boundary (`URL.createObjectURL`/anchor click).
  `@/hermes` is mocked on the same seam `model-override.test.tsx` uses (the
  electron bridge; not a component/hook/query).
- `drawer.tsx` (edited) — the drawer now resolves the real board slug
  (`useResolvedBoardSlug`), checks `/evidence/context` alignment
  (`useEvidenceContext`), and when aligned reads the bounded detail via
  `fetchTaskWithoutHistory(slug, id)` (`include_history=false`, so legacy
  runs/events/attachments are never materialised) and renders `DrawerEvidence`.
  The detail query is gated on `contextQuery.isSuccess`, and the body withholds
  detail while the identity check is unresolved or failed. When positively
  unaligned, the legacy full-history detail (`fetchTask`) and the legacy
  events/runs/attachments sections are preserved unchanged.
  `WorkerEvidenceSection` stays visible.

Download stays on the existing authenticated REST door
`GET /attachments/{id}?format=json&board=slug` (`fetchAttachmentDownload`, already
exported by `api.ts`), which returns `{id, filename, content_type, size,
content_base64}` and never `stored_path`. The base64 payload is decoded to a
real Blob, handed to a named anchor download, and the object URL is revoked
after the click. Pending state and a visible failure/missing-404 remedy are
rendered per row; the existing upload mutation is reused (passed in as
`onUpload`/`uploadPending`).

## Verification (all real, executed on this box)

Pre-fix RED (proves the missing controls, not a helper green substitute):

    vitest run --project ui src/plugins/kanban/drawer-evidence.test.tsx
    # 5 failed on commit 5cdcd2e: identity-lookup error fetches legacy fetchTask,
    #   pending runs render "No runs.", first-page refresh retains old row, plus the
    #   two new controls (cached-detail transition, later-page refresh) failing too.

Post-fix GREEN:

    vitest run --project ui src/plugins/kanban/drawer-evidence.test.tsx   # 11 passed
    vitest run --project ui src/plugins/kanban/                            # 67 passed (4 files)
    eslint src/plugins/kanban/drawer.tsx src/plugins/kanban/drawer-evidence.tsx src/plugins/kanban/drawer-evidence.test.tsx   # exit 0
    tsc -p . --noEmit                                                     # exit 0

The 11 tests in `drawer-evidence.test.tsx` cover: legacy `fetchTask` disabled while
aligned (only `fetchTaskWithoutHistory` fires); real run/event/attachment rows
rendered after page 2 via load-more; Blob bytes equal the fixture binary and the
anchor `download` name matches; a visible 404 remedy on download failure; a visible
remedy when a page query fails (no hang); a late page from the previous card ignored
after a card switch; the three parent acceptance controls; and the two added repair
controls (cached-detail transition, later-page refresh). No pre-existing test was
modified; no config or gate changed.

## ACTUALLY_USED

- `fetchTaskWithoutHistory(slug, id)` — already exported by `api.ts`.
- `fetchEvidencePage(slug, resource, card, cursor, 50)` — already exported.
- `fetchAttachmentDownload(slug, id)` — already exported.
- `useEvidenceContext(slug)` — already exported by `evidence.tsx`.
- `useQueryClient().getQueryCache().subscribe` — the TanStack Query cache
  subscription (v5.101.2) used to reset paging on a genuine refresh.
- `model-override.test.tsx` setup seam (`vi.mock('@/hermes', ...)`) — reused.
- Node 22 (`.../toolchain/node-v22.22.0-linux-x64/bin/node`), vitest/vite/tsc/
  eslint from `.../node_modules/.bin/` (repo ROOT).

## DISCOVERED

- `/attachments/{id}?format=json` returns exactly
  `{id, filename, content_type, size, content_base64}` (confirmed in
  `plugins/kanban/dashboard/plugin_api.py`, `download_attachment`).
- `/evidence/page` item shapes (run/event/attachment) and the
  `include_history=false` detail contract (no runs/events/attachments/
  diagnostics) confirmed against the parent-owned browser fixture
  (`plugins/kanban/dashboard/tests/browser/run-browser-test.js`) and
  `plugin_api.py`.
- TanStack Query `QueryCache.subscribe` delivers `{ type: 'updated', query,
  action }` with `action.type === 'invalidate'` for each matching query on
  `invalidateQueries` — the explicit refresh signal used to reset paging.

## UNTOUCHED

`board.tsx`, `evidence.tsx`, `api.ts`, `types.ts`, the SDK, Electron, Python,
and the browser IIFE were not edited. Only `drawer.tsx`,
`drawer-evidence.tsx`, `drawer-evidence.test.tsx`, and this report changed.

## MISSING / FRICTION

- The capability map (relay-react-drawer-artifacts-20260912 and
  relay-drawer-truth-repair-20260913) was not consulted; the parent chose
  bare-hands and the work proceeded without it. Nothing else was missing or
  blocked.
- New affordance strings in `drawer-evidence.tsx` and the drawer's withheld-state
  copy are hardcoded English, consistent with the existing `evidence.tsx`;
  localizing them is a follow-up for integration, not a correctness gap.

## Handoff

This card implements and proves the React slice only. Integration, release, and
the actual installed-renderer proof are owned by the parent (atlas-relay).
