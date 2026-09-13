# REPORT-NEXT-BROWSER.md — next-ten Kanban browser implementation (R1-R10)

Worker: evo (physical EVO), card t_c723bea3, branch `feat/next-ten-browser`,
base `19e70e911fb1067cb096df8a287f6d6035e30475` (verified: both commits sit
directly on the briefed base; `git merge-base --is-ancestor` holds).

Commits:
1. `96bde8ebc` feat(kanban): next-ten REST bridge doors R1/R2/R4/R6/R8/R9
2. `b4bcc8613` feat(kanban): next-ten browser IIFE user paths R1-R10

Scope held: only `plugins/kanban/dashboard/**`,
`tests/plugins/test_kanban_*` and this report. `apps/desktop` and the
adapter repo untouched. No second receipt store was added to plugin_api.py
(parent ruling honored: the adapter owns the ONE append-only acceptance
store; this bridge reads linkage only through the helper).

## What was built

### Backend (plugins/kanban/dashboard/plugin_api.py)

Six new doors, all through the same released-helper boundary
(`atlas-kanban-call <tool> -`, flat JSON args on stdin, write boards
stripped from the child env):

- `GET /evidence/releases` (R1) — served adapter release identity; never
  live main HEAD.
- `GET /evidence/acceptance-compare` (R4) — current/previous run
  comparison; parent attestation stays labelled; missing receipts stay
  UNKNOWN.
- `GET /evidence/reviewer-packet` (R6) — bounded packet from allowlisted
  fields; no raw bodies/results/logs/comments/argv/env/stored_path.
- `GET /evidence/attachment-provenance` (R8) — accepted run only from the
  guarded receipt association; an attachment beside a done card is not
  acceptance.
- `POST /workflow/readiness-batch` (R9) — 1..10 distinct held cards,
  check_model explicit, `extra=forbid` body, alignment guard, repair
  previews derived from failed/unknown checks only; zero
  release/dispatch/config writes.
- `GET /evidence/browser-readiness` (R2) — authenticated readiness
  separate from public reachability; lives behind the dashboard auth gate
  like every `/api/` path.

New fixed allowlist `NEXT_TEN_READ_TOOLS` (5 read-tool literals; existing
constants byte-stable). Per-tool PASS shape + scope-echo validation with no
coercion (wrong type → UNKNOWN), same stdout-cap/timeout/stderr discipline
as the evidence bridge. R3 repair-preview table is fixed text, derived only
from failed/unknown checks.

### Browser IIFE (plugins/kanban/dashboard/dist/index.js)

All existing UI functions preserved; additions:

- SupportPanel (R1/R2/R7): expandable, fetches only on expand; adapter
  identity + local frontend build stamp; readiness as three separate
  facts; refresh interval, snapshot round-trip, checked-vs-skipped process
  checks.
- Readiness panel (R3): "Next repair (preview only)" block from failed or
  unknown checks; never triggers repairs.
- DrawerNextTenSection (R4/R6/R8): acceptance compare of the two most
  recent recorded runs with labelled change kinds; reviewer packet real
  blob download (download event verified with file content equality);
  attachment provenance line (accepted run / explicit unknown).
- ReadinessBatchSection (R9): held (blocked/triage) cards from the
  snapshot as a checkbox pool; preview POST; per-card state + repair
  preview + omission lines; NO release affordance.
- Notifications (R5): intervention fingerprint
  (task+kind+reason+remedy+run) — unchanged repeat quiet, changed
  reason/remedy or new run notifies, recovery (completed / review flow)
  clears the fingerprint, board cursors remain isolated per board.
- Pagination (R10): drift detection when a refresh under appended pages
  shows a changed total; the banner reports loaded/total/previous total
  and offers "Restart paging" rather than blending stale generations.
  Board switch already resets paging (pre-existing generation guard).

## Exact test commands and results

Canonical Hermes runner (repo AGENTS requirement), vars as briefed:

```
HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
HERMES_TEST_PATHS="tests/plugins/test_kanban_next_ten_api.py" bash scripts/run_tests.sh
-> 1 files, 50 tests passed, 0 failed

HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
HERMES_TEST_PATHS="tests/plugins/test_kanban_next_ten_auth.py" bash scripts/run_tests.sh
-> 1 files, 5 tests passed, 0 failed
```

Full kanban sweep (one file per invocation; HERMES_TEST_PATHS takes one
path per run, a multi-path value was refused by the runner as a single
nonexistent path — see FRICTION):

```
test_kanban_dashboard_plugin.py          43 passed
test_kanban_evidence_api.py              61 passed
test_kanban_workflow_api.py              40 passed
test_kanban_board_lifecycle_api.py        4 passed
test_kanban_attachments.py                9 passed
test_kanban_board_project_api.py          6 passed
test_kanban_dashboard_task_updated_hook.py 2 passed
test_kanban_estimate.py                   5 passed
test_kanban_model_override.py            18 passed
test_kanban_worker_runs.py                4 passed
test_kanban_ws_idle_disconnect.py         3 passed
test_kanban_next_ten_api.py              50 passed  (new)
test_kanban_next_ten_auth.py              5 passed  (new)
total: 13 files, 250 tests, 0 failed
```

