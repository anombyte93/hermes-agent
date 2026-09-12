# UI Evidence Report — t_a97c4b6b

Physical EVO worker (profile evo, deepseek-v4-pro). Workspace
`/home/hayden/atlas/work/trajectory-20260912/ui`, branch
`feat/kanban-evidence-ui-20260912`. Two functional slices committed; the run
reached its 25-minute bound before the IIFE consumer and board-level paging
were wired, so the remainder is listed under MISSING with exact next edits.

## What landed

### Slice 1 — backend identity alignment (commit `355a86c`)

- New read-only `GET /evidence/context?board=` on the kanban plugin router
  (`plugins/kanban/dashboard/plugin_api.py`). Returns `aligned` + a short
  reason, never a raw path.
- Alignment requires ALL of: `socket.gethostname() == "evo"`,
  `kanban_db.kanban_db_path(board).resolve()` equals the OS-account
  `~/.hermes/kanban.db` (default) or `~/.hermes/kanban/boards/<board>/kanban.db`
  (named board), both existing regular files, and no symlink aliasing.
  `HERMES_KANBAN_DB`/`HERMES_HOME` overrides therefore defeat alignment even
  on the evo host. Blank board resolves via `get_current_board()` without
  switching the global pointer. No helper invocation, no local DB open.
- 8 tests added to `tests/plugins/test_kanban_evidence_api.py` (aligned,
  blank-board, wrong-host, db-override divergence, missing db, symlink,
  named-board path).

### Slice 2 — React evidence layer (committed with the report)

- `types.ts`: `EvidenceContext`, `WorkerObservation`, `EvidenceCounts`,
  `EvidenceSnapshotData`, `EvidenceEnvelope<T>`, `WorkerEvidenceData`,
  `EvidenceCardData`.
- `api.ts`: `fetchEvidenceContext`, `fetchEvidenceSnapshot`,
  `fetchEvidenceWorker`, `fetchEvidenceCard`, `fetchEvidencePage` + the five
  board-scoped evidence query keys. All go through `withBoard` so the evidence
  read is pinned to the selected board, never the server's current pointer.
- `evidence.tsx` (new): `resolveWorkerState` (pure), `useEvidenceContext`,
  `WorkerEvidenceSection`, `EvidenceStateBadge`. Running is positive
  process-present evidence only; Stopped is the explicit full aggregate with
  `complete:true` and no unknown/running; missing observation is UNKNOWN, never
  Stopped. Non-PASS envelopes render Unavailable (amber-neutral, not red).
  When `/evidence/context` is not aligned, the section renders nothing and the
  worker query never fires.
- `drawer.tsx`: `WorkerEvidenceSection` rendered after the parent result /
  latest-summary sections, visually distinct from the parent result text.
- `evidence.test.tsx` (new): 5 pure resolver tests + 4 rendered-component
  tests (Running / Unknown / Unavailable / not-aligned), HTTP boundary mocked.

## Verification

- `apps/desktop` `npm run test:ui -- src/plugins/kanban` → 46 passed
  (37 baseline + 9 new), 3 files.
- `.venv/bin/python -m ruff check` on both touched Python files → clean.
- `pytest tests/plugins/test_kanban_evidence_api.py` → 60 passed, 1 failed.
  The failure is the PRE-EXISTING `test_missing_helper_is_unknown`, which now
  fails because the released `atlas-kanban-call` is installed at
  `~/.local/bin` (verified failing on the base commit `1a0195b` before this
  work). Unrelated to these changes.

## ACTUALLY_USED

- `hermes_cli.kanban_db` (`kanban_db_path`, `get_current_board`,
  `DEFAULT_BOARD`, `_normalize_board_slug`) for the context endpoint.
- The existing `/evidence/*` envelope contract in `plugin_api.py`
  (`_evidence_board_slug`, `_evidence_call`, `_EVIDENCE_EXECUTION_HOST`).
- React SDK `useQuery` / `useValue` / `atom` / `withBoard` patterns already
  in the kanban plugin.
- Node 22 (`toolchain/node-v22.22.0-linux-x64/bin`) for vitest.

## DISCOVERED

- The released `atlas-kanban-call` helper IS now installed on this host
  (`/home/hayden/.local/bin/atlas-kanban-call`), which invalidates the
  "helper missing" unit test's premise on this box.
- `withBoard` reads the global `$boardSlug` atom, not a parameter, so the
  evidence fetchers are board-pinned correctly for free.
- Tirith (the shell security scanner) blocks heredoc commit messages in
  single-query mode; `git commit -m` with plain flags is required.

## UNTOUCHED (deliberately preserved)

- All 95 parent API tests + 3 WebSocket tests + ruff; the four malformed
  receipt paths and the full-application auth tests.
- `/board`, `/tasks`, `/attachments`, `/events` local-DB routes.
- `/evidence/snapshot|page|worker|card` existing behaviour (no changes).
- Original stopped cards / board history.
- Auth middleware, connection registry, live installs, dispatcher, and any
  service/config/cap changes (none made during the run).

## MISSING (exact next edits)

1. **Board-level evidence badges** — wire `EvidenceStateBadge` (already
   exported from `evidence.tsx`) into `board.tsx` `Card`/`CardFooter`, driven
   by one `/evidence/snapshot` poll per aligned board (not `/board`+snapshot
   every poll). Merge snapshot `worker_observations` into the card map.
2. **Bounded paging + Load more** — use `fetchEvidenceSnapshot`/`fetchEvidencePage`
   (already in `api.ts`) with stable cursors bound to `(status, board)`, a
   visible remaining count (`counts.omitted` / `matching_filter`), reset on
   filter/board change, and a generation counter to reject stale in-flight
   responses on board switch.
3. **Attachment gating** — in `drawer.tsx` `AttachmentsSection`, only offer the
   `/attachments/<id>?board` download when `/evidence/context` is aligned;
   otherwise show "no linked result" (never a clickable stored_path, never a
   failure).
4. **IIFE consumer** — `plugins/kanban/dashboard/dist/index.js` (handwritten,
   4853 lines) needs the same evidence + paging; untouched this run.
5. **Browser-mounted IIFE test** — spin a task-owned headless Chromium
   (`.cache/ms-playwright/chromium-1243/.../chrome`) against a local fixture
   server serving the IIFE; assert rendered Running/Stopped/Unknown and the
   Load-more remaining count. (Backend `test_kanban_evidence_api.py` already
   covers the HTTP contract; this is the browser proof for the IIFE.)

## FRICTION

- 25-minute bound vs. reading 95k-char `AGENTS.md`, 146k-char `plugin_api.py`,
  and ~2400 lines of React. Most of the budget went to orientation.
- Heredoc commit message blocked by the shell security scanner; worked around
  with `git commit -m`.
- Pre-existing test now fails on this host due to the installed helper (see
  Verification); documented, not touched.
