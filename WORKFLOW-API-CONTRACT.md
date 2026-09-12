# Workflow API contract (K2 readiness + K5 continuation/hold + K6/K7/K8 projections)

Owner: API worker t_0811e161 on EVO. Published for the React and browser UI
workers consuming WORKFLOW-UI-CONTRACT.md. Unproved until the parent runs
actual helper + authenticated API integration; helpers and routes match the
captured source in ADAPTER-SERVER.py / ADAPTER-CONTINUATION.py and the live
receipts in ADAPTER-LIVE.json.

Every route is mounted under `/api/plugins/kanban` behind the existing
dashboard session-token auth middleware. There is no new credential route and
no auth bypass. Every workflow read/write requires current
`/evidence/context` alignment with the selected EVO board (hostname `evo` plus
the canonical board DB path); unaligned requests are refused without invoking
the helper.

Helper boundary: each route invokes the released `atlas-kanban-call` helper
exactly once, as `[<helper>, <fixed tool>, "-"]` with the flat JSON args
object on stdin (no nested `{tool, args}` envelope, no shell, no
request-selectable executable/argv/tool).

## Envelope

All routes return HTTP 200 with a JSON envelope unless a request field is
malformed (400/422, see "Validation errors"). The envelope is:

```json
{
  "state": "PASS" | "FAIL" | "UNKNOWN",
  "board": "<slug>",
  "card": "<card>",              // workflow routes only
  "evidence": <payload | null>,
  "reason": "<string>",          // present on FAIL/UNKNOWN and helper reasons
  "remedy": "<string>",          // present when a next step is actionable
  "helper": {"tool": "<tool>", "execution_host": "evo" | "unverified"},
  "request": {<echo of the request>},
  "timing": {"helper_roundtrip_ms": <float|null>, "collection_ms": <float|null>},
  "observed_at": <float|null>
}
```

- `evidence` is null for every FAIL/UNKNOWN except readiness, which forwards
  its `checks` so the UI can show which check failed.
- `helper.execution_host` is `"evo"` only when a PASS receipt's own
  `execution_host` was validated against the EVO host; otherwise `"unverified"`.
- The helper's stderr/argv/environment are never echoed.

## Evidence projections (GET)

These are read-only and always run with `ATLAS_KANBAN_WRITE_BOARDS` forced
empty in the helper child, so they can never write. `evidence` is the helper's
`data` object (data-wrapped projection). `limit` is 1..200, `cursor` at most
512 chars.

### GET /evidence/attention?board=...&limit=50&cursor=...

Tool `kanban_attention`. `evidence`:

```json
{
  "board": "<slug>",
  "read_at": <float>,
  "observed_at": <float>,
  "freshness": {"read_at": <float>, "note": "<string>"},
  "cards": [
    {"id", "title", "assignee", "status", "block_reason", "block_kind",
     "current_run_id", "created_at", "started_at", "completed_at",
     "reasons": ["..."], "next_action": "<string>",
     "operator_authority_needed": null, "running_total": <int>,
     "running_truncated": <bool>, "process_state": "<string>",
     "process_present": <bool|null>}
  ],
  "returned": <int>,            // == len(cards)
  "has_more": <bool>,
  "next_cursor": "<string|null>",
  "omitted": <int>,
  "remaining_after_page": <int>,
  "high_water_rowid": <int>,
  "running_verification": {"selected", "checked", "absent", "healthy", "unexamined", "truncated"},
  "process_checks": {"performed", "capped", "cap", "note"},
  "bounded": {"limit", "max_limit", "process_check_cap", "runs_per_card_cap"},
  "incomplete": <bool>
}
```

`operator_authority_needed` is null (unknown) by default; never infer operator
authority from a generic blocked status. This projection never approves,
unblocks or mutates anything.

### GET /evidence/changes?board=...&limit=50&cursor=...

Tool `kanban_changes`. First read (no cursor) is baseline-now and returns no
history. `evidence`:

```json
{
  "board": "<slug>",
  "read_at": <float>,
  "observed_at": <float>,
  "freshness": {"read_at": <float>, "note": "<string>"},
  "first_read_policy": "baseline-now: no historical events returned",
  "events": [{"id", "task_id", "kind", "created_at", "run_id"}],
  "returned": <int>,            // == len(events)
  "has_more": <bool>,
  "next_cursor": "<string|null>",
  "total_events": <int>,
  "baseline_id": <int>,
  "high_water_rowid": <int>,
  "anchor_state": "baseline" | "...",
  "incomplete": <bool>,
  "empty_means_no_observed_changes": true,
  "id_gap_note": "<string>"
}
```

An empty page with a fresh timestamp means no observed changes, not a dead
worker. The cursor is a resume position, never mutation authority.

