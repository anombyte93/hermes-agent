# EVIDENCE-PARTIAL

Raw RED/GREEN evidence for the partial-evidence bridge fix. Committed for the
parent's independent review; the supplied REAL-BATCH-RECEIPT.json is the shape
reference, not a test fixture (its board/card names a different read than the
test request, so a verbatim replay would be rightly scope-rejected).

## RED (unfixed code, observed first)

Command (hermetic per the canonical runner's env; direct pytest for iteration):

    .venv/bin/python -m pytest tests/plugins/test_kanban_next_ten_api.py -q \
        -k "exit1 or claimed_pass"

Result: 4 failed, 3 passed, 54 deselected.

The four failures are exactly the preservation tests. Representative failure:

    def test_acceptance_compare_unknown_exit1_preserves_unproved(...):
        ...
        body = r.json()
        assert body["state"] == "UNKNOWN"
        ev = body["evidence"]
    >   assert isinstance(ev, dict)
    E   assert False
    E    +  where False = isinstance(None, dict)

i.e. the helper's valid data payload was discarded (evidence None) before any
validation, matching the live symptom (UI shows only `helper reported UNKNOWN
(helper exited 1)` with no per-card advice). The three passing controls
(wrong-host drop, bad-shape drop, claimed-PASS reject) were already correct.

## GREEN (fixed code, same checks)

    .venv/bin/python -m pytest tests/plugins/test_kanban_next_ten_api.py -q
    -> 63 passed

    .venv/bin/python -m pytest \
        tests/plugins/test_kanban_next_ten_auth.py \
        tests/plugins/test_kanban_evidence_api.py \
        tests/plugins/test_kanban_workflow_api.py -q
    -> 106 passed

Total focused: 169 passed, 0 failed.

## New test matrix (additions to test_kanban_next_ten_api.py)

- test_readiness_batch_unknown_exit1_preserves_items        (UNKNOWN + items preserved)
- test_readiness_batch_fail_exit1_preserves_measured_item   (FAIL + measured item preserved)
- test_acceptance_compare_unknown_exit1_preserves_unproved  (realistic UNKNOWN compare)
- test_attachment_provenance_unknown_exit1_preserves_missing (UNKNOWN provenance preserved)
- test_unknown_exit1_wrong_host_drops_data                  (foreign host -> empty evidence)
- test_unknown_exit1_badshape_drops_data                    (malformed data -> empty evidence)
- test_readiness_batch_foreign_item_card_dropped            (foreign item card -> empty evidence)
- test_readiness_batch_foreign_nested_request_dropped       (foreign nested request -> empty evidence)
- test_readiness_batch_nonzero_claimed_pass_rejected        (PASS + nonzero -> UNKNOWN, no evidence)

## Falsifiability

Each negative control is a positive control in reverse: a real foreign identity
or malformed payload, watched to degrade to empty-evidence FAIL/UNKNOWN rather
than pass through. The preservation tests are the positive direction: a real
bounded payload, watched to survive. RED-then-GREEN on the SAME assertions is
the proof the fix (not the fixture) changed the behaviour.

## Scope note

The evidence bridge (`_evidence_run_helper`) has the same discard shape for
FAIL/UNKNOWN-with-data but is out of scope for this R3/R4/R8/R9 card; flagged
for the parent, not widened here.
