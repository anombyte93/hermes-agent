# UI Bridge Types Report — t_a8f1ecec

Task: Finish typed response validation for the shared UI evidence bridge.
Branch `feat/kanban-evidence-ui-20260912`, base
`8fd40caf4bce34209125accd370414ff008dc41b` (final empty validation-receipt
commit; source unchanged from `211dd46`).
Worker: evo (deepseek-v4-pro), focused continuation of stopped worker
t_8bfaf14e. Parent: atlas-relay. Issue: anombyte93/hermes-agent#54.

## The located gap

`_evidence_pass_shape_error` in `plugins/kanban/dashboard/plugin_api.py`
validated only the PRESENCE of required `data` keys, not their TYPES. A
malformed helper boundary receipt could therefore slip through as PASS:
`snapshot.cards` as a string, `page.items` string / `returned` bool /
`has_more` string, `worker.observations` dict, `card.runs` null — all green.

## RED (reproduced before the fix)

`../parent-controls/verify-ui-malformed.py` (unmodified parent program,
driving the real HTTP router with the four malformed receipts):

```
snapshot: http 200, state PASS, expected UNKNOWN   # cards:string
page:     http 200, state PASS, expected UNKNOWN   # items:string, returned:bool, has_more:string
worker:   http 200, state PASS, expected UNKNOWN   # observations:dict
card:     http 200, state PASS, expected UNKNOWN   # runs:null
```

All four collapsed to PASS instead of UNKNOWN.

## GREEN (after fix, exact commands and results)

```
$ .venv/bin/python ../parent-controls/verify-ui-malformed.py
snapshot: http 200, state UNKNOWN
page:     http 200, state UNKNOWN
worker:   http 200, state UNKNOWN
card:     http 200, state UNKNOWN

$ .venv/bin/python ../parent-controls/verify-ui-bridge.py   # real helper positives
helper_positive: exit 0, state PASS, host evo
routes.card:     http 200, state PASS
routes.snapshot: http 200, state PASS
routes.page:     http 200, state PASS

$ .venv/bin/python -m pytest -q tests/plugins/test_kanban_dashboard_plugin.py tests/plugins/test_kanban_evidence_api.py
95 passed, 1 warning in 11.58s

$ .venv/bin/python -m ruff check plugins/kanban/dashboard/plugin_api.py tests/plugins/test_kanban_evidence_api.py
All checks passed!
```

## What was fixed

1. **Typed field validation, never coerce.** `_evidence_pass_shape_error`
   now delegates to a per-tool `_evidence_pass_type_error` after the
   key-presence check. Consumed types enforced:
   - `cards`/`items`/`observations`/`runs`/`comments`/`events` must be lists
     whose entries are all objects;
   - `snapshot.counts` must be an object (of per-status counts);
   - `page.returned` must be a non-negative integer (never a boolean) equal
     to `len(items)`;
   - `page.has_more` must be a boolean;
   - a page cursor (`cursor`/`next_cursor`) is absent, null, or a string;
   - `card.task` must be an object retaining its `id`.
   Optional forward-compatible fields are preserved untouched; a wrong type
   is a hard UNKNOWN, never a repaired value.
2. **Finite timestamps.** `observed_at` must be a finite number (never a
   boolean) at BOTH the top level of the receipt and inside
   `snapshot.data`. NaN and +/-Infinity are rejected by
   `_evidence_finite_number` (new `import math`) before FastAPI's JSON
   serialiser can emit non-standard `NaN`/`Infinity` tokens.
3. **No scope echo invented for page.** The helper emits no board/card echo
   for `kanban_page`; none is claimed. Existing board/card/status binding is
   unchanged.

## New controls

- `test_wrong_consumed_type_is_unknown` (parametrised, all four parent
  malformed examples) — each UNKNOWN with the offending field named in the
  reason and `evidence` null.
- `test_page_returned_mismatching_items_length_is_unknown` — returned 99 vs
  1 item.
- `test_nonfinite_top_observed_at_is_unknown` — NaN top-level observed_at.
- `test_nonfinite_snapshot_observed_at_is_unknown` — +Inf snapshot data
  observed_at.
- `test_correct_typed_shapes_still_pass` — correct-shaped positives stay
  green; snapshot `counts` (dict) and `observed_at` (float) forwarded
  untouched.

Fake helper gained `badtypes`, `badreturned`, `nonfinite_top`,
`nonfinite_snapshot` behaviours mirroring the parent's receipt shapes.

## Files changed (allowed list only)

- `plugins/kanban/dashboard/plugin_api.py`
- `tests/plugins/test_kanban_evidence_api.py`
- `docs/kanban-evidence-bridge.md`
- `UI-BRIDGE-TYPES-REPORT.md`

No renderer/core/auth/config/service/live-install changes, no browser/
credential/session access, no package installs, no process signals, no
push/merge, no children. The real helper candidate
(`../ui-helper-candidate/`, isolated MCP 1.x source 27ef127) was NOT changed;
the current Hermes MCP 2.x runtime stays separate.

## Explicitly unproved (parent-owned)

- Production dashboard auth middleware integration (parent tested 401/200
  separately).
- Both UI renderers and the live helper install pointer.

## ACTUALLY_USED

- Parent's exact verification programs `../parent-controls/verify-ui-malformed.py`
  and `../parent-controls/verify-ui-bridge.py` (after fix).
- Brief's test gate: `.venv/bin/python -m pytest -q
  tests/plugins/test_kanban_dashboard_plugin.py
  tests/plugins/test_kanban_evidence_api.py` — 95 passed.
- ruff on changed source/tests — clean.
- First fix committed immediately (`c72ae67`), then tests and this report.
