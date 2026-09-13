# REPORT-PARTIAL-COUNTS

Status: FINAL. Worker report for the C10/C11 partial-batch correction, not a
parent acceptance claim. Backend bridge fixed and verified; parent owns
acceptance and landing.

## What was rejected (parent review controls)

Parent's independent GLM FastAPI/subprocess controls (PARENT-REVIEW-CONTROLS)
reproduced two forward-pass defects in the partial-evidence bridge, both in
`plugins/kanban/dashboard/plugin_api.py`:

- C10_count_mismatch: a UNKNOWN readiness_batch receipt whose `data.requested`
  (99) does not reconcile with the carried items (1) was forwarded as evidence.
- C11_duplicate_items: a receipt carrying the same card identity in two items
  was forwarded as evidence.

Both should degrade to empty-evidence UNKNOWN (state and reason retained, no
trusted evidence). C12 (absent item.card) already dropped correctly, but the
item-card path did not reject non-string identities and would have raised an
unhandled TypeError on an unhashable card (dict/list) instead of rejecting.

## What changed (two files, both in scope)

1. `plugins/kanban/dashboard/plugin_api.py`

   - `_next_ten_pass_shape_error` (kanban_readiness_batch): the count check is
     now exact. `returned` must equal `len(items)`, and `requested` must equal
     `returned + omitted`. The previous conditional only rejected when BOTH
     `requested` and `returned` differed from `len(items)`, so a wrong
     `requested` alone slipped through. This closes C10 at the shape layer.
   - `_next_ten_scope_error` (kanban_readiness_batch): added the request-scope
     count echo: `requested` must equal the number of unique cards actually
     selected (`len(set(args.cards))`). Closes the C10 sub-case where the
     receipt reconciles `requested == returned + omitted` but still claims a
     request size the caller never sent.
   - `_next_ten_batch_item_scope_error`: every carried item's `card` must be a
     non-empty string identity (absent/null/unhashable reject, no TypeError),
     and a duplicated card identity across items rejects. Closes C11 and the
     missing/unhashable card requirement. Nested `receipt.requested`
     board/card echo checks are preserved unchanged.

   No limits raised; no UI, auth, write scope, CLI exit semantics, or unrelated
   changes. Valid partial omissions and error-only card results remain
   preserved (see the omitted-batch positive test). A claimed PASS from a
   nonzero exit stays rejected.

2. `tests/plugins/test_kanban_next_ten_api.py`

   - New fake-helper behaviors: `unknown_data_count_mismatch`,
     `unknown_data_requested_count_mismatch`, `unknown_data_duplicate_item`,
     `unknown_data_missing_card`, `unknown_data_unhashable_card`,
     `unknown_data_omitted`.
   - New regression tests (below).

## Tests (exact)

Canonical runner (hermetic env -i, per-file isolation), as required:

    HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
      scripts/run_tests.sh tests/plugins/test_kanban_next_ten_api.py
    -> 69 passed, 0 failed

    HERMES_TEST_WORKERS=2 HERMES_TEST_FILE_RETRIES=0 \
      scripts/run_tests.sh \
        tests/plugins/test_kanban_next_ten_auth.py \
        tests/plugins/test_kanban_evidence_api.py \
        tests/plugins/test_kanban_workflow_api.py
    -> 106 passed, 0 failed (auth 5, workflow 40, evidence 61)

    Total focused: 175 passed, 0 failed.

Supplied parent driver, run unchanged (args: `<plugin_root> <receipts_dir>
<results_json_out>`):

    .venv/bin/python PARENT-REVIEW-CONTROLS/driver.py . \
      PARENT-REVIEW-CONTROLS /tmp/driver-results-after.json
    -> failures: [], preserved_ok: true, n_cases: 16

    RED first (before the fix), same driver: failures = [C10, C11]; the other
    14 cases already passed. GREEN after the fix: 16/16.

## New regression matrix

- test_readiness_batch_count_mismatch_dropped
      requested=99 vs returned=1/omitted=0 -> evidence None (C10, shape layer)
- test_readiness_batch_requested_count_mismatch_dropped
      requested=2 vs 1 unique selected card -> evidence None (C10, scope layer)
- test_readiness_batch_duplicate_item_dropped
      two items share one card -> evidence None (C11)
- test_readiness_batch_missing_item_card_dropped
      item.card absent -> evidence None
- test_readiness_batch_unhashable_item_card_dropped
      item.card = {dict} -> evidence None, status 200 (no 500)
- test_readiness_batch_omitted_batch_preserved
      requested=2, returned=1, omitted=1, one carried item -> evidence PRESERVED
      (valid omission stays accepted; host evo, no_mutation_performed True)

Each negative control is a positive control in reverse: a real malformed or
inconsistent payload, watched to degrade to empty-evidence UNKNOWN rather than
pass through. The omitted-batch test is the positive direction for the same
count invariants, proving valid omissions are not over-rejected.

## Limits enforced (unchanged, now exact)

- returned == len(items)
- requested == len(unique requested cards)
- requested == returned + omitted
- each item.card is a non-empty string; duplicates rejected
- no_mutation_performed is True; observed_at finite; execution_host == evo
- 1..10 distinct cards per batch (route-level, unchanged)
- stdout cap, timeout, CLI exit semantics, auth and write scope unchanged

## Limitations

- The helper is faked at the subprocess boundary (the shipped suite's own
  seam), not a live `atlas-kanban-call`; receipt shapes mirror the adapter
  contract and the supplied REAL-BATCH-RECEIPT.json / PARENT-REVIEW-CONTROLS
  receipts.
- No live browser access is permitted; the rendered React/browser readback
  remains the parent's integration step.
