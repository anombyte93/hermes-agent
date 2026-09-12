# Drawer Acceptance Report — React TaskDrawer history paging + download

Task: t_871a1265 (parent atlas-relay). Issue: https://github.com/anombyte93/hermes-agent/issues/54

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
  The detail query is gated on `!contextQuery.isLoading` so the aligned branch
  never issues a legacy `fetchTask` first. When unaligned, the legacy
  full-history detail (`fetchTask`) and the legacy events/runs/attachments
  sections are preserved unchanged. `WorkerEvidenceSection` stays visible.

Download stays on the existing authenticated REST door
`GET /attachments/{id}?format=json&board=slug` (`fetchAttachmentDownload`, already
exported by `api.ts`), which returns `{id, filename, content_type, size,
content_base64}` and never `stored_path`. The base64 payload is decoded to a
real Blob, handed to a named anchor download, and the object URL is revoked
after the click. Pending state and a visible failure/missing-404 remedy are
rendered per row; the existing upload mutation is reused (passed in as
`onUpload`/`uploadPending`).

## Verification (all real, executed on this box)

Pre-fix RED (proves the missing control, not a helper green substitute):

    vitest run --project ui src/plugins/kanban/drawer-evidence.test.tsx
    # 6 failed: no aligned history, no download control, no paged rows

Post-fix:

    vitest run --project ui src/plugins/kanban/drawer-evidence.test.tsx   # 6 passed
    vitest run --project ui src/plugins/kanban/                            # 62 passed (4 files)
    eslint src/plugins/kanban/drawer.tsx src/plugins/kanban/drawer-evidence.tsx src/plugins/kanban/drawer-evidence.test.tsx   # exit 0
    tsc -p . --noEmit                                                     # exit 0

The 6 new tests cover: legacy `fetchTask` disabled while aligned (only
`fetchTaskWithoutHistory` fires); real run/event/attachment rows rendered after
page 2 via load-more; Blob bytes equal the fixture binary and the anchor
`download` name matches; a visible 404 remedy on download failure; a visible
remedy when a page query fails (no hang); and a late page from the previous card
ignored after a card switch. No pre-existing test was modified; no config or
gate changed.

## ACTUALLY_USED

- `fetchTaskWithoutHistory(slug, id)` — already exported by `api.ts`.
- `fetchEvidencePage(slug, resource, card, cursor, 50)` — already exported.
- `fetchAttachmentDownload(slug, id)` — already exported.
- `useEvidenceContext(slug)` — already exported by `evidence.tsx`.
- `model-override.test.tsx` setup seam (`vi.mock('@/hermes', ...)`) — reused.
- Node 22 (`.../toolchain/node-v22.22.0-linux-x64/bin/node`), vitest/vite/tsc/
  eslint from `.../ui/node_modules/.bin/`.

## DISCOVERED

- `/attachments/{id}?format=json` returns exactly
  `{id, filename, content_type, size, content_base64}` (confirmed in
  `plugins/kanban/dashboard/plugin_api.py`, `download_attachment`).
- `/evidence/page` item shapes (run/event/attachment) and the
  `include_history=false` detail contract (no runs/events/attachments/
  diagnostics) confirmed against the parent-owned browser fixture
  (`plugins/kanban/dashboard/tests/browser/run-browser-test.js`) and
  `plugin_api.py`.
- The drawer's `useValue($boardSlug)` slug was replaced by a resolved slug; this
  is a small intentional behavior change (board-scoped cache keys) and the only
  spot in `drawer.tsx` that was modified beyond wiring.

## UNTOUCHED

`board.tsx`, `evidence.tsx`, `api.ts`, `types.ts`, the SDK, Electron, Python,
and the browser IIFE were not edited. Only `drawer.tsx`,
`drawer-evidence.tsx`, `drawer-evidence.test.tsx`, and this report changed.

## MISSING / FRICTION

- The capability map (relay-react-drawer-artifacts-20260912) was not consulted;
  the parent chose bare-hands and the work proceeded without it. Nothing else
  was missing or blocked.
- The terminal guard misclassified a few `ls` probes as long-lived processes;
  worked around with `search_files`. No impact.
- New affordance strings in `drawer-evidence.tsx` (Load more, Download, Could
  not load, Unavailable, "All <resource> loaded.", "No <resource>.") are
  hardcoded English, consistent with the existing `evidence.tsx`; localizing
  them is a follow-up for integration, not a correctness gap.

## Handoff

This card implements and proves the React slice only. Integration, release, and
the actual installed-renderer proof are owned by the parent (atlas-relay).
