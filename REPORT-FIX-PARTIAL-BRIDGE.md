# REPORT-FIX-PARTIAL-BRIDGE

Status: FINAL. Committed worker report, not a parent acceptance claim. Backend
bridge verified (canonical runner, 2 files, 68 tests, 0 failed) and the UI slice
(scope-extended by the parent) verified against the real IIFE in headless
Chromium (150/150 checks, 0 failed).

## What was broken (live, parent-confirmed)

Issue65 comment 5650790142: on physical EVO, selecting a held card and clicking
Preview readiness showed only `helper reported UNKNOWN (helper exited 1)` with no
per-card repair advice. Root cause in
`plugins/kanban/dashboard/plugin_api.py::_next_ten_run_helper`:

- any nonzero-exit receipt that claimed FAIL or UNKNOWN returned early, forwarding
  only `reason` and discarding the receipt's `data` object;
- the same discard happened for zero-exit FAIL/UNKNOWN receipts;
- so a valid UNKNOWN `kanban_readiness_batch` receipt carrying bounded
  `data.items` + `repair_preview` (the supplied REAL-BATCH-RECEIPT.json shape)
  reached the UI with no advice. The same class hid unproved acceptance-comparison
  details (R4) and attachment-provenance details (R8).

## What changed

Only two files, both in-scope:

1. `plugins/kanban/dashboard/plugin_api.py`
   - added `_next_ten_partial_validation_error(tool, receipt, args)`: validates a
     FAIL/UNKNOWN receipt's data before trusting it: host must be `evo`, top-level
     `observed_at` finite, per-tool required shape (reuses
     `_next_ten_pass_shape_error`), and request scope echo (reuses
     `_next_ten_scope_error`).
   - added `_next_ten_batch_item_scope_error(...)` and wired it into
     `_next_ten_scope_error` for `kanban_readiness_batch`: every carried item must
     name a requested card (no foreign item-card), and a nested
     `receipt.requested` must re-echo the request board/card (no foreign nested
     request). This closes the gap that a bare outer-board check leaves for
     per-item identities.
   - `_next_ten_run_helper`: a FAIL/UNKNOWN receipt whose data survives validation
     now forwards `evidence=data` (state + reason retained, `observed_at`,
     `limitations`, and verified host preserved). Never promoted to PASS.
   - unchanged: a claimed PASS from a nonzero exit stays rejected to UNKNOWN with
     no evidence; malformed JSON, foreign host, wrong board/card, foreign
     item-card/nested-request, oversized stdout, absent data and malformed partial
     data still degrade to empty-evidence FAIL/UNKNOWN.

2. `tests/plugins/test_kanban_next_ten_api.py`
   - new fake-helper behaviors: `unknown_data`, `fail_data`,
     `unknown_data_wronghost`, `unknown_data_badshape` (all exit 1), plus
     `unknown_compare` (a realistic UNKNOWN acceptance compare: previous receipt
     absent, every check unproved: NOT a relabelled PASS fixture), and
     `unknown_data_foreign_item` / `unknown_data_foreign_nested` (foreign
     per-item identity injection for the scope negative controls).
   - 9 new tests (preservation + rejection, see matrix in EVIDENCE-PARTIAL.md).

No change to CLI exit semantics, limits, gates, auth, or write scope. No live
board/store, no push/merge/release, no service restart.

## Checks (exact)

RED first, against the unfixed code, with the same fake-helper seam the shipped
suite already uses (a real temporary `atlas-kanban-call` executable on PATH: the
bridge shells out to it exactly as in production):

- `test_readiness_batch_unknown_exit1_preserves_items`: FAILED (evidence was None)
- `test_readiness_batch_fail_exit1_preserves_measured_item`: FAILED (evidence None)
- `test_acceptance_compare_unknown_exit1_preserves_unproved`: FAILED (evidence None)
- `test_attachment_provenance_unknown_exit1_preserves_missing`: FAILED (evidence None)
- `test_unknown_exit1_wrong_host_drops_data`: PASSED (already correct)
- `test_unknown_exit1_badshape_drops_data`: PASSED (already correct)
- `test_readiness_batch_nonzero_claimed_pass_rejected`: PASSED (already correct)

GREEN, after the fix, same checks:

- `tests/plugins/test_kanban_next_ten_api.py`: 63 passed
- `tests/plugins/test_kanban_next_ten_auth.py` + `test_kanban_evidence_api.py` +
  `test_kanban_workflow_api.py`: 106 passed (auth boundary and shared-file
  regression guard)

