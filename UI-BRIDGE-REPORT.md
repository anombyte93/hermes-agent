# UI Bridge Report — t_1b7a5c96 (EVO, GLM-5.3 via zai, 2026-09-12)

## What was built

Read-only `/evidence/*` REST bridge on the kanban dashboard plugin
(`plugins/kanban/dashboard/plugin_api.py`, +~440 lines appended): four
GET-only routes (`snapshot`, `page`, `worker`, `card`) proxying to the
RELEASED `atlas-kanban-call` executable — resolved via `shutil.which`,
invoked shell-less as `[helper, <fixed tool>, "-"]`, bounded JSON stdin,
one invocation per HTTP request, off the event loop via `asyncio.to_thread`.

Test file `tests/plugins/test_kanban_evidence_api.py` (32 tests): real
FastAPI router + real temporary fake executable on PATH (writes an
invocation record with argv/stdin/env). Covers PASS forwarding, FAIL /
UNKNOWN receipts, wrong execution host, malformed stdout, missing receipt
keys, oversized stdout (>1 MiB cap), nonzero-exit-claiming-PASS, timeout
(boundary injected: timeout constant monkeypatched to 0.3s, helper sleeps
600s), input validation preventing invocation (bad board slug, missing
board, bad status, card_limit bounds, cursor length, card pattern, bad
resource, card-required-except-cards), missing helper → UNKNOWN on all four
routes, no local DB creation, no `current` pointer mutation, empty child
`ATLAS_KANBAN_WRITE_BOARDS` even when the parent grants write authority,
fixed read-tool allowlist, GET-only route surface, and the production-auth
coverage check (evidence paths not in `web_server._PUBLIC_API_PATHS`, the
`/api/` token gate covers them).

Docs: `docs/kanban-evidence-bridge.md` (contract + hardening + limits).

## Commands and results (all actually run)

- `.venv/bin/python -m pytest -q tests/plugins/test_kanban_dashboard_plugin.py tests/plugins/test_kanban_evidence_api.py`
  → **73 passed** (41 baseline + 32 new), 1 pre-existing warning, exit 0.
- `.venv/bin/python -m ruff check tests/plugins/test_kanban_evidence_api.py` → clean.
- `.venv/bin/python -m ruff check plugins/kanban/dashboard/plugin_api.py` → clean
  (note: the brief said plugin_api carries existing lint debt; at this base
  it was already clean, and remains clean after the append).
- Baseline re-verified before any edit: 41 passed on the untouched tree.
- `which atlas-kanban-call` → not found (exit 1): consistent with the brief
  ("NOT currently installed on this EVO UI test workspace"); missing-helper
  → UNKNOWN is exercised by tests.

Commits on `feat/kanban-evidence-ui-20260912` (base 4048995):
- 24c20d1 evidence bridge routes
- 287fb61 evidence bridge tests
- (this report + docs commit follows)

## Limits (explicit)

- Bare-FastAPI tests do NOT prove production auth: no live dashboard was
  booted and no token handshake was exercised. The real-webserver check is
  the static coverage assertion described above (allowlist + gate source).
  Per the brief, this limit is stated rather than papered over.
- The helper is absent on this host, so no live receipt from the real
  `atlas-kanban-call` was observed; the receipt contract
  (`state`/`execution_host`/`data`/`reason`) is what the fake encodes from
  the brief.
- Process observations cover the subprocess boundary only; no claim that one
  SQLite transaction spans them (none is opened at all).
- Bridge ≠ UI delivery. No renderer/adapter files touched; no live UI proof
  is claimed or fabricated.

## FRICTION

- `git commit` initially failed: no `user.name`/`user.email` configured in
  this workspace (fresh worktree). Fixed with repo-local `git config`
  (evo / evo@local). No global config touched.
- Pyright flags cosmetic typing nits in the new test file (pytest import
  resolution, attribute assignment on TestClient). Ruff — the briefed
  linter — is clean; nits left as-is.

## ACTUALLY_USED / DISCOVERED / MISSING

- ACTUALLY_USED: `.venv` (Python 3.11, pytest 9.1.1, ruff) exactly as
  briefed; `hermes_cli.kanban_db._normalize_board_slug` for pure slug
  validation; `kanban_db.VALID_STATUSES`; the dashboard plugin's existing
  router/auth conventions (module docstring; `_PUBLIC_API_PATHS` gate).
- DISCOVERED: `web_server._PUBLIC_API_PATHS` + `auth_middleware`'s
  `/api/`-prefix token gate is the precise production seam covering plugin
  routes (used for the auth-coverage test instead of broader setup);
  `_normalize_board_slug` accepts mixed case and normalises (so
  "Evo-Alpha" → "evo-alpha" is correct behaviour, not a validation failure).
- MISSING: the released `atlas-kanban-call` executable (parent owns final
  installation); live UI journey proof (parent owns); real MCP receipt
  sample (parent owns verification).

Finish state: normal block `needs_input` — "Awaiting parent acceptance",
per brief. No children spawned, no push/merge, no files outside the
workspace, no renderer/adapter/core/auth-config edits.
