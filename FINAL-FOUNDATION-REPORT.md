# FINAL Foundation Report — t_b2f6f659 (browser board + drawer evidence journeys)

Physical EVO worker (profile evo, deepseek-v4-pro). Workspace
`/home/hayden/atlas/work/trajectory-20260912/ui-browser`, branch
`feat/kanban-browser-evidence-20260912`. Sole purpose: finish the K10
foundation gaps for the handwritten IIFE browser dashboard in
`plugins/kanban/dashboard/dist/index.js`, with the real headless browser
harness in `plugins/kanban/dashboard/tests/browser/`.

## Exact result

Browser suite: 34/34 checks pass. The original 18 assertions from cb5f3c7 are
all preserved and still green; 16 new assertions cover the K10 gaps. Run with:

```
PATH=/home/hayden/atlas/work/trajectory-20260912/toolchain/node-v22.22.0-linux-x64/bin:$PATH \
NODE_PATH=/home/hayden/atlas/work/trajectory-20260912/ui/node_modules \
node plugins/kanban/dashboard/tests/browser/run-browser-test.js
```

Two commits landed:

1. `3839acf` feat(kanban): bounded drawer runs/events/attachments via
   /evidence/page (K10). The IIFE rewires the drawer so RUNS/EVENTS/ATTACHMENTS
   each page through the bounded `/evidence/page` route with one stable cursor
   per resource, an omissions rollup, and a visible Load more that appends real
   rows.
2. `9f24fc4` test(kanban): browser harness for bounded drawer
   runs/events/attachments (K10). Fixture is resource-aware and multi-page, the
   detail route mirrors include_history=false, and the phone/race/stale/
   helper-unavailable scenarios are added.

## What the drawer does now (K10)

- A new `useEvidenceResourcePage(boardSlug, cardId, resource, enabled)` hook
  owns one bounded cursor per drawer resource (runs | events | attachments).
  Load more appends visible details (deduped by id), never just flips a flag,
  and clears the affordance after the final page. Generation is bumped on
  board/card/resource change so a slow page for the previous card cannot leak
  into the current one.
- When the board is EVO-aligned, `TaskDetail` sources runs/events/attachments
  from the three evidence pages instead of the unbounded legacy detail read.
  When not aligned, the legacy canonical read is unchanged.
- The evidence attachment list is consolidated into the single
  `AttachmentsSection` (evidence mode: header "Evidence attachments", 404
  "missing file:" surfacing, Load more). The previous second
  `EvidenceAttachmentsSection` panel is deleted: no second Evidence History
  panel. `WorkerEvidenceSection` (live worker state, K9) is kept.
- `RunHistorySection` and the events block gain a Load more + omissions + a
  total count; the "+N earlier" collapse only applies to the unbounded
  canonical list.
- The board-level snapshot still refreshes counts/worker map/freshness on
  poll without resetting the Load-more cursor (page-0 replace keeps appended
  pages stable), and a removed running observation resolves to Unknown, never
  a stale Running with a fresh timestamp. This was already in place and is
  unchanged.

## Backend read option (parent-owned, reported not implemented here)

The legacy `GET /tasks/{id}` always materialised full history
(`list_events`/`list_attachments`/`list_runs` and derived diagnostics), which
is an unbounded read. The parent has added an optional `include_history` query
flag to that route (default `true`, so all other callers are unchanged):

- `?include_history=false` returns `history_included=false` and
  `diagnostics_state="UNKNOWN"`, and does not materialise
  events/attachments/runs or diagnostics; `task`/`comments`/`links`/
  `child_results` keep existing behaviour (not claimed bounded).
- The IIFE now sends `include_history=false` on aligned drawers only, and
  reads the three resources from `/evidence/page`. When the response reports
  `diagnostics_state="UNKNOWN"` the drawer shows an honest "Not loaded:
  diagnostics are unavailable on an evidence read." instead of silently
  collapsing the section to an implicit "no diagnostics".

I did not write or commit the Python change: it is parent-owned and left in the
working tree uncommitted. The default `include_history=true` is preserved for
all non-aligned callers.

## Test envelopes match the real contract

The `/evidence/page` fixture returns the `kanban_page` shape from
EVIDENCE-CONTRACTS.json (items, returned, has_more, next_cursor, total,
omitted, remaining_after_page, high_water_rowid, evolving_view). Run/event/
attachment item shapes match the plugin serialisers (`_run_dict`,
`_event_dict`, `_attachment_dict`). Network boundary mocking only: fetchJSON
and authedFetch hit the fixture over the real fetch(); the real IIFE is bundled
with esbuild and driven by Playwright against headless chromium-1243, closed in
finally.

