# CI stale-head rerun and cancellation truth

Repairs two defects captured in ISSUE60.md. Parent retains acceptance and
all release authority; this report records the exact RED/GREEN state and the
boundaries of what was and was not exercised.

This is a bounded same-profile retry that addresses the two parent findings
against the earlier attempt (whose four commits are preserved in history).
The parent independently executed the ORIGINAL workflow scripts and
confirmed 2 RED (stale-head rerun and cancelled aggregate fail correctly)
and 2 passing controls; this report does not reimplement that RED.

## Parent findings addressed

1. **Return-code swallowing.** The earlier `label-rerun.yml` ended with a
   trailing `echo` after the helper call and relied on `set -uo pipefail`,
   so a helper failure was swallowed. The fresh-head recheck and rerun are
   now inline, and every failure path is an explicit `exit 1` (unreadable
   live head, failed `gh run rerun`). A superseded head is a successful
   no-op (`exit 0`), exactly as required: superseded means nothing to rerun,
   not an error.
2. **Helper bootstrap on the default branch.** The earlier helper was
   checked out from `repository.default_branch`, which on this fork is
   `feat/kanban-desktop-release-20260913`, not `main` where this PR merges,
   so the helper would be absent from default. The small fresh-head guard is
   now inline in the existing trusted `label-rerun` workflow, and the
   checkout step is gone entirely. No helper, no bootstrap dependency, and
   the `contents: read` permission was dropped with it (no checkout needs
   it).

## What changed

- `.github/workflows/label-rerun.yml`: the fresh-head recheck and rerun are
  inline in the run step. The `actions/checkout` step (which fetched the
  helper from the default branch) and the `contents: read` permission are
  removed. Errors propagate with explicit non-zero exits; a superseded head
  is a successful no-op.
- `.github/scripts/rerun_current_head.py`: deleted. The guard is inline and
  no longer needs a separate helper.
- `.github/scripts/evaluate_gate.py`: unchanged. The aggregate gate helper
  remains, consumed by `all-checks-pass` via `actions/checkout` of the PR
  merge ref (the same trust model as the pre-existing `detect` job).
- `.github/workflows/ci.yaml`: unchanged in this retry.
- `tests/ci/test_ci_rerun_current_head.py`: rewritten to extract and execute
  the ACTUAL `run` scripts from the final YAML at a mock `gh` command
  boundary, plus the gate cases against the real `evaluate_gate.py` helper.

## Trust model and parent correction

The label workflow checks out no separate helper. It remains triggered by
`pull_request`, whose revision is the PR merge ref. Therefore the workflow
itself can be changed by a PR; absence of a checkout is not proof that source
is trusted. GitHub's configured token policy and review of workflow changes
remain the authority boundary. This repair does not change that policy.
The aggregate helper is checked out from the same merge revision as the
existing detect job. Review labels are a review signal, not structural
containment or proof that arbitrary source is safe.

