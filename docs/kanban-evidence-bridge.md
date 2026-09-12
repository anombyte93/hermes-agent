# Kanban Evidence Bridge (read-only REST)

Status: preparatory bridge, implemented 2026-09-12 on branch
`feat/kanban-evidence-ui-20260912` (card `t_1b7a5c96`, parent `atlas-relay`).
The bridge is **not** a claim that either Kanban UI is delivered; the parent
integrates the reviewed contracts and both consumers later.

## What this adds

Four **GET-only, read-only** routes on the existing kanban dashboard plugin
router (`plugins/kanban/dashboard/plugin_api.py`), mounted in production at
`/api/plugins/kanban/evidence/...` behind the dashboard's session-token
`auth_middleware` (same gate as every other `/api/` path; the plugin-bypass
does not exempt them):

| Route | Helper tool (fixed) | Notes |
|---|---|---|
| `/evidence/snapshot` | `kanban_snapshot` | `board` required; `status` = `all` or a valid status; `card_limit` 1..200 (default 100); `cursor` optional ≤512 chars |
| `/evidence/page` | `kanban_page` | `resource` = `cards`/`runs`/`events`/`attachments`; `card` required except for `cards`; `limit` 1..200; `cursor` ≤512 |
| `/evidence/worker` | `kanban_worker` | explicit `board` + `card` |
| `/evidence/card` | `kanban_card` | `include_body=false` and `recent_items=10` are FIXED, not request-selectable |

## Response envelope

Every response is HTTP 200 with a state envelope (client errors for
malformed input are 400/422 before any subprocess runs):

```json
{
  "state": "PASS | FAIL | UNKNOWN",
  "evidence": { "...": "receipt data on PASS, else null" },
  "reason": "short reason for non-PASS",
  "remedy": "what to do about it, where applicable",
  "board": "slug",
  "helper": {"tool": "kanban_snapshot", "execution_host": "evo"},
  "request": {"board": "...", "status": "...", "card_limit": 50},
  "timing": {"helper_roundtrip_ms": 12.3, "collection_ms": 0.1}
}
```

Board binding is visible in three places (`board`, `request.board`, and the
helper payload's `args.board`). Helper round-trip timing is surfaced
separately from local collection time.

## Dependency decision (verified upstream, do not revisit)

Hermes runs MCP 2.0.0; AtlasKanban is an MCP 1.x runtime. The helper is
**never imported** into the Hermes runtime — no pip installs, no
`sys.path` injection, no SDK pin changes. The bridge invokes the RELEASED
`atlas-kanban-call` executable:

- resolved with `shutil.which("atlas-kanban-call")` per invocation;
- executed without a shell as `[<absolute path>, <fixed tool>, "-"]`;
  the tool name arrives **only in argv** and stdin carries **only the flat
  JSON args object** (`{"board": ..., ...}`). The released helper rejects
  the nested `{"tool": ..., "args": ...}` envelope this bridge originally
  sent — the original fake accepted it, which is why fake-only tests
  never caught the divergence (repaired 2026-09-12, card `t_8bfaf14e`,
  verified against candidate source 27ef127);
- the request can never select the executable, the argv or the tool name —
  the tool is a literal at each route, drawn from the fixed read allowlist
  `EVIDENCE_READ_TOOLS = {kanban_snapshot, kanban_page, kanban_worker,
  kanban_card}`. No write tool name appears in the module.

The helper was **not installed** on this host during the original build, so
routes returned `UNKNOWN` ("not installed") by design. A released candidate
(source 27ef127, isolated MCP 1.x venv at `../ui-helper-candidate/`) is now
staged for verification; the live install pointer remains parent-owned. On
hosts without the helper the routes still return `UNKNOWN` with the
"not installed" reason.

## Hardening

- **Pure slug validation** for `board` (`kanban_db._normalize_board_slug`
  only — never `_resolve_board`, which 404s against the local DB). The
  EVO-owned board need not exist locally. Card ids must match
  `^t_[0-9a-f]{8,32}$`.
- **No local DB fallback.** No evidence route opens a local kanban
  connection; unknown routes and incomplete evidence never fall back to SQL.
- **One HTTP request = exactly one helper invocation** (never per-card
  subprocesses). The subprocess runs off the event loop via
  `asyncio.to_thread`, so a 75s round trip never blocks the dashboard.
- **Bounded capture (read/parse bounds, not disk-write caps):** stdout is
  captured to a temporary file and at most 1 MiB is read back after child
  exit (over-cap → UNKNOWN); while the child runs it may write more than
  that to the temporary file — the cap is enforced on the read-back, not
  on the child's writes. The retained stderr copy is truncated to 16 KiB
  after exit and its content is never logged or returned. Both temporary
  files are closed (unlinked) in `finally`.
- **Timeout:** 75s local wall clock (helper's own remote cap is 60s);
  timeout → UNKNOWN with remedy.
- **Nonzero exit claiming PASS → UNKNOWN.** Exit status is checked before
  the receipt is trusted.
- **FAIL reasons survive exit 1.** The released helper exits 1 on ordinary
  FAIL outcomes (missing board, missing card); the receipt's safe reason is
  preserved (annotated with the exit code) so "board absent" stays
  distinguishable from "helper unavailable". Raw stdout/stderr are never
  forwarded.
- **Receipt validation:** `state` must be PASS/FAIL/UNKNOWN. A PASS receipt
  must carry `execution_host == "evo"`, a numeric `observed_at`, the
  per-tool required `data` shape (only fields the adapter actually emits),
  and — wherever the receipt echoes scope — a board/card match with the
  request (`kanban_snapshot` → `data.board` + `status_filter`,
  `kanban_card` → `data.task.id`, `kanban_worker` → `data.task_id`;
  `kanban_page` emits no scope echo and none is claimed). Any violation is
  UNKNOWN, never PASS.
- **Envelope honesty:** `execution_host` is forwarded only when validated
  from a PASS receipt; every other response reports `"unverified"` — the
  bridge never stamps the host as its own observation. Validated responses
  preserve the receipt's `observed_at` and its limitations block
  (`bounded` for card, `data.omitted` rollup otherwise); completeness
  fields (`has_more` / `next_cursor` / `incomplete`) stay inside
  `evidence`.
- **Child env:** `ATLAS_KANBAN_WRITE_BOARDS` is forced empty in the child
  regardless of what this process was granted; the rest of the runtime env
  is inherited unchanged (never printed).

## Explicit limits

- The tests drive a real FastAPI router + real temporary fake executable on
  PATH; they do **not** prove production auth (no live token handshake
  against a booted dashboard). The auth integration check in the test file
  asserts the production `auth_middleware`'s public-path allowlist does not
  exempt `/api/plugins/kanban/evidence/*` and that the `/api/` gate covers
  them.
- Process-observation claims cover the subprocess boundary only; no single
  SQLite transaction spans them.