## New browser coverage

- Multi-page history/attachments: runs header shows total 3, Load more appends
  the third run and clears; events and attachments Load more each append their
  next page and clear after exhaustion; no omissions after exhaustion.
- Helper unavailable: a FAIL snapshot renders the "Worker evidence unavailable"
  banner with reason and remedy, never a green board.
- Stale freshness: an observation 400s old renders the red stale tier
  (`hermes-kanban-evidence--stale-red`) with an "observed Ns ago" label. Clock
  advance is simulated via a past `observed_at`, which is equivalent for the
  IIFE's age computation.
- Drawer card race: a runs page in flight for one card is dropped when the user
  opens another card (the second card shows its own run, the late page never
  leaks).
- Phone width 390x844: the real `dist/style.css` is served so column layout and
  overflow render as the host does; the columns strip scrolls horizontally
  (scrollWidth 2210 > clientWidth 374) and a card click target is genuinely
  visible (Playwright click succeeds, drawer opens).
- Desktop 1280 width is exercised by every other scenario.

## ACTUALLY_USED

- The released bridge envelope contract in EVIDENCE-CONTRACTS.json (snapshot,
  page, worker).
- The parent-owned `include_history=false` detail route, exercised through the
  fixture mirror of its response contract.
- Playwright 1.62.1 + the task-pinned chromium-1243 binary at
  `/home/hayden/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome` via
  explicit executablePath.
- esbuild to bundle React 19.2.7 + react-dom/client + the IIFE.
- Node 22 at `toolchain/node-v22.22.0-linux-x64/bin`.
- The real `plugins/kanban/dashboard/dist/style.css` served into the fixture
  page so layout/overflow assertions are faithful.

## DISCOVERED

- The drawer fixture page previously served only the bundle, so column width
  and horizontal overflow were unstyled (scrollWidth == clientWidth). Serving
  the real style.css is what makes the horizontal-scroll assertion meaningful.
- The legacy detail read was the only unbounded fetch; include_history=false is
  the supported parent-owned read option, and the IIFE must gate it on
  alignment so non-aligned callers keep full history.

## UNTOUCHED (deliberately preserved)

- `plugin_api.py` and every other Python file: the include_history route change
  is parent-owned and left uncommitted in the working tree.
- The React consumer (`apps/desktop`) and all other plugin files.
- The WebSocket subscription and its event callback: still live, still routes
  to the single `loadBoard`; no duplicate poller added, no existing invalidation
  disabled. Evidence refresh is wired into the existing refresh path only.
- All original reports (BROWSER-EVIDENCE-REPORT.md, UI-EVIDENCE-REPORT.md,
  UI-BRIDGE-REPORT.md, UI-BRIDGE-REPAIR-REPORT.md, UI-BRIDGE-TYPES-REPORT.md)
  are unchanged.
- The 18 original assertions: all preserved and passing.
- No child cards, subagents, pushes, merges, live installs, service/config/cap/
  credential changes, raw argv/env, browser auth, or unrelated-board edits.

## MISSING

- Actual authenticated HTTP for /evidence/* and /attachments/* against the real
  EVO host remains parent proof: the browser test mocks only the network
  boundary, so bearer/cookie auth is exercised against the fixture, not a live
  server. This is out of scope for this worker by the brief.
- After an upload or delete on an aligned drawer, the bounded attachment list
  does not auto-refresh in place: the write goes through the canonical endpoint
  and the board refreshes, but the evidence attachment page for the open card
  refetches on card/board change (or next drawer open), not on that write. A
  refresh tick for attachment writes is a small follow-up, not required by the
  K10 read-pagination mandate.

## FRICTION

- `node -e`/`-c` shapes and execute_code are refused in this single-query mode;
  scratch verification used `node --check <file>` and full file writes instead.
- The Load-more button label reads "Load more (1 omitted)": the first assertion
  regex guessed "omitted 1" and had to be corrected to "1 omitted".

## Honesty note

This run finished only the K10 foundation gaps. K2 and K5-K8 remain explicitly
out of scope and were not implemented or claimed. The drawer now pages runs,
events and attachments through bounded evidence reads with stable cursors and
visible Load more, and the harness proves it across desktop and phone widths.