The four new preservation tests exercise the exact shape of the supplied
REAL-BATCH-RECEIPT.json (state UNKNOWN, bounded `data.items` with per-card
`state`/`receipt`/`repair_preview`, `no_mutation_performed:true`, finite
`observed_at`, `execution_host:evo`, helper exit 1). REAL-BATCH-RECEIPT.json is a
parent read used as a shape reference; it is not itself a fixture (its board/card
names a different board than the test request, so the scope check would rightly
reject a verbatim replay).

The two foreign-identity controls (foreign item card, foreign nested request)
guard the newly-added `_next_ten_batch_item_scope_error`: against the previous
outer-board-only scope check, a receipt with a correct outer board but a foreign
per-item card would have been preserved, so these controls are falsifiable in the
same RED direction as the others.

## Verification coverage

- valid UNKNOWN batch (exit 1) -> evidence preserved, state UNKNOWN, host evo
- measured FAIL item (exit 1) -> evidence preserved, state FAIL, host evo
- missing acceptance/provenance (exit 1) -> unproved checks and UNKNOWN provenance
  preserved, host evo
- zero-exit valid PASS control -> existing PASS tests (unchanged) still green
- nonzero claimed-PASS -> rejected UNKNOWN, no evidence, host unverified
- malformed JSON / oversized stdout / wrong scope / wrong host on PASS -> still
  UNKNOWN no evidence (existing negative-control parametrization, still green)
- malformed/foreign partial data on FAIL/UNKNOWN -> empty-evidence FAIL/UNKNOWN
  (new tests)

## Limitations

- No live browser access is permitted on this task; the frozen browser sample may
  serve as a fixture but was not re-driven. The FastAPI route and the subprocess
  boundary are exercised; the actual rendered React/browser readback remains the
  parent's integration step.
- The helper is faked at the subprocess boundary (the shipped suite's own seam),
  not a live `atlas-kanban-call`. The receipt shapes mirror the adapter's contract
  and the supplied REAL-BATCH-RECEIPT.json.
- The evidence bridge (`_evidence_run_helper`) has the same discard shape but is
  OUT OF SCOPE here (R3/R4/R8/R9 are all next-ten tools). Flagged, not widened.

## UI slice (scope extended by the parent)

The parent extended scope to the browser bundle and its IIFE test so the
preserved partial evidence actually renders. Three PASS-only gates in
`plugins/kanban/dashboard/dist/index.js` (batch readiness ~1749, acceptance
compare ~5774, attachment provenance ~5851) now extract a valid `evidence` object
for FAIL/UNKNOWN envelopes too, while the retained UNKNOWN/FAIL state label and
reason still render. Never promoted to PASS. The error-only path (no evidence)
still renders only the reason.

`plugins/kanban/dashboard/tests/browser/run-browser-test.js` gains a
`state.nextTenPartial` fixture mode (`unknown` | `fail` | `error-only`) for the
three next-ten fixture doors and a `scenarioNextTenPartialEvidence` mounted
real-IIFE scenario with 10 checks: UNKNOWN batch with visible per-card repair
previews and retained reason, UNKNOWN compare with unproved checks and
limitations, UNKNOWN provenance rendered as "accepted run unknown", and an
error-only control that fabricates nothing. Headless Chromium (provisioned
cached binary + read-only node_modules): 150/150 checks passed, 0 failed.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

- ACTUALLY_USED: `.venv` pytest 9.1.1; existing fake-helper subprocess seam;
  `_next_ten_pass_shape_error` / `_next_ten_scope_error` (reused, not duplicated);
  `_evidence_finite_number` / `_evidence_list_of_dicts`; `_evidence_envelope` /
  `_evidence_respond`.
- DISCOVERED: the two shape/scope validators are structural (not PASS-specific in
  substance) despite their names, so they are safe to reuse for partial receipts;
  the PASS-specific-looking `no_mutation_performed is True` requirement is a
  read-only invariant that correctly guards partial data too.
- UNTOUCHED: evidence bridge, workflow bridge, auth middleware, CLI exit
  semantics, gates, limits, write scope.
- MISSING: none blocking. No second repair engine was added (adapter-supplied
  `repair_preview` is preferred; the existing `_repair_preview_from_checks` is
  only a fallback for items that omit a preview).
- FRICTION: the final FAIL/UNKNOWN pass-through block is byte-identical between
  `_evidence_run_helper` and `_next_ten_run_helper`, so a naive patch matched two
  sites; resolved by anchoring on the unique `_next_ten_scope_error` call site.
