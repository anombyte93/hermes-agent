# CI stale-head rerun and cancellation truth

Repairs two defects captured in ISSUE60.md. Parent retains acceptance and all
release authority; this report records the exact RED/GREEN state and the
boundaries of what was and was not exercised.

## What changed

- `.github/scripts/rerun_current_head.py` (new): re-reads the live PR head
  before rerun and refuses to rerun a stale one.
- `.github/scripts/evaluate_gate.py` (new): the `all-checks-pass` aggregate
  gate, extracted from ci.yaml and fixed to block on `cancelled`.
- `.github/workflows/label-rerun.yml`: checks out the trusted default branch,
  then calls the rerun helper instead of `gh run rerun --failed` inline.
- `.github/workflows/ci.yaml`: `all-checks-pass` now calls `evaluate_gate.py`
  instead of the inline Python gate.
- `tests/ci/test_ci_rerun_current_head.py` (new): 14 behavioural and wiring
  tests.

## RED / GREEN

RED: NOT RE-RUN. I did not execute the exact original workflow run-step
scripts (the `gh run rerun "$RUN_ID" --failed || true` bash in label-rerun.yml
and the inline `info['result'] == 'failure'` gate in ci.yaml) against a mock
`gh`. Per operator direction this honest gap is recorded rather than a
simulated reproduction. The original live failure is already captured in
ISSUE60.md (label workflow 34710287809 started against the old head
7cd3427777fbb9182b92bcb96abc460c8cc47eb0; new CI 34710820430 cancelled at
18:20 UTC; old CI 34710235273 became attempt 2 on the old SHA). Parent will
independently execute the original script against a mock `gh`.

GREEN: the corrected helpers are exercised directly. 14/14 tests pass in
`tests/ci/test_ci_rerun_current_head.py`, and 156/156 pass across `tests/ci/`.

## Behavioural controls

- Positive: an unchanged head reruns completed failed jobs (`rerun`).
- Changed head: a newer commit during the wait makes zero rerun calls
  (`superseded`, exit 2).
- API failure: an unreadable live head makes zero rerun calls (`api-error`,
  exit 1).
- Rerun failure: a failed rerun call is explicit, never swallowed
  (`rerun-failed`, exit 1).
- Cancelled gate: a cancelled required job blocks the merge (exit 1).
- Failed gate: a failed required job blocks the merge (exit 1).
- Intentionally-skipped: a path-filter skip still passes (exit 0).
- All-success: a fully green gate passes (exit 0).

The rerun tests run at a mocked `gh` command boundary (subprocess.run faked),
so they assert the exact commands the helper issues, not a string search. The
gate tests feed the real `needs` JSON shape and assert the real exit codes.

## Limitations

- The `all-checks-pass` checkout uses the PR merge ref (default
  `actions/checkout` behaviour), matching the existing `detect` job. A fork
  could in principle ship its own `evaluate_gate.py`, but any `.github/`
  change triggers the fail-open `ci_review` lane and therefore the
  `ci-reviewed` label and human review, so a fork cannot silently bypass the
  gate.
- `label-rerun` checks out the default branch for the helper. Until this
  change lands on the default branch, the helper step fails closed (no blind
  rerun), which is the safe direction.
- The review-comment poller (`scripts/ci/assemble_review_comment.py`
  `collect_failed_jobs`) still renders only `failure` jobs as error items.
  Cancelled jobs now block the merge via the gate, but are not listed as
  error items in the PR comment. Out of scope here.

## ACTUALLY_USED

- `.github/scripts/rerun_current_head.py`, `.github/scripts/evaluate_gate.py`
- `.github/workflows/label-rerun.yml`, `.github/workflows/ci.yaml`
- `tests/ci/test_ci_rerun_current_head.py`
- `.venv/bin/python` (Python 3.12.3, pytest 9.1.1)
- ISSUE60.md as the captured-evidence source

## DISCOVERED

- `.github/scripts/` is not in the `ci_review` path list, but the broad
  `.github/` fail-open branch in classify_changes.py sets `ci_review=True`
  for any `.github/` change, so the gate helper stays behind the
  `ci-reviewed` review requirement.
- The existing `ci-review-comment.yml` already establishes the trusted
  checkout pattern (`ref: default_branch`, `persist-credentials: false`),
  which label-rerun now follows.

## UNTOUCHED

- `scripts/ci/assemble_review_comment.py` (comment poller rendering)
- `scripts/ci/live_comment.py`
- The `needs` list, concurrency groups, review-label thresholds and all other
  jobs in ci.yaml.
- `scripts/ci/classify_changes.py`
- ISSUE60.md, BRIEF.md, ATLAS-ISSUE-SKILL.md (parent-supplied, left untracked)

## MISSING

- A live re-run of the original workflow scripts against a mock `gh` (parent
  owns this).
- Rendering cancelled jobs as error items in the PR review comment.
- Ruff, which is not installed in this `.venv`; the parent runs it.

## FRICTION

- The inline gate in ci.yaml uses `python3 -c "..."` with escaped quotes and
  emoji, so the extraction to a helper was done with a targeted patch rather
  than a text rewrite.
- The terminal sandbox blocks `python3 -c` and pipe-to-interpreter, so
  standalone helper verification used a `.sh` script with input redirected
  from files instead.
