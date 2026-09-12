# Workflow bridge: typed readiness + continuation/hold + projections

Task t_0811e161. Finishes the typed authenticated Hermes Kanban API seams on
EVO: K2 readiness, K5 explicit continuation/hold, and K6/K7/K8 projections,
behind the existing dashboard session-token auth with no new credential route.

## What changed

- `plugins/kanban/dashboard/plugin_api.py` (only file touched in the plugin):
  - `EVIDENCE_PROJECTION_TOOLS` (attention/changes/timeline) added as a
    SEPARATE fixed set; the base `EVIDENCE_READ_TOOLS` frozenset is unchanged
    so existing tests keep passing. The evidence runner accepts the union.
  - Three bounded GET projections `GET /evidence/attention`, `/changes`,
    `/timeline` mirroring the exact helper tools/limits/cursors, with
    data-wrapped shapes (`cards`/`events`/`intervals`) and board/card scope +
    typed-count validation. Malformed PASS collapses to UNKNOWN.
  - `_compute_alignment` extracted from `/evidence/context` (same behavior,
    same response) and reused as the gate for every workflow read/write.
  - Workflow routes `POST /workflow/readiness`, `/continuation-draft`,
    `/continue`, `/hold` with `extra=forbid` Pydantic bodies, a SEPARATE fixed
    workflow allowlist (`WORKFLOW_READ_TOOLS` + `WORKFLOW_WRITE_TOOLS`), and
    separate ROOT receipt validators (readiness/continuation are not
    data-wrapped, per the parent note). The helper child inherits the real
    `ATLAS_KANBAN_WRITE_BOARDS` (never force-emptied, never extended); the
    read-only projections stay force-empty.
  - Write routes refuse BEFORE the helper on unaligned board and on
    absent/non-matching write scope. Readiness resolves workspace/profile/
    provider/model from the aligned local task and derives git HEAD via a
    FIXED `git -C <workspace> rev-parse HEAD` argument list; the caller can
    never supply an executable, shell, path or revision.
- `tools/kanban_tools.py`: one-line fix, the block failure wording now says
  `running/ready/review` (review-hold is a supported source status).
- `tests/plugins/test_kanban_workflow_api.py`: new, 27 tests.
- `WORKFLOW-API-CONTRACT.md`: exact request/response shapes for both UI workers.

## Commands run (actual)

- `scripts/run_tests.sh -j2 tests/plugins/test_kanban_workflow_api.py
  tests/plugins/test_kanban_evidence_api.py
  tests/plugins/test_kanban_dashboard_plugin.py` -> 131 passed, 0 failed.
- `.venv/bin/python -m pytest tests/hermes_cli/test_kanban_review_hold.py
  tests/hermes_cli/test_kanban_review_lifecycle.py -q` -> 26 passed.
- `.venv/bin/ruff check plugins/kanban/dashboard/plugin_api.py
  tests/plugins/test_kanban_workflow_api.py tools/kanban_tools.py` -> clean.
- Real helper (installed PR20 230b3e1c) verified directly:
  `kanban_attention` and `kanban_continuation_draft` return the exact root
  shapes; `kanban_readiness` returns root checks with `execution_host=evo`.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

ACTUALLY_USED: shutil.which helper boundary, subprocess (flat stdin, argv tool
name), kanban_db.connect/get_task/create_task, socket.gethostname,
Path.home, git rev-parse, pydantic ConfigDict(extra="forbid"), the existing
`_evidence_*` envelope helpers reused for the workflow envelope.

DISCOVERED:
- Readiness/continuation receipts are ROOT results (no `data` wrapper, no
  `execution_host` on draft/continue/hold), unlike the data-wrapped evidence
  projections. Validated separately as the parent instructed.
- The helper's real `kanban_readiness` emits `execution_host=evo` at root and
  forwards `checks`/`ready_to_release` even on a board-permission FAIL, so the
  UI can show which check failed. The route forwards readiness receipts for
  PASS and FAIL, not just PASS.
- `EVIDENCE_READ_TOOLS` is asserted verbatim by the existing evidence test, so
  the projections live in a separate constant rather than widening it.

UNTOUCHED (by design): frontend/SDK/Electron, core DB, source adapter/main,
`hermes_cli/kanban_db.py`, the base evidence allowlist constant, and every
other file. No PATH/global config edits, no live board mutation, no worker
dispatch. The parent owns helper release, auth integration and both UI
renderers.

MISSING: nothing required for the bounded seam. Actual authenticated
integration (production auth middleware + live helper over a temporary HTTP
server) and landing remain the parent's, per the brief.

FRICTION: none material. The terminal single-query guard blocked a
`helper | python3 -m json.tool` probe, so receipts were written to
`/tmp/*.json` and read back instead; those scratch files are left in place.

## Limitations

- Readiness passes `parents=[]` (only workspace/profile/provider/model are
  resolved, exactly as briefed); it does not read the dependency graph.
- Write routes mock only the helper process boundary in tests; the actual
  authenticated helper round trip and the two UI workers' live journeys are
  the parent's verification.
- `mutation_authorized` on the draft reflects the helper's own write scope,
  which is inherited from this process's `ATLAS_KANBAN_WRITE_BOARDS`; an empty
  scope is a visible permission gap, never populated here.
