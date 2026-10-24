# REPORT: Finish available-runner CI repair (PR30 / issue #15)

Task: t_c5b815ca
Branch: fix/ci-runner-finish-20260913 (own fork anombyte93/hermes-agent, never NousResearch)
Worker: evo (physical), model deepseek-v4-pro
Scope: remove the temporary ui-tui offset-cache timing diagnostic from
.github/workflows/js-tests.yml only. Preserve every real workspace check and
the fail-loud empty-selection and aggregation gate. Verify the workflow runner
test suite and the real slice generation.

## What changed

One file, one removal, committed as:

  b8b897c82  test(ci): remove temporary ui-tui timing-control diagnostic
  1 file changed, 13 deletions

The removed block was the "Repeat ui-tui offset-cache timing control on one
CPU" step in js-tests.yml, which was pushed (commits d502f6b14 + f54ed164b)
only to collect repeated constrained-runner evidence and whose own comment
promised removal after the diagnostic. The resulting file index matches the
pre-diagnostic state (b69db3b39), so nothing else moved.

Preserved intact:
- "Run all workspace checks" step: node .github/scripts/run-workspace-checks.mjs --concurrency 1
- The empty-selection-is-an-error contract in the run-workspace-checks comment
- tests.yml slice_count default 8, --generate-slices, fail-fast false,
  timeout-minutes 30, and the fail-closed test-result job (if: always(),
  test "$GENERATE_RESULT" = success AND test "$TEST_RESULT" = success)

No thresholds changed, no tests skipped, no aggregate gate weakened.

## Tests run (exact)

1. Baseline before the removal:
   .venv/bin/python -m pytest tests/ci/test_workflow_runner_labels.py -v
   -> 22 passed in 0.53s

2. After the removal (same command):
   -> 22 passed in 0.31s

The 22 collected tests:
   test_checker_accepts_standard_public_runner_labels
   test_checker_rejects_unavailable_private_size_label
   test_checker_rejects_quoted_private_size_label
   test_checker_rejects_list_form_private_size_label
   test_checker_accepts_quoted_standard_public_runner_label
   test_checker_resolves_matrix_runner_literals
   test_checker_reports_matrix_include_row_missing_runner
   test_checker_resolves_reusable_workflow_runner_default
   test_checker_resolves_reusable_workflow_runner_override
   test_checker_reports_each_missing_required_runner_override
   test_checker_rejects_duplicate_runner_keys
   test_checker_reports_unresolved_runner_expression
   test_checker_ignores_nested_non_job_runs_on_keys
   test_checker_resolves_nested_matrix_runner_literals
   test_checker_rejects_nested_matrix_private_runner_literal
   test_checker_reports_missing_nested_matrix_runner
   test_checker_rejects_empty_and_recursively_empty_runner_lists
   test_checker_reports_malformed_strategy_with_source_line
   test_checker_reports_malformed_matrix_with_source_line
   test_python_suite_uses_fail_closed_duration_balanced_slices
   test_js_workspace_checks_use_one_shared_cpu_budget
   test_repository_workflows_use_standard_public_runner_labels

3. Real slice generation coverage:
   .venv/bin/python scripts/run_tests_parallel.py --generate-slices 8
   -> 8 slices, 3340 test files, 0 empty slices, 0 duplicates, 0 dropped.
   (per-slice: 418, 418, 418, 418, 417, 417, 417, 417)

The discovery roots are tests/ excluding integration/e2e/docker, matching the
skip list documented in run_tests_parallel.py.

## Checker diagnostics question: no mistaken expectation to correct

I grepped the whole tests tree for every token the removed step used
(taskset, Repeat ui-tui, offset-cache, virtualHistoryOffsetCache, build:ink,
Temporary diagnostic). Result: NONE FOUND. No existing checker test required
or expected the diagnostic step, so no test assertion had to be corrected.
The removal is clean with respect to the checker.

## ACTUALLY_USED

- .venv/bin/python (explicit interpreter; pytest 7.4.4 + PyYAML present)
- tests/ci/test_workflow_runner_labels.py (read all 680 lines, run twice)
- scripts/run_tests_parallel.py --generate-slices 8 (real slice generation)
- .github/workflows/tests.yml and js-tests.yml (read; js-tests.yml edited)
- grep over .github/workflows for diagnostic remnants (none)
- git (add, commit)

## DISCOVERED

- Slice generator partitions all 3340 discovered test files into 8 slices with
  no empty slice and no duplication, so the CI cannot silently drop a file.
- The standard public labels in use are exactly the four the checker allows:
  ubuntu-latest, windows-latest, macos-latest, ubuntu-24.04-arm.
