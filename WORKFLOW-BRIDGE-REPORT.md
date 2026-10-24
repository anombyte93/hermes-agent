# Workflow bridge: typed readiness + continuation/hold + projections (repair)

Task t_0811e161, second pass. The parent's first-hand actual API run on the
frozen `98489cd` seam found five real defects that the original 131-test suite
was blind to; this pass fixes the root causes and extends the harness to cover
them. Scope unchanged: `plugins/kanban/dashboard/plugin_api.py`, the workflow
test file, and the two reports. Frontends, core DB, source adapter/main and
`tools/kanban_tools.py` untouched.

## Root causes and fixes (parent controls RED -> GREEN)

1. Readiness hardcoded `parents=[]`. K2 dependency readiness must reflect the
   real graph. Fix: `_aligned_parent_ids()` reads the card's direct parent ids
   from the aligned board's `task_links` (one bounded query, no traversal) and
   the route passes them; an unreadable or overbound graph (>128) is
   `state=UNKNOWN`, never a silent empty list.
2. Readiness accepted `ready_to_release=true` with a FAIL check. Fix: the
   readiness validator now requires uniquely named checks with strict
   PASS/FAIL/UNKNOWN states, finite freshness, and `ready_to_release` false on
   any FAIL/UNKNOWN/missing check or a skipped model proof.
3. Readiness accepted a mismatched `requested.workspace`. Fix: the validator
   now binds every `requested` identity field (board/profile/provider/model/
   workspace/expected_revision/python/check_model/parents) to the exact args
   sent.
4. Hold accepted `read_back={}`. Fix: the hold validator requires
   `read_back.state == "PASS"` and `read_back.data.id == card`.
5. Hold accepted a still-running readback. Fix: the hold validator requires
   `read_back.data.status` in `blocked`/`triage` (a durable hold).

Also tightened (briefed, not in the five REDs): continue now requires
`no_original_mutation=true` and `new_card_status` in `blocked`/`triage`;
draft requires a typed `commission`; and any UNKNOWN after a write says
"inspect before retry", never a blind "Retry".

## Actual-helper defect (operator note, fixed first-hand)

The released `atlas-kanban-call` exits 1 for a legitimate structured FAIL
(e.g. a denied board), and the old `_workflow_run_helper` returned before the
readiness-forwarding branch, so a real readiness FAIL lost its `checks`. Fix:
the FAIL/UNKNOWN path now runs for any exit code, forwarding a valid
structured readiness receipt's checks and only rejecting a `PASS` that
contradicts a nonzero exit. Added a process-boundary positive (helper exit 1
with structured FAIL -> FAIL + checks) and a malformed-nonzero negative
(exit 1 + garbage -> UNKNOWN).

## Harness honesty

The fake helper's readiness "pass" shape claimed `ready_to_release=true` with
`check_model=false` and only a `board_permission` check, which the real helper
never produces. It now emits a `model` check (UNKNOWN when the proof is
skipped, PASS when requested) and derives `ready_to_release` from the checks.
The readiness "fail" shape now carries the full `requested` identity and exits
1 (matching the real helper). `test_readiness_pass_resolves_task_and_git_head`
now requests `check_model=true` so a positive `ready_to_release` is honest.

## Commands run (actual)

- `scripts/run_tests.sh -j2 tests/plugins/test_kanban_workflow_api.py
  tests/plugins/test_kanban_evidence_api.py
  tests/plugins/test_kanban_dashboard_plugin.py` -> 137 passed, 0 failed.
- `scripts/run_tests.sh -j2 tests/hermes_cli/test_kanban_review_hold.py
  tests/hermes_cli/test_kanban_review_lifecycle.py
  tests/hermes_cli/test_kanban_review_lifecycle_complete.py` -> 48 passed.
- `.venv/bin/python -m ruff check plugins/kanban/dashboard/plugin_api.py
  tests/plugins/test_kanban_workflow_api.py` -> clean.

## ACTUALLY_USED / DISCOVERED / UNTOUCHED / MISSING / FRICTION

ACTUALLY_USED: `kanban_db.parent_ids` (bounded direct-parent query),
`_aligned_local_task`, the `_evidence_envelope`/`_evidence_finite_number`
helpers, a new `_workflow_unresolved_remedy` for tool-aware UNKNOWN remedies.

DISCOVERED: the released helper's exit-1 structured FAIL convention (its
readiness receipt still carries `checks`); the fake harness's former PASS was
itself incomplete (model check + `ready_to_release` semantics).

UNTOUCHED (by design): frontend/SDK/Electron, core DB, source adapter/main,
`hermes_cli/kanban_db.py`, `tools/kanban_tools.py`, the base evidence
allowlist constant. No PATH/global config edits, no live board mutation, no
worker dispatch.

MISSING: nothing required for the bounded seam. Actual authenticated
integration (production auth middleware + live helper over a temporary HTTP
server) and landing remain the parent's.

FRICTION: none material.

## Limitations

- Readiness derives only DIRECT parent ids (matching the helper's flat
  `parents` argument); transitive dependency completion is the helper's job.
- Write routes mock only the helper process boundary in tests; the actual
  authenticated helper round trip and the two UI workers' live journeys are
  the parent's verification.
- `mutation_authorized` on the draft reflects the helper's own write scope,
  inherited from this process's `ATLAS_KANBAN_WRITE_BOARDS`; an empty scope is
  a visible permission gap, never populated here.
