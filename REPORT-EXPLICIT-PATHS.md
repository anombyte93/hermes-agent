# Reject missing explicitly-requested test paths

Scope: `scripts/run_tests_parallel.py` and `tests/test_run_tests_parallel.py`
on branch `fix/explicit-test-paths-20260913`, base
`f06addcc87f01bfb1d7232966dc85fe204410159`. No other files.

## The defect

`_discover_files` silently skipped a nonexistent root, so an explicit
selection of valid plus missing paths exited 0 having run only the valid
subset. A caller asking for `a.py b.py missing.py` got a green run over
`a.py` and `b.py` with no complaint about `missing.py`.

## The fix

Reject a missing explicitly-named path before any test child spawns:

- positional paths: always rejected when missing
- `--paths` entries: rejected when the flag or `HERMES_TEST_PATHS` is
  supplied (explicit), not the hardcoded default `tests`
- `--files` entries: always rejected when missing

Every invalid path is named on stderr and the runner exits 2 before
`_discover_files` or the thread pool is touched. Default discovery, the
integration/e2e/docker skip, and explicit opt-in of a skipped dir are
unchanged.

## RED (before the fix)

```
.venv/bin/python scripts/run_tests_parallel.py \
  tests/agent/test_billing_links.py \
  tests/agent/test_reactions.py \
  tests/gateway/test_kanban_block_reason.py -j 1 -q
```

`tests/gateway/test_kanban_block_reason.py` does not exist. Observed:

```
Discovered 2 test files (~6 tests) under ['tests/agent/test_billing_links.py', 'tests/agent/test_reactions.py', 'tests/gateway/test_kanban_block_reason.py']; running with -j 1
...
=== Summary: 2 files, 31 tests passed, 0 failed (100% complete) ...
EXIT_CODE=0
```

Three paths were requested; only two ran; the missing one was dropped
silently; the run reported success. This is the bug.

## GREEN (after the fix)

Same command:

```
.venv/bin/python scripts/run_tests_parallel.py \
  tests/agent/test_billing_links.py \
  tests/agent/test_reactions.py \
  tests/gateway/test_kanban_block_reason.py -j 1 -q
```

Observed:

```
error: 1 explicitly requested test path do not exist:
  - /home/hayden/atlas/work/trajectory-20260912/runner-explicit-paths/tests/gateway/test_kanban_block_reason.py
Refusing to run: every explicitly named path must exist. Fix or drop the path(s) above, then retry.
EXIT_CODE=2
```

The missing path is named, the exit is nonzero, and no test child ran (no
per-file output, no summary line). The valid files were never executed.

## Regression tests

Five tests added to `tests/test_run_tests_parallel.py`, all behaviour
tests over a synthetic temporary tree with a marker file:

- `test_missing_positional_path_rejected_before_any_test_runs`
- `test_missing_paths_entry_rejected_before_any_test_runs`
- `test_missing_files_entry_rejected_before_any_test_runs`
- `test_all_missing_paths_are_named`
- `test_explicit_skipped_dir_optin_still_runs`

The first three write a marker from the valid test and assert the marker
is absent after a rejected run, proving the valid file never executed
(rather than inspecting source strings). The fourth asserts every invalid
path is named. The fifth asserts an explicitly named `docker` dir still
opts in and its test actually runs.

Run via the canonical wrapper:

```
./scripts/run_tests.sh tests/test_run_tests_parallel.py -j 1
```

Result: `14 passed, 1 skipped, 0 failed`, exit 0. The skip is the
existing win32-only drive-letter test.

## Defaults unchanged

Default discovery still finds the full tree. Checked without running
tests:

```
.venv/bin/python scripts/run_tests_parallel.py --generate-slices 2
```

Emits the expected two-slice JSON matrix over the full `tests/` tree,
exit 0.

## Capability ingest

- ACTUALLY_USED: isolated EVO workspace `.venv` (pytest 9.1.1, Python
  3.12.3), `scripts/run_tests.sh` canonical wrapper, and the runner CLI
  (positional, `--paths`, `--files`, `--generate-slices`).
- DISCOVERED: the minimal `.venv` lacks `rich` and `requests`, so most
  real test files fail collection in this environment; only self-contained
  files (test_billing_links.py, test_reactions.py,
  test_raft_check_fn_silent.py) pass. `test_durations.json` is gitignored
  so runner invocations do not dirty the tree.
- UNTOUCHED: capability 1858 (EVO delegation) and 2131 (independent
  release review) were not exercised; this was a bare-hands fix. GitHub,
  integration, and release authority stayed with the parent. No workflow
  edits, global config, or live board operations.
- MISSING: none. The absent `rich`/`requests` extras were not needed for
  this change.

## Friction

Single-query mode refuses `python3 -c`, inline heredocs, and `rm -rf`; I
used file-based probes and `.venv/bin/python` directly instead. Choosing
RED "real files" needed care: files that import the CLI pull in `rich` and
`requests` and die at collection, which would have masked the exit-0
signal, so I picked self-contained passing files.