### GET /evidence/timeline?board=...&card=...&limit=50&cursor=...

Tool `kanban_timeline`. `evidence`:

```json
{
  "board": "<slug>",
  "card": "<card>",
  "card_status": "<status>",
  "read_at": <float>,
  "observed_at": <float>,
  "freshness": {"read_at": <float>, "note": "<string>"},
  "boundary": {"read_at": <float>, "clamped_at": <float>, "note": "<string>"},
  "intervals": [
    {"kind": "execution|blocked|review|unknown", "start": <float>,
     "end": <float>, "duration_seconds": <int>,
     "source_runs": [<int>], "source_events": [<int>]}
  ],
  "returned": <int>,            // == len(intervals)
  "has_more": <bool>,
  "next_cursor": "<string|null>",
  "totals": {"covered_window": {"execution_seconds", "blocked_seconds", "review_wait_seconds", "unknown_seconds"}, "page": {...}, "all_time": null},
  "coverage": {"window_start": <float>, "window_end": <float>, "gaps": [], "note": "<string>"},
  "incomplete": <bool>
}
```

Durations are wall-clock observation, never productivity, cost or savings.
Covered-window totals never claim all-time.

## Workflow routes (POST)

Board arrives as a query param; the card and payload arrive in the JSON body.
`extra=forbid`: an unknown body field is a 422 and nothing is invoked.
The helper child inherits the real `ATLAS_KANBAN_WRITE_BOARDS` (never force-
emptied, never extended); write routes additionally refuse before the helper
unless the board is aligned AND named in that setting.

### POST /workflow/readiness?board=...  body {card, check_model}

Tool `kanban_readiness`. The API resolves workspace/profile/provider/model from
the aligned local task, derives the current git HEAD with a fixed argument
list inside that workspace, and uses `<workspace>/.venv/bin/python`. It also
derives the card's real parent ids from the aligned board's `task_links`
(bounded, one query; no recursive traversal) and passes them as `parents` so
dependency completion is judged against the actual graph, never a hardcoded
empty list. The caller never supplies an executable, shell, path or revision.
Model proof runs only when `check_model` is true (explicit action, never a
polling loop).

Body:

```json
{"card": "t_<hex>", "check_model": false}
```

On success the root helper receipt is forwarded as `evidence`:

```json
{
  "state": "PASS" | "FAIL",
  "requested": {"board", "profile", "provider", "model", "workspace", "expected_revision", "parents": [], "check_model", "python", "minimum_python", "require_modules", "context_files"},
  "observed_at": <float>,
  "freshness": {"checked_at": <float>, "stale_after": <float>, "note": "<string>"},
  "ready_to_release": <bool>,      // true only when ALL checks PASS
  "checks": [
    {"name": "board_permission|profile_exists|workspace_exists|expected_revision|python_interpreter|required_modules|context_files|ram_available|parents|model",
     "state": "PASS|FAIL|UNKNOWN", "reason": "<string>", "..."}
  ],
  "boundary": "<string>"
}
```

A board-permission FAIL is NOT readiness: it reports the server's write scope
without granting it. Never auto-release on `ready_to_release`. Missing or
unsupported configuration returns `state=UNKNOWN` with `reason` naming the
missing field and a `remedy`; nothing is invented.

The bridge validates every readiness receipt before forwarding it (a
contradictory receipt is `state=UNKNOWN`, never a coerced PASS):

- `requested` identity must bind board/profile/provider/model/workspace/
  `expected_revision`/`python`/`check_model`/`parents` to the exact args sent.
- checks must be uniquely named with strict `PASS`/`FAIL`/`UNKNOWN` states.
- `freshness.checked_at`/`stale_after` must be finite with a non-inverted
  horizon.
- `ready_to_release` must be false when any check is FAIL/UNKNOWN/missing or
  when the model proof was skipped (`check_model=false`).

The released helper exits 1 for a legitimate structured FAIL (e.g. a denied
board); the bridge still forwards that receipt's `checks` and only rejects a
`PASS` that contradicts a nonzero exit. An unreadable or overbound dependency
graph is `state=UNKNOWN`, never silently `parents=[]`.

### POST /workflow/continuation-draft?board=...  body {...}

Tool `kanban_continuation_draft`. Read-only. Explicit commission fields win;
omitted fields derive from the aligned local task (present fields only, never
invented).

Body:

```json
{
  "card": "t_<hex>",
  "passed_checks": ["<string>", ...],
  "remaining_checks": [{"check": "<string>", "evidence": "<string>", "acceptance": "<string>"}],
  "verification_note": "<string>",
  "workspace": "<abs path|null>",
  "profile": "<string|null>",
  "provider": "<string|null>",
  "model": "<string|null>",
  "max_runtime_minutes": <int>,     // default 60
  "creator": "<string|null>",
  "title": "<string|null>"
}
```