Source: [GitHub event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
Parent corrected the worker's stronger trust claim during acceptance. Parent
reran the exact final workflow regression suite on physical EVO: 17 passed.
Original baseline reproduction: 2 failing defects, 2 passing controls.
This proves the bounded subprocess behaviour, not a new live GitHub rerun race.

## RED / GREEN

RED: owned by the parent. Parent independently executed the original
workflow run-step scripts and confirmed the stale-head rerun and the
cancelled aggregate both fail correctly (2 RED), with success/failure
controls passing (2). Receipts live in
`../parent-controls/test_ci_original.py` and
`/tmp/relay-parent-ci-original-red.log` on Archie; this retry does not
reimplement that RED.

GREEN (fresh, this retry): `tests/ci/test_ci_rerun_current_head.py` passes
17/17 via the canonical runner, and 159/159 pass across `tests/ci/`. The
rerun tests execute the extracted workflow run script against a mock `gh`;
the gate tests execute the extracted `echo "$NEEDS" | python3
.github/scripts/evaluate_gate.py` step against the real helper.

## Behavioural controls (workflow-level)

- Positive (current rerun): an unchanged live head issues the rerun call
  (`rerun 42` recorded), exit 0.
- Stale no-op: a changed live head makes zero rerun calls and exits 0
  (successful superseded).
- Real gh failure (API): an unreadable live head exits non-zero and makes
  zero rerun calls.
- Rerun failure: a failed `gh run rerun` exits non-zero, never swallowed.
- Cancelled aggregate: a cancelled required job blocks the gate (non-zero).
- Failed aggregate: a failed required job blocks the gate (non-zero).
- Success/skipped policy: all-success and success+skipped both pass (exit 0).

## Limitations

- The gate step's helper is read from the PR merge ref. This is the same
  trust model as the pre-existing `detect` job and the original inline gate;
  it is not a new widening, and the `ci_review` lane still gates any
  `.github/` change behind the `ci-reviewed` label and human review.
- The review-comment poller (`scripts/ci/assemble_review_comment.py`
  `collect_failed_jobs`) still renders only `failure` jobs as error items;
  cancelled jobs now block the merge via the gate but are not listed as
  error items in the PR comment. Out of scope here.
- The mock `gh` tests exercise the extracted run script against a simulated
  command boundary, not against the live GitHub API. A live re-run remains
  the parent's call.

## ACTUALLY_USED

- `.github/workflows/label-rerun.yml`, `.github/workflows/ci.yaml`
- `.github/scripts/evaluate_gate.py`
- `tests/ci/test_ci_rerun_current_head.py`
- `.venv/bin/python` (Python 3.12.3, pytest 9.1.1, PyYAML)
- `scripts/run_tests.sh` (canonical runner), `HERMES_PYTHON` set to the
  workspace venv
- ISSUE60.md as the captured-evidence source

## DISCOVERED

- GitHub Actions runs `run:` steps under `bash -eo pipefail` by default, so
  the `-e` flag alone would mask the swallow bug in live CI; the parent's
  mock harness runs plain `bash -c` (no `-e`), which is exactly the mode
  that exposes it. The inline script is therefore made correct under BOTH
  modes by using explicit `exit` codes instead of relying on any shell flag.
- Removing the checkout step also lets the `contents: read` permission be
  dropped, narrowing the token surface beyond the minimum the parent asked
  for.

## UNTOUCHED

- `scripts/ci/assemble_review_comment.py` (comment poller rendering)
- `scripts/ci/live_comment.py`
- `scripts/ci/classify_changes.py`
- The `needs` list, concurrency groups, review-label thresholds and all
  other jobs in ci.yaml.
- `.github/workflows/ci.yaml` (unchanged this retry)
- ISSUE60.md, BRIEF.md, FOLLOWUP.md, ATLAS-ISSUE-SKILL.md (parent-supplied,
  left untracked)

## MISSING

- A live re-run of the workflow against the real GitHub API (parent owns
  this).
- Rendering cancelled jobs as error items in the PR review comment.
- Ruff, which is not installed in this `.venv`; the parent runs it.

## FRICTION

- The gate helper is consumed by `echo "$NEEDS" | python3
  .github/scripts/evaluate_gate.py`, so the workflow-level gate test must
  run the extracted step with `cwd` at the repo root or `python3` cannot
  resolve the relative helper path; this is baked into the test as an
  explicit `cwd=ROOT`.
- The terminal sandbox blocks `python3 -c` and pipe-to-interpreter, so
  standalone helper checks used a `.sh` script with heredoc input instead.

## Parent live-CI correction, 2026-09-13
The actual Label rerun run 34713311665 exposed an already-successful CI run: GitHub refuses --failed when there are no failed jobs. Parent reproduced two RED cases in the exact extracted workflow shell, then added an explicit completed-conclusion read. Successful/neutral/skipped runs stop successfully; unknown conclusions fail closed; failed runs retain fresh-head validation and loud rerun errors. Physical EVO verification: 19 tests passed, including the two new controls. This is parent mechanical completion after the worker same-profile retry, not a claim that the worker delivered these cases.