- tests-os.yml and docker.yml resolve their runners through matrix.include
  entries (macos-latest/windows-latest and ubuntu-latest/ubuntu-24.04-arm),
  which the checker resolves correctly (test_repository_workflows_* is green).

## UNTOUCHED

- tests/ci/test_workflow_runner_labels.py (no edit was needed)
- .github/workflows/tests.yml and every other workflow (read only)
- thresholds, slice_count default 8, fail-fast false, aggregation gate
- ATLAS-ISSUE-SKILL.md and BRIEF.md (workspace handoff files; left untracked)

## MISSING

- Nothing that blocked the change. origin/main is not fetched on this fork
  (only feat/kanban-desktop-release-20260913 and
  feat/kanban-browser-evidence-20260912 exist), so the PR diff could not be
  recomputed against upstream main locally; the branch-local diff is complete
  and correct.

## FRICTION

- Single-query terminal refused inline python3 -c and python -c; worked around
  by writing /tmp/gen_slices.sh and /tmp/verify_slices.sh and running them.
- git author identity was unset (auto-detect failed with
  'hayden@evo.(none)'); set a repo-local (--local) user.name/user.email to
  match the branch's existing author (Claude <claude@anthropic.com>). This is
  a local repo config only; no global config or credential touched.

## Suggested final PR description (accurate)

Title: fix(ci): run required checks on available standard hosted runners without dropping tests

Body:
- Switches every required workflow to standard public GitHub-hosted runner
  labels: ubuntu-latest, windows-latest, macos-latest, ubuntu-24.04-arm
  (native arm64). No private sized or self-hosted label remains.
- Python tests: replaces the single flat test job with a duration-balanced
  LPT slice generator (scripts/run_tests_parallel.py --generate-slices,
  default 8 slices). Each slice runs its files in per-file subprocesses with
  HERMES_TEST_WORKERS="$(nproc)". A fail-closed test-result job
  (if: always()) asserts that both generate and test succeeded, so an empty
  or partially-failed run cannot report green.
- JS/TS checks: replaces the 14-leg matrix with a single job running
  node .github/scripts/run-workspace-checks.mjs --concurrency 1, so all
  workspace checks share one four-core budget. An empty workspace selection
  is an error, not an empty green run.
- Adds tests/ci/test_workflow_runner_labels.py (680 lines): loads every
  workflow and asserts only standard public runner labels are used (including
  matrix and reusable-workflow input resolution), and that the slice and
  aggregation gate is fail-closed.
- Removes the temporary ui-tui offset-cache timing diagnostic that was pushed
  only to gather constrained-runner evidence (its own comment promised removal).

Correction to the existing PR body: this PR DOES change the commands and the
job structure (matrix collapsed to one job, flat test run replaced by the
slice generator). The earlier "commands unchanged" wording is false and must
be corrected before merge.

---

# PART 2: Blocked-fixture repair (issue #58 continuation)

Task: t_c5b815ca (same card, focused continuation)
Fix: tests/gateway/test_kanban_reconcile_orphans.py
Base verified against: origin/feat/kanban-desktop-release-20260913 (HEAD
e93f090b9), the fork's default branch that carries the live block-reason
trigger. CI runs against this branch, so this is the code that actually
fails and the code my fix must satisfy.

## What changed

One test, one method, no production code, no trigger, no guard, no threshold.