On success `evidence` is the root draft receipt:

```json
{
  "state": "PASS",
  "board": "<slug>",
  "card": "<card>",
  "fingerprint": "<64-hex sha256>",
  "original": {"status", "assignee", "result_excerpt", "latest_run_summary_excerpt", "source"},
  "worker": {"verdict": "STOPPED|NO_WORKERS|...", "reason": "<string>", "aggregate": {...}},
  "passed_checks": ["<string>"],
  "remaining_checks": [{"check", "evidence", "acceptance"}],
  "verification_note": "<string>",
  "commission": {"workspace", "profile", "provider", "model", "max_runtime_minutes", "creator", "title"},
  "read_at": {"original": <float>, "worker": <float>, "note": "<string>"},
  "mutation_authorized": <bool>,
  "limitation": "<string>",
  "no_mutation_performed": true
}
```

The fingerprint is an optimistic concurrency check, not signed authority.
`original.result_excerpt` / `latest_run_summary_excerpt` are unverified,
truncated, redacted source excerpts.

### POST /workflow/continue?board=...  body {...}

Tool `kanban_continue` (write). Same body as continuation-draft plus the
`fingerprint` from the draft. This is a separate user click after reviewing
the draft. Requires the board in `ATLAS_KANBAN_WRITE_BOARDS` plus alignment;
an empty or non-matching scope is refused before the helper (visible
permission gap, never populated). Creates a NEW assigned+blocked card; the
original is never rewritten and never becomes a dependency. Same fingerprint
and intent returns the same new card id.

On success `evidence`:

```json
{
  "state": "PASS",
  "board": "<slug>",
  "original_card": "<card>",
  "new_card": "t_<hex>",
  "new_card_status": "blocked",
  "new_card_assignee": "<profile>",
  "held": true,
  "released_after_previous_creation": false,
  "reblocked_after_release": false,
  "origin": {"board", "card", "original_status", "note"},
  "worker": {"verdict": "<string>", "reason": "<string>"},
  "no_original_mutation": true
}
```

`held` must be true: no unblock or dispatch ever occurs here. The bridge also
requires `no_original_mutation=true` and `new_card_status` in `blocked`/
`triage` (a held state); a receipt claiming otherwise is `state=UNKNOWN`. On
UNKNOWN, the UI must tell the user to inspect before retrying; on a stale
fingerprint (`fingerprint_mismatch`) require a re-draft. Never silently retry
with a new key.

### POST /workflow/hold?board=...  body {card, reason}

Tool `kanban_hold` (write). Requires the board in `ATLAS_KANBAN_WRITE_BOARDS`
plus alignment. Durably blocks a ready/running/review card while preserving
its profile; it does NOT stop a live worker. A review card held here records
`source_status=review` so an explicit unblock restores review. On success
`evidence` is the compact hold receipt:

```json
{
  "state": "PASS",
  "read_back": {"state": "PASS", "data": {"id", "title", "status", "assignee", "block_reason", "workspace_path", "branch_name", "model_override", "provider_override", "created_at", "started_at", "completed_at"}},
  "worker_before": {...},
  "worker_after": {...},
  "warning": "Holding blocks dispatch, not the process. A live worker may still write its workspace."
}
```

The bridge requires the hold receipt to be a durable, verified hold: `read_back.state`
must be `PASS`, `read_back.data.id` must equal the requested card, and
`read_back.data.status` must be `blocked` or `triage`. A missing or
contradictory `read_back` (e.g. an empty object or a still-running status) is
`state=UNKNOWN`, never a silent PASS. A held card stays held; nothing here
stops a live worker.

## Validation errors

- 400: malformed board slug (`../escape`, `ev o`, too long, leading dash).
- 422: malformed card id (must match `t_[0-9a-f]{8,32}`), out-of-range
  `limit`/`card_limit`, over-long `cursor`, an unknown body field
  (`extra=forbid`), a missing required body field.

## Safe next actions (both UI workers)

- Attention card: inspect worker or review block reason; prepare a
  continuation. Never infer operator authority from a generic blocked status.
- Changes empty page: no observed changes, not proof of worker liveness.
- Timeline: bounded wall-time observation only.
- Readiness `ready_to_release=false` with a board-permission FAIL: the board
  is read-only for this server; surface the remedy, do not auto-release.
- Draft: review `worker.verdict` and the unverified excerpts before continue.
- Continue: show the new held card; never unblock/dispatch; on UNKNOWN inspect
  before retry; on stale fingerprint require a re-draft.
- Hold: preserves history and never pretends a running worker stopped.
