# Workflow browser UI — report (K2/K3/K4/K5/K6/K7/K8 + notifications)

Branch feat/kanban-browser-workflow-20260913. Physical EVO, evo/deepseek/deepseek-v4-pro.

## What was implemented

The browser IIFE surface (plugins/kanban/dashboard/dist/index.js + dist/style.css)
carries the workflow panels from WORKFLOW-UI-CONTRACT.md, using the standard
envelope and route schemas from WORKFLOW-API-CONTRACT.md (materialised mid-run,
source not yet parent-verified). Network is the only mocked layer; the real IIFE,
real React components, real CSS and the existing SDK.fetchJSON + selected-board
identity are exercised unchanged.

- K2 attention queue: GET /evidence/attention, bounded queue, visible omission
  count, "More attention" paging, exact card opens the drawer.
- K3 changes: GET /evidence/changes, baseline-now first read, bounded 30s poll
  while visible, cursor kept after baseline, visible freshness. No second
  notifier for polling; the existing /events stream is the only notifier.
- K4 timeline: GET /evidence/timeline, disjoint execution/blocked/review/unknown
  intervals, covered-window totals labelled "wall-clock observation, not
  productivity", visible gaps, bounded paging.
- K5 readiness: POST /workflow/readiness on explicit click only, all checks
  rendered with the board_permission check shown as a separate Permission row
  (mutation authorized / not authorized), ready_to_release verdict, FAIL/UNKNOWN
  checks in evidence still rendered (including permission and model reason).
- K6/K7 continuation: read-only draft, then a separate "Create continuation"
  click. Draft shows the fingerprint, the original card (result and latest-run
  excerpts labelled "unverified"), worker verdict, passed/remaining checks.
  Continue sends the EXACT reviewed draft commission + checks + note (never a
  rebuild from live inputs); changed inputs invalidate the draft and late
  mutation replies are discarded via a generation guard. Held result shows the
  new card and an "Open new card" action. Stale fingerprint requires a re-draft;
  UNKNOWN says inspect before retrying.
- K8 hold: POST /workflow/hold, separate explicit click, shows the held result
  and the "does not stop a live worker" warning.
- K7 notification region (this revision): actionable, deduplicated change
  notifications driven exclusively by the EXISTING /events WebSocket stream
  (the onmessage is extended, never a second WebSocket / process / poll
  notifier). review_requested and changes_requested are new; completed, blocked,
  gave_up, crashed and timed_out are the existing terminal kinds. Each notice
  row opens the exact card through the existing selected-board/drawer state, no
  new OS permissions. The notification baseline comes from GET /events/baseline
  (one MAX(id) from the same selected-server DB as /events, works local and EVO
  alike), gated on strict PASS + exact board + nonnegative integer baseline id.
  An unknown baseline (FAIL/UNKNOWN/malformed/wrong-board) suppresses historical
  notifications and offers a retry; it never replays history. Per-board dedup /
  replay / switch isolation; duplicate frames collapse; late previous-board
  frames are ignored. No dispatch is ever issued.

No auto readiness/model, no dispatch, no unblock, no second notifier. Workflow
writes carry the board in the query param and the card in the JSON body.

### EvidenceBanner de-duplication (parent real-SPA finding)

The EvidenceBanner previously rendered every snapshot card as a flat repeated
list above the real board, pushing BoardColumns below the fold (visible once the
parent's full built SPA + authenticated API + installed EVO helper rendered 40
cards). Removed the duplicated per-card list: the banner is now a compact strip
of identity + counts + freshness + Load more, and the actual cards (with their
per-card worker badges) live only in BoardColumns, driven by the same snapshot.
Existing card-locating tests continue to target BoardColumns, not a debug list.

## Exact commands

```
export NODE_PATH=/home/hayden/atlas/work/trajectory-20260912/ui/node_modules
export PATH=/home/hayden/atlas/work/trajectory-20260912/toolchain/node-v22.22.0-linux-x64/bin:$PATH
node plugins/kanban/dashboard/tests/browser/run-browser-test.js
```

Result: 96/96 checks passed (34 pre-existing foundation checks preserved, plus
41 workflow checks from the first run, plus 21 notification-region checks),
exit code 0. Syntax: node --check clean on dist/index.js,
run-browser-test.js and fixture-entry.js.