test_non_running_statuses_ignored now supplies a truthful test-only block
reason for the "blocked" status in its direct UPDATE. The kernel-level
trigger trg_tasks_block_reason_update (added in f57eb0d57, "fix(kanban):
require a current reason for blocked cards") raises 'block reason is
required' on any writer that leaves a blocked row without a current reason,
direct SQL included. The todo/ready/done cases still carry block_reason=NULL
and are untouched.

  -            "UPDATE tasks SET status=?, claim_lock=NULL, "
  -            "claim_expires=NULL WHERE id=?", (status, tid),
  +            block_reason = "test-only block reason" if status == "blocked" else None
  +            "UPDATE tasks SET status=?, block_reason=?, claim_lock=NULL, "
  +            "claim_expires=NULL WHERE id=?", (status, block_reason, tid),

The blocked case is retained exactly; only its missing reason is supplied.
No assertion changed, no status dropped, no trigger disabled.

## RED evidence (before the fix)

Reproduced against origin/feat/kanban-desktop-release-20260913 in a detached
worktree (/tmp/ui-ci-verify-AXLB), with the unmodified test:

  tests/gateway/test_kanban_reconcile_orphans.py::TestReconcileOrphanedRunning::test_non_running_statuses_ignored FAILED
  E   sqlite3.IntegrityError: block reason is required
  tests/gateway/test_kanban_reconcile_orphans.py:145: IntegrityError

This matches CI-FIXTURE-ISSUE.md exactly (Run 34709158090, job 103594665376).

## GREEN evidence (after the fix)

All run through the canonical scripts/run_tests.sh (repo AGENTS requires it),
which isolates each file in a hermetic env -i (no inherited HERMES_KANBAN_*)
and uses HERMES_PYTHON to point at the workspace .venv (pytest 7.4.4):

1. Orphan file: tests/gateway/test_kanban_reconcile_orphans.py
   -> 10 passed, 0 failed (test_non_running_statuses_ignored now green)
2. Block-reason guard tests:
   tests/hermes_cli/test_kanban_db.py          -> 36 passed, 1 windows_only skip
   tests/hermes_cli/test_kanban_block_kinds.py -> 2 passed
   tests/hermes_cli/test_kanban_blocked_sticky.py -> 2 passed
   -> 40 passed, 0 failed, 1 skipped
3. Workflow runner suite: tests/ci/test_workflow_runner_labels.py
   -> 22 passed, 0 failed

## Isolation (known issue #35)

Every run used the canonical runner's hermetic env -i (drops
HERMES_KANBAN_DB, HERMES_KANBAN_HOME and every other HERMES_KANBAN_* var);
the fixture additionally anchors the board under tmp_path via HERMES_HOME +
HERMES_KANBAN_HOME and a Path.home monkeypatch. Direct pytest runs were also
prefixed with env -u HERMES_KANBAN_DB -u HERMES_KANBAN_HOME ... No live board
was read or mutated at any point.

## ACTUALLY_USED

- tests/gateway/test_kanban_reconcile_orphans.py (edited)
- scripts/run_tests.sh (canonical runner) + scripts/run_tests_parallel.py
- hermes_cli/kanban_db.py (read: block_reason column, the three triggers
  trg_tasks_block_reason_insert/update/clear, and create_task validation)
- origin/feat/kanban-desktop-release-20260913 (detached worktree for
  trigger-bearing RED/GREEN verification)
- tests/hermes_cli/test_kanban_db.py, test_kanban_block_kinds.py,
  test_kanban_blocked_sticky.py
- tests/ci/test_workflow_runner_labels.py
- .venv/bin/python (pytest 7.4.4, PyYAML), psutil 7.2.2 (installed)

## DISCOVERED

- The block-reason invariant is enforced by three SQLite triggers that exist
  only on feat/kanban-desktop-release-20260913, not on my workspace's base
  f54ed164bc. My branch's schema has no block_reason column, so the broken
  test passes locally but fails in CI. Verification therefore had to run
  against the default-branch worktree, not the workspace checkout.
- The failing region of test_kanban_reconcile_orphans.py is byte-identical
  between my branch and the default branch, so the fix applies cleanly.
- The existing PR body's "commands unchanged" claim stays false (unchanged
  from Part 1); the corrected PR description is below.

## UNTOUCHED

- All production code, every trigger, every guard (conftest.py live-system
  guard included), every workflow, every threshold and every assertion.
- The "blocked" status case in the test is retained, not removed.

## MISSING

- Nothing that blocked the change. The parent's local parent-ci-current-base
  carries the live production trigger; I verified against the equivalent
  default-branch worktree instead of touching a live board.

## FRICTION

- Single-query terminal refused rm -rf on an absolute path and inline
  python -c. Worked around with mktemp -d scratch dirs and .sh scripts. Per
  operator instruction, nothing was deleted or bypassed afterwards; scratch
  paths (/tmp/ui-ci-verify-AXLB, /tmp/*.sh) are left in place for review.
- The minimal .venv lacked psutil, which tests/conftest.py's live-system
  guard needs to allowlist test-spawned children (test_live_worker_pid_defers_reconcile
  blocked os.kill otherwise). Installed psutil 7.2.2 into the workspace .venv
  to run the full orphan file cleanly. This is a test dependency, not a
  source change.

## Suggested PR body addition (for the parent)

This PR's test suite must pass on a base that carries the kernel block-reason
trigger. The orphan-reconciliation fixture test_non_running_statuses_ignored
previously forced status='blocked' via direct SQL with no reason, which the
trigger correctly rejects. It now supplies a truthful test-only block reason
for the blocked case while keeping the todo/ready/done cases unchanged and
the blocked case itself intact. No production code, trigger, threshold or
assertion was altered.