Browser harness (real IIFE, real DOM events, real WebSocket frames,
headless Chromium on local fixtures only, desktop 1280px + 390px):

```
PATH=<node22 toolchain>:$PATH NODE_PATH=<repo>/node_modules \
  node plugins/kanban/dashboard/tests/browser/run-browser-test.js
-> 129/129 checks passed (96 pre-existing preserved + 33 new)
```

R2 auth proof (tests/plugins/test_kanban_next_ten_auth.py) uses the REAL
`web_server.app` with `auth_required=True` and a real registered password
provider, the plugin router mounted through the same `include_router`
call the server performs: unauthenticated → 401 JSON with no evidence
fields; login with valid credentials then → 200 full envelope; wrong
password → 401, never authenticates; public-path allowlist provably does
not exempt the new doors. No direct function calls, no weakened auth.

## Red controls (falsifiability)

Control script `/tmp/kbt-smoke/red_controls.sh` (scratch, disclosed):

1. Scope gate: disabled the board scope-echo check for
   `kanban_release_identity` → `test_next_ten_negative_controls[...
   wrongscope ... /evidence/releases]` FAILED (49/50), proving the route
   actually validates the receipt's board echo. Restored → 50/50.
2. R5 gate: removed the `if (prevFp === fp) continue;` fingerprint line →
   browser checks `notices-r5: unchanged repeat is quiet` and
   `notices-r5: changed reason notifies` FAILED (127/129), proving the
   quiet/notify discrimination is real. Restored → 129/129.

Both mutations red for the RIGHT reason, both suites green after restore.

## Negative controls covered by the new suites

- PASS with missing/wrong-typed shape fields → UNKNOWN (per tool).
- Wrong board/card/attachment echo → UNKNOWN.
- Wrong execution_host → UNKNOWN; nonzero-exit PASS → UNKNOWN.
- Oversized stdout → UNKNOWN; malformed stdout → UNKNOWN.
- Missing helper → UNKNOWN with remedy; FAIL receipts forwarded with
  reason; timeout → UNKNOWN.
- Batch: duplicate cards deduped; >10 distinct → 422; extra body field
  (`release: true`) → 422; unaligned board → UNKNOWN before any helper
  call; malformed card id → 422 with zero invocations.
- Auth: 401 JSON without evidence; wrong password stays 401.
- UI: empty held pool disables the run button and fires no POST; no
  release affordance exists anywhere in the batch section.

## Limitations

- The adapter-side tools (`kanban_release_identity`,
  `kanban_acceptance_compare`, `kanban_reviewer_packet`,
  `kanban_attachment_provenance`, `kanban_readiness_batch`) are consumed
  here per CONTRACT.md shapes; the sibling adapter worker owns their
  server-side implementation. Until both land and the parent wires the
  real adapter, these doors honestly read UNKNOWN on a host without the
  released helper.
- R7 process-check counts render "checked unknown / skipped unknown"
  until the snapshot envelope carries `process_checks`; the panel never
  invents numbers.
- The reviewer packet download uses a blob URL + programmatic anchor
  click; verified via the browser's download event with content equality.
- The acceptance-compare drawer affordance derives run ids from the
  drawer's bounded runs page (the loaded window), not the full run
  history; with fewer than two loaded runs it renders the honest
  "needs two recorded runs" state.
- R10 drift detection compares totals between page-0 refreshes; it does
  not attempt per-card generation tags (out of contract scope).

## Tools actually used / misses

Used: Hermes kanban tools (show/heartbeat/complete), terminal, read_file,
write_file, patch, the repo's `scripts/run_tests.sh`, node-v22 toolchain
at the briefed path, repository node_modules (read-only hardlinked cache
provided by the parent; untouched, no install run), Playwright Chromium
from `/home/hayden/.cache/ms-playwright` via the existing harness.

Misses: none blocking. Notes: (1) multi-path `HERMES_TEST_PATHS` is not
supported by the runner — sweep ran one file per invocation (disclosed
above); (2) single-query terminal mode refused an inline multi-file bash
loop — written to a scratch script instead; scratch leftovers disclosed
under /tmp/kbt-smoke/ (smoke scripts, red-control script, run logs,
plugin/index backups used for control restore); (3) no capability-service
MCP was reachable from this session, so the bare-hands proof is this
report plus the committed diffs and logged test output.

## FRICTION

- `HERMES_TEST_PATHS` accepts ONE path; the brief's plural phrasing
  suggested a list. Runner error text was clear; cost one rerun.
- The first SupportPanel draft kept fetch results in state that fed its
  own effect deps, so results were dropped on cleanup (caught by the
  browser scenario, fixed with a ref). Existing 96 checks never
  regressed at any point.
- The fake-helper fixture for the new API tests embeds the data builder
  in the executable's script text (self-contained subprocess); the first
  draft tried to import the test module from the fake — impossible.
- Local git identity is unset on this box; commits were made with
  `-c user.name=evo -c user.email=evo@local` per the sibling-worker
  precedent in this workspace.
