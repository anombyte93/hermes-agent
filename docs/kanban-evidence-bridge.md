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
- bounded JSON payload (`{"tool": ..., "args": ...}`) on stdin;
- the request can never select the executable, the argv or the tool name —
  the tool is a literal at each route, drawn from the fixed read allowlist
  `EVIDENCE_READ_TOOLS = {kanban_snapshot, kanban_page, kanban_worker,
  kanban_card}`. No write tool name appears in the module.

The helper is **not currently installed** on this EVO UI test host, so every
route returns `UNKNOWN` with reason "not installed" and the remedy naming
the parent-owned released installation. That is by design.

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
- **Bounded capture:** stdout disk-backed, capped at 1 MiB (over-cap →
  UNKNOWN); stderr capped at 16 KiB and never surfaced — its content is
  never logged or returned.
- **Timeout:** 75s local wall clock (helper's own remote cap is 60s);
  timeout → UNKNOWN with remedy.
- **Nonzero exit claiming PASS → UNKNOWN.** Exit status is checked before
  the receipt is trusted; a claimed FAIL/UNKNOWN from a nonzero exit keeps
  its state class with a short reason.
- **Receipt validation:** `state` must be PASS/FAIL/UNKNOWN; PASS receipts
  must carry `execution_host == "evo"` or the result is UNKNOWN.
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