The notification cases are driven by a real WebSocket fixture server: the IIFE
opens a genuine /events socket (fixture buildWsUrl resolves a real ws:// URL)
and the harness sends real frames over the wire (handshake + unmasked server
frames implemented in the harness, no `ws` dependency, no installs). No
callback is invoked by hand and nothing is source-grepped.

## PASS / FAIL / UNKNOWN

- PASS: all 34 foundation checks (snapshot grid, real attachment download,
  run/event/attachment paging, drawer race, phone column scroll, stale/helper
  banner) still green after the workflow + notification additions and the
  EvidenceBanner de-duplication.
- PASS: all 41 workflow checks (attention paging + exact drawer, timeline
  kinds/gaps/paging, readiness only-on-click + separate permission, draft ->
  separate create -> held, stale re-draft, denied writes, late-card response
  dropped, no dispatch, phone workflow surface).
- PASS: all 21 notification-region checks (baseline/replay zero alerts, newly
  arrived review_requested + changes_requested open exact cards, duplicate
  frame once, board-switch isolation, late previous-board frame ignored,
  FAIL/UNKNOWN/malformed/wrong-board baseline -> unknown + retry, retry
  recovery, 390px phone, no dispatch).
- FAIL: none.
- UNKNOWN: none in the fixture suite. The final authenticated API connection and
  the real GET /events/baseline route are parent-owned and NOT exercised here;
  renderer network fixtures (including the WS server) are substitute proof only,
  as briefed.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

- ACTUALLY_USED: window.__HERMES_PLUGIN_SDK__ (React + primitives + fetchJSON +
  buildWsUrl), window.__HERMES_PLUGINS__.register, the existing withBoard() and
  evidence bridge, the existing selected-board identity
  (localStorage hermes.kanban.selectedBoard), the existing /events WebSocket
  onmessage path, the existing headless Chromium harness + fixture HTTP server,
  real esbuild IIFE bundle, and a real WebSocket upgrade + frame server.
- DISCOVERED: the live receipt schemas in ADAPTER-LIVE.json (attention/changes/
  timeline/continuation-draft/continue/card), and the helper receipts in
  ADAPTER-SERVER.py / ADAPTER-CONTINUATION.py (readiness checks + hold receipt).
  The mid-run WORKFLOW-API-CONTRACT.md corrected the envelope shape: workflow
  receipts live under evidence, not at the top level. A mid-run parent note
  corrected the notification baseline source to GET /events/baseline (EVO+local,
  one MAX(id)) instead of the EVO-only /evidence/changes; the changes panel keeps
  its own cursor, unchanged.
- UNTOUCHED: React board (plugins/kanban/dashboard React tree), Python plugin_api
  and SDK, the separate drawer worker's workspace and drawer.tsx, the changes
  panel's /evidence/changes cursor behaviour, and the existing /events WS
  reconnect/taskEventTick/scheduleReload path (only its onmessage was extended to
  feed the notice region). No installs, no config changes, no child cards, no
  push/merge.
- MISSING (parent-owned): the actual authenticated /workflow/* ,
  /evidence/attention|changes|timeline routes AND the new GET /events/baseline
  route are not present in this workspace's plugin_api.py; the API worker
  t_0811e161 owns that surface in another workspace. Final WORKFLOW-API-CONTRACT.md
  is "source not yet parent-verified".
- FRICTION: the shared evidenceEnvelope catch remedy ("Worker evidence is
  unavailable on this server") collided with the existing "text=Worker evidence"
  selector once workflow sections reused it; fixed with a dedicated
  workflowEnvelope that never emits the worker-evidence wording (prior run).
  No `ws` module exists in node_modules and installs are forbidden, so the
  harness implements the WebSocket handshake + server-frame encoding directly
  over Node http/crypto.

## Known gaps (honest)

- Continue accepts the draft's own commission; a draft whose commission was
  omitted (read-only draft) has no commission to send and continue will be
  refused by the real helper as "commission required". The UI surfaces that as
  the FAIL reason, it does not invent a commission.
- The changes 30s poll is bounded and cursor-driven; it is not a liveness proof.
  An empty fresh page is shown as "no observed changes", never worker liveness.
- Timeline durations are rendered as wall-clock observation only.
- The notification region reads GET /events/baseline once per board
  (re-establishment), plus once on a retry; a board switch can issue the read
  twice before the alignment flag settles, which the generation guard makes
  harmless (idempotent single MAX(id) read, only the latest result is applied).
- The /events/baseline route and its Python implementation are parent-owned; the
  renderer treats any non-PASS / non-exact-board / non-nonnegative-integer
  baseline as "unknown" and suppresses notifications with a retry, never a
  replay.
