// Browser-mounted test for the kanban dashboard IIFE evidence layer.
//
// Exercises the real IIFE (bundled with a minimal React SDK shim) against a
// temporary local HTTP fixture server whose /evidence/* responses match
// EVIDENCE-CONTRACTS.json. Network boundary mocking only: fetchJSON/authedFetch
// hit the fixture server over the real fetch(). Headless Chromium only, closed
// in finally.
//
// K10 additions (this revision): the drawer's RUNS/EVENTS/ATTACHMENTS are
// driven by bounded /evidence/page cursors (one per resource) with omissions
// and a visible Load more that APPENDS rows. The fixture now serves
// multi-page runs/events/attachments, an include_history-aware /tasks/:id
// detail read, plus "broken" (helper unavailable) and "stale" (old
// observation) boards, and the suite gains phone-width + drawer-race
// scenarios while preserving the original 18 assertions.
//
// Run:  node plugins/kanban/dashboard/tests/browser/run-browser-test.js
//       (with node-v22 on PATH and NODE_PATH pointing at the repo node_modules)
const path = require("path");
const fs = require("fs");
const os = require("os");
const http = require("http");
const crypto = require("crypto");
const { chromium } = require("playwright");

const REPO = path.resolve(__dirname, "..", "..", "..", "..", "..");
const CHROME = "/home/hayden/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome";
const ENTRY = path.join(__dirname, "fixture-entry.js");
const STYLE = path.join(__dirname, "..", "..", "dist", "style.css");

const API = "/api/plugins/kanban";
const now = Math.floor(Date.now() / 1000);

// --- fixture state (reset between scenarios) --------------------------------
const state = {
  hits: [],
  evoSnapshotCalls: 0,
  evoDelayMs: 0, // delay applied to the NEXT evo page-0 snapshot call
  pageDelay: null, // {ms, card, resource} — delay the NEXT matching /evidence/page
  revision: 0, // bumped to simulate a concurrent card change (stale draft)
  continueUnknown: false, // when true, /workflow/continue returns UNKNOWN
  timelineDelayMs: 0, // delay the NEXT /evidence/timeline for timelineDelayCard
  timelineDelayCard: null,
  eventsBaseline: null, // null | "fail" | "unknown" | "malformed" | "wrongboard"
};

function resetState() {
  state.hits = [];
  state.evoSnapshotCalls = 0;
  state.evoDelayMs = 0;
  state.pageDelay = null;
  state.revision = 0;
  state.continueUnknown = false;
  state.timelineDelayMs = 0;
  state.timelineDelayCard = null;
  state.eventsBaseline = null;
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
  res.end(body);
}

function envelope(envState, evidence, reason, remedy) {
  const e = { state: envState, evidence: evidence, board: null, helper: { tool: "kanban_snapshot", execution_host: "evo" }, request: {}, timing: {} };
  if (reason) e.reason = reason;
  if (remedy) e.remedy = remedy;
  if (evidence && typeof evidence.observed_at === "number") e.observed_at = evidence.observed_at;
  return e;
}

function snapshotFor(board, cursor) {
  const obs = Math.floor(Date.now() / 1000);
  if (board === "evo") {
    if (cursor) {
      return envelope("PASS", {
        board: "evo", status_filter: "all",
        cards: [{ id: "t_done1", title: "Done card", assignee: "evo", status: "done", created_at: obs - 100 }],
        returned: 1, has_more: false, next_cursor: null, omitted: 0,
        counts: { by_status: { running: 1, done: 2 }, total: 3, matching_filter: 3, in_page: 1, omitted: 0 },
        worker_observations: [], worker_observation_cap: 20, worker_observations_capped: 0,
        worker_evidence_scope: "running records only", bounded: { card_limit: 100, max_card_limit: 200, worker_check_cap: 20 },
        incomplete: false, observed_at: obs,
      });
    }
    state.evoSnapshotCalls += 1;
    const updated = state.evoSnapshotCalls >= 2;
    const cards = [
      { id: "t_run1", title: updated ? "Run card one UPDATED" : "Run card one", assignee: "evo", status: updated ? "done" : "running", created_at: obs - 10 },
      { id: "t_run2", title: "Run card two", assignee: "evo", status: "running", created_at: obs - 10 },
    ];
    const obs_list = updated ? [] : [{ task_id: "t_run1", run_id: 10, classification: "live", state: "PASS", process_present: true, workspace_matches: true, run_start_matches: true, reason: "worker process present and matching" }];
    return envelope("PASS", {
      board: "evo", status_filter: "all", cards, returned: 2, has_more: true, next_cursor: "evo:p2", omitted: 1,
      counts: { by_status: { running: updated ? 1 : 2, done: updated ? 2 : 1 }, total: 3, matching_filter: 3, in_page: 2, omitted: 1 },
      worker_observations: obs_list, worker_observation_cap: 20, worker_observations_capped: 0,
      worker_evidence_scope: "running records only", bounded: { card_limit: 100, max_card_limit: 200, worker_check_cap: 20 },
      incomplete: true, observed_at: obs,
    });
  }
  if (board === "held") {
    // Next-ten batch-readiness board: two blocked (held) cards + one triage.
    return envelope("PASS", {
      board: "held", status_filter: "all",
      cards: [
        { id: "t_hold1", title: "Held card one", assignee: "evo", status: "blocked", created_at: obs - 30 },
        { id: "t_hold2", title: "Held card two", assignee: "evo", status: "blocked", created_at: obs - 20 },
        { id: "t_hold3", title: "Triage card", assignee: "evo", status: "triage", created_at: obs - 10 },
      ],
      returned: 3, has_more: false, next_cursor: null, omitted: 0,
      counts: { by_status: { blocked: 2, triage: 1 }, total: 3, matching_filter: 3, in_page: 3, omitted: 0 },
      worker_observations: [], worker_observation_cap: 20, worker_observations_capped: 0,
      worker_evidence_scope: "running records only", bounded: { card_limit: 100, max_card_limit: 200, worker_check_cap: 20 },
      incomplete: false, observed_at: obs,
    });
  }
  if (board === "other") {
    return envelope("PASS", {
      board: "other", status_filter: "all",
      cards: [{ id: "t_other1", title: "Other board card", assignee: "evo", status: "running", created_at: obs - 10 }],
      returned: 1, has_more: false, next_cursor: null, omitted: 0,
      counts: { by_status: { running: 1 }, total: 1, matching_filter: 1, in_page: 1, omitted: 0 },
      worker_observations: [], worker_observation_cap: 20, worker_observations_capped: 0,
      worker_evidence_scope: "running records only", bounded: { card_limit: 100, max_card_limit: 200, worker_check_cap: 20 },
      incomplete: false, observed_at: obs,
    });
  }
  if (board === "broken") {
    // Helper available on the host but the read itself failed: surface the
    // FAIL envelope + remedy, never a green board.
    return envelope("FAIL", null, "helper unavailable", "Install the helper on the EVO host");
  }
  if (board === "stale") {
    // Observation is 400s old -> past the red freshness threshold.
    const oldObs = obs - 400;
    return envelope("PASS", {
      board: "stale", status_filter: "all",
      cards: [{ id: "t_stale1", title: "Stale card", assignee: "evo", status: "running", created_at: oldObs - 10 }],
      returned: 1, has_more: false, next_cursor: null, omitted: 0,
      counts: { by_status: { running: 1 }, total: 1, matching_filter: 1, in_page: 1, omitted: 0 },
      worker_observations: [], worker_observation_cap: 20, worker_observations_capped: 0,
      worker_evidence_scope: "running records only", bounded: { card_limit: 100, max_card_limit: 200, worker_check_cap: 20 },
      incomplete: false, observed_at: oldObs,
    });
  }
  return envelope("UNKNOWN", null, "board not found");
}

function workerFor(card) {
  const obs = Math.floor(Date.now() / 1000);
  if (card === "t_run1") {
    return envelope("PASS", {
      task_id: "t_run1", card_status: "running", assignee: "evo",
      observations: [{ run_id: 10, classification: "live", state: "PASS", process_present: true, workspace_matches: true, run_start_matches: true, reason: "worker process present and matching" }],
      completion_runs: [],
      aggregate: { overall: "RUNNING", running: 1, stopped: 0, completion_records: 0, unknown: 0, complete: false },
      limitation: "process presence only",
    });
  }
  if (card === "t_run2") {
    return envelope("PASS", {
      task_id: "t_run2", card_status: "running", assignee: "evo",
      observations: [],
      completion_runs: [],
      aggregate: { overall: "UNKNOWN", running: 1, stopped: 0, completion_records: 0, unknown: 1, complete: false },
      limitation: "no live observation",
    });
  }
  if (card === "t_done1") {
    return envelope("PASS", {
      task_id: "t_done1", card_status: "done", assignee: "evo",
      observations: [],
      completion_runs: [],
      aggregate: { overall: "STOPPED", running: 0, stopped: 1, completion_records: 0, unknown: 0, complete: true },
      limitation: "stopped",
    });
  }
  return envelope("FAIL", null, "card does not exist on this board");
}

// --- bounded /evidence/page resource items (shape matches _run_dict /
// _event_dict / _attachment_dict serialisers) --------------------------------
function run(id, profile, outcome, summary, started_at, ended_at) {
  return { id: id, task_id: "t_run1", profile: profile, step_key: null, status: outcome, claim_lock: null, claim_expires: null, worker_pid: null, max_runtime_seconds: 1500, last_heartbeat_at: null, started_at: started_at, ended_at: ended_at, outcome: outcome, summary: summary, metadata: {}, error: null };
}
function ev(id, kind, created_at) {
  return { id: id, task_id: "t_run1", kind: kind, payload: {}, created_at: created_at, run_id: null };
}
function att(id, filename, size, task_id) {
  return { id: id, task_id: task_id || "t_run1", filename: filename, content_type: "text/plain", size: size, uploaded_by: "evo", stored_path: null, created_at: now - 5 };
}

function pageData(board, resource, card, cursor) {
  const empty = { items: [], returned: 0, has_more: false, next_cursor: null, total: 0, omitted: 0, remaining_after_page: 0, high_water_rowid: 0, evolving_view: "live" };
  if (board !== "evo") return empty;
  const obs = Math.floor(Date.now() / 1000);

  if (resource === "attachments" && card === "t_run1") {
    if (cursor) return { items: [att(8, "report2.txt", 8)], returned: 1, has_more: false, next_cursor: null, total: 2, omitted: 0, remaining_after_page: 0, high_water_rowid: 8, evolving_view: "live" };
    return { items: [att(7, "report.txt", 12)], returned: 1, has_more: true, next_cursor: "att:2", total: 2, omitted: 1, remaining_after_page: 1, high_water_rowid: 7, evolving_view: "live" };
  }
  if (resource === "attachments" && card === "t_run2") {
    return { items: [att(999, "gone.txt", 5, "t_run2")], returned: 1, has_more: false, next_cursor: null, total: 1, omitted: 0, remaining_after_page: 0, high_water_rowid: 999, evolving_view: "live" };
  }
  if (resource === "runs" && card === "t_run1") {
    if (cursor) return { items: [run(3, "evo", "completed", "third run summary", obs - 3, obs - 3)], returned: 1, has_more: false, next_cursor: null, total: 3, omitted: 0, remaining_after_page: 0, high_water_rowid: 3, evolving_view: "live" };
    return { items: [run(1, "evo", "completed", "first run summary", obs - 2, obs - 2), run(2, "evo", "completed", "second run summary", obs - 1, obs - 1)], returned: 2, has_more: true, next_cursor: "runs:2", total: 3, omitted: 1, remaining_after_page: 1, high_water_rowid: 2, evolving_view: "live" };
  }
  if (resource === "runs" && card === "t_run2") {
    return { items: [run(20, "evo", "completed", "RUN TWO A", obs - 5, obs - 5)], returned: 1, has_more: false, next_cursor: null, total: 1, omitted: 0, remaining_after_page: 0, high_water_rowid: 20, evolving_view: "live" };
  }
  if (resource === "events" && card === "t_run1") {
    if (cursor) return { items: [ev(13, "spawned", obs - 1)], returned: 1, has_more: false, next_cursor: null, total: 3, omitted: 0, remaining_after_page: 0, high_water_rowid: 13, evolving_view: "live" };
    return { items: [ev(11, "created", obs - 3), ev(12, "claimed", obs - 2)], returned: 2, has_more: true, next_cursor: "ev:2", total: 3, omitted: 1, remaining_after_page: 1, high_water_rowid: 12, evolving_view: "live" };
  }
  if (resource === "events" && card === "t_run2") {
    return { items: [ev(21, "created", obs - 5)], returned: 1, has_more: false, next_cursor: null, total: 1, omitted: 0, remaining_after_page: 0, high_water_rowid: 21, evolving_view: "live" };
  }
  return empty;
}

function localTask(card, title, status) {
  return { id: card, title: title, status: status, assignee: "evo", tenant: null, created_at: now - 50 };
}

function localBoard() {
  return {
    columns: [
      { name: "triage", tasks: [] }, { name: "todo", tasks: [] }, { name: "ready", tasks: [] },
      { name: "running", tasks: [localTask("t_run1", "Local run card", "running")] },
      { name: "blocked", tasks: [] }, { name: "review", tasks: [] }, { name: "done", tasks: [localTask("t_done1", "Local done card", "done")] },
    ],
    latest_event_id: 3,
    assignees: [],
  };
}

// --- workflow fixtures (WORKFLOW-API-CONTRACT.md standard envelope) ---------
function wfEnvelope(state_, board, card, evidence, reason, remedy, tool, extra) {
  const e = {
    state: state_,
    board: board,
    card: card || null,
    evidence: evidence == null ? null : evidence,
    helper: { tool: tool || "kanban_workflow", execution_host: "evo" },
    request: {},
    timing: { helper_roundtrip_ms: 1.0, collection_ms: 0.5 },
    observed_at: Math.floor(Date.now() / 1000),
  };
  if (reason != null) e.reason = reason;
  if (remedy != null) e.remedy = remedy;
  if (extra) Object.assign(e, extra);
  return e;
}

function attentionCard(id, title, status, reasons, nextAction) {
  return {
    id: id, title: title, assignee: "evo", status: status, block_reason: null,
    block_kind: null, current_run_id: null, created_at: now - 60, started_at: null,
    completed_at: null, reasons: reasons || ["status " + status],
    next_action: nextAction || "inspect card", operator_authority_needed: null,
    running_total: 0, running_truncated: false, process_state: "not_applicable", process_present: null,
  };
}

function attentionData(board, cursor) {
  if (board !== "evo" && board !== "denied") {
    return { cards: [], returned: 0, has_more: false, next_cursor: null, omitted: 0 };
  }
  const p2 = cursor === "attn:p2";
  const cards = p2
    ? [attentionCard("t_attn3", "Attention triage card", "triage", ["raw idea"], "specify before dispatch")]
    : [
        attentionCard("t_attn1", "Attention blocked card", "blocked", ["status blocked"], "inspect worker / review block reason"),
        attentionCard("t_attn2", "Attention review card", "review", ["implementation complete"], "review and complete"),
      ];
  return {
    board: board, read_at: now, observed_at: now, freshness: { read_at: now, note: "one bounded read" },
    cards: cards, returned: cards.length, has_more: !p2, next_cursor: p2 ? null : "attn:p2",
    omitted: p2 ? 0 : 1, remaining_after_page: p2 ? 0 : 1, high_water_rowid: 40,
    running_verification: { selected: 0, checked: 0, absent: 0, healthy: 0, unexamined: 0, truncated: false },
    process_checks: { performed: 0, capped: 0, cap: 20, note: "bounded" },
    bounded: { limit: 50, max_limit: 200, process_check_cap: 20, runs_per_card_cap: 20 },
    incomplete: false,
  };
}

function changeEvent(id, kind, taskId, ts) {
  return { id: id, task_id: taskId, kind: kind, created_at: ts, run_id: null };
}

function changesData(board, cursor) {
  if (board !== "evo" && board !== "denied") {
    return { events: [], returned: 0, has_more: false, next_cursor: null };
  }
  const base = {
    board: board, read_at: now, observed_at: now, freshness: { read_at: now, note: "one bounded read" },
    first_read_policy: "baseline-now: no historical events returned",
    total_events: 874, baseline_id: 874, empty_means_no_observed_changes: true,
    id_gap_note: "id gaps alone do not prove loss", incomplete: false,
  };
  if (cursor === "chg:p3") {
    return Object.assign({}, base, { events: [changeEvent(877, "commented", "t_run1", now - 3)], returned: 1, has_more: false, next_cursor: null, high_water_rowid: 877, anchor_state: "poll" });
  }
  if (cursor === "chg:p2") {
    return Object.assign({}, base, { events: [changeEvent(875, "blocked", "t_attn1", now - 10), changeEvent(876, "claimed", "t_run1", now - 5)], returned: 2, has_more: true, next_cursor: "chg:p3", high_water_rowid: 876, anchor_state: "poll" });
  }
  return Object.assign({}, base, { events: [], returned: 0, has_more: true, next_cursor: "chg:p2", high_water_rowid: 874, anchor_state: "baseline" });
}

// GET /events/baseline — the notification baseline for the existing /events
// WebSocket stream. Reads the current MAX(id) from the SAME selected-server
// DB as /events (works local and EVO alike). Evidence is {board, baseline_id}.
function eventsBaseline(board) {
  if (board === "evo") return { board: "evo", baseline_id: 874 };
  if (board === "other") return { board: "other", baseline_id: 150 };
  if (board === "denied") return { board: "denied", baseline_id: 874 };
  // local/broken/stale have no event table in the fixture: UNKNOWN.
  return null;
}

function interval(kind, start, end, runIds, evIds) {
  return { kind: kind, start: start, end: end, duration_seconds: end - start, source_runs: runIds || [], source_events: evIds || [] };
}

function timelineData(board, card, cursor) {
  if (board !== "evo" && board !== "denied") {
    return { intervals: [], returned: 0, has_more: false, next_cursor: null };
  }
  // t_attn2 is the discriminator for the late-card test: one lone execution
  // interval, no blocked/review/unknown kinds, no gap, no further page.
  if (card === "t_attn2") {
    return {
      board: board, card: card, card_status: "review", read_at: now, observed_at: now,
      freshness: { read_at: now, note: "one bounded read" },
      boundary: { read_at: now, clamped_at: now, note: "clamped" },
      intervals: [interval("execution", now - 600, now - 120, [99], [])],
      returned: 1, has_more: false, next_cursor: null,
      totals: { covered_window: { execution_seconds: 480, blocked_seconds: 0, review_wait_seconds: 0, unknown_seconds: 0 }, page: {}, all_time: null },
      coverage: { window_start: now - 600, window_end: now, gaps: [], note: "covered window" },
      incomplete: false,
    };
  }
  const t0 = now - 2000;
  if (cursor === "tl:p2") {
    const ints = [interval("execution", t0 - 2000, t0 - 1500, [52], [])];
    return {
      board: board, card: card, card_status: "blocked", read_at: now, observed_at: now,
      freshness: { read_at: now, note: "one bounded read" },
      boundary: { read_at: now, clamped_at: now, note: "clamped" },
      intervals: ints, returned: 1, has_more: false, next_cursor: null,
      totals: { covered_window: { execution_seconds: 978, blocked_seconds: 1212, review_wait_seconds: 0, unknown_seconds: 387 }, page: {}, all_time: null },
      coverage: { window_start: t0 - 2000, window_end: now, gaps: [], note: "covered window" },
      incomplete: false,
    };
  }
  const ints = [
    interval("execution", t0, t0 + 978, [51], []),
    interval("blocked", t0 + 978, t0 + 1038, [], [724]),
    interval("unknown", t0 + 1038, t0 + 1425, [], []),
    interval("review", t0 + 1425, t0 + 1545, [], []),
  ];
  return {
    board: board, card: card, card_status: "blocked", read_at: now, observed_at: now,
    freshness: { read_at: now, note: "one bounded read" },
    boundary: { read_at: now, clamped_at: now, note: "clamped" },
    intervals: ints, returned: 4, has_more: true, next_cursor: "tl:p2",
    totals: { covered_window: { execution_seconds: 978, blocked_seconds: 60, review_wait_seconds: 120, unknown_seconds: 387 }, page: {}, all_time: null },
    coverage: { window_start: t0, window_end: now, gaps: [{ start: t0 + 1200, end: t0 + 1380, duration_seconds: 180, note: "no record" }], note: "covered window" },
    incomplete: true,
  };
}

function readinessReceipt(board, card) {
  if (board === "denied") {
    return wfEnvelope("FAIL", board, card, {
      state: "FAIL",
      requested: { board: board, profile: "evo", provider: "deepseek", model: "deepseek-v4-pro", workspace: "/tmp/ws", expected_revision: "0000000000000000000000000000000000000000", parents: [], check_model: true, python: ".venv/bin/python", minimum_python: "3.12", require_modules: [], context_files: [] },
      observed_at: now,
      freshness: { checked_at: now, stale_after: now + 60, note: "bounded" },
      ready_to_release: false,
      checks: [
        { name: "board_permission", state: "FAIL", reason: "this server is read-only for the board; ATLAS_KANBAN_WRITE_BOARDS must name it", exists: true, write_scope_configured: false, mutation_authorized: false },
      ],
      boundary: "readiness-boundary",
    }, "readiness not established", "Add the board to ATLAS_KANBAN_WRITE_BOARDS to permit writes", "kanban_readiness");
  }
  return wfEnvelope("PASS", board, card, {
    state: "PASS",
    requested: { board: board, profile: "evo", provider: "deepseek", model: "deepseek-v4-pro", workspace: "/tmp/ws", expected_revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", parents: [], check_model: true, python: ".venv/bin/python", minimum_python: "3.12", require_modules: [], context_files: [] },
    observed_at: now,
    freshness: { checked_at: now, stale_after: now + 60, note: "bounded evidence validity" },
    ready_to_release: true,
    checks: [
      { name: "board_permission", state: "PASS", reason: "board exists and this server may mutate it", exists: true, write_scope_configured: true, mutation_authorized: true },
      { name: "profile_exists", state: "PASS", reason: "profile evo exists" },
      { name: "workspace_exists", state: "PASS", reason: "workspace present" },
      { name: "expected_revision", state: "PASS", reason: "git HEAD matches" },
      { name: "model", state: "PASS", reason: "deepseek-v4-pro READY under installed runtime" },
    ],
    boundary: "readiness-boundary",
  }, null, null, "kanban_readiness");
}

function computeFingerprint(board, body) {
  const canonical = {
    board: board,
    card: body.card,
    revision: state.revision,
    passed_checks: body.passed_checks || [],
    remaining_checks: body.remaining_checks || [],
    verification_note: body.verification_note || "",
    workspace: body.workspace || "",
    profile: body.profile || "",
    provider: body.provider || "",
    model: body.model || "",
    title: body.title || "",
  };
  return crypto.createHash("sha256").update(JSON.stringify(canonical)).digest("hex");
}

function draftReceipt(board, body) {
  const fp = computeFingerprint(board, body);
  return wfEnvelope("PASS", board, body.card, {
    state: "PASS",
    board: board,
    card: body.card,
    fingerprint: fp,
    original: { status: "blocked", assignee: "evo", result_excerpt: "prior attempt result excerpt", latest_run_summary_excerpt: "previous worker stopped after verified work", source: "unverified source excerpt; truncated; not confirmed by a worker" },
    worker: { verdict: "STOPPED", reason: "previous workers are stopped; safe to continue", aggregate: { overall: "STOPPED", running: 0, stopped: 1, completion_records: 0, unknown: 0, complete: true } },
    passed_checks: body.passed_checks || [],
    remaining_checks: body.remaining_checks || [],
    verification_note: body.verification_note || "",
    commission: { workspace: body.workspace || "", profile: body.profile || "", provider: body.provider || "", model: body.model || "", max_runtime_minutes: 60, creator: body.creator || "", title: body.title || "" },
    read_at: { original: now, worker: now, note: "fresh reads at draft time" },
    mutation_authorized: board === "evo",
    limitation: "fingerprint is an optimistic concurrency check, not signed authority",
    no_mutation_performed: true,
  }, null, null, "kanban_continuation_draft");
}

function continueReceipt(board, body) {
  if (board === "denied") {
    return wfEnvelope("FAIL", board, body.card, null,
      "This client refuses mutations outside ATLAS_KANBAN_WRITE_BOARDS; it is read-only for this board",
      "Add the board to ATLAS_KANBAN_WRITE_BOARDS to permit writes", "kanban_continue");
  }
  if (state.continueUnknown) {
    return wfEnvelope("UNKNOWN", board, body.card, null,
      "continuation create accepted but the new card identity could not be re-read; hold state is UNKNOWN, inspect before release",
      "Inspect the result before retrying", "kanban_continue", { new_card: "t_held1" });
  }
  const fresh = computeFingerprint(board, body);
  if (fresh !== body.fingerprint) {
    return wfEnvelope("FAIL", board, body.card, null,
      "fingerprint does not match the fresh original state; the draft is stale or the intent changed. Re-draft before continuing",
      "Re-draft before continuing", "kanban_continue", { fingerprint_mismatch: true });
  }
  return wfEnvelope("PASS", board, body.card, {
    state: "PASS", board: board, original_card: body.card, new_card: "t_held1",
    new_card_status: "blocked", new_card_assignee: "evo", held: true,
    released_after_previous_creation: false, reblocked_after_release: false,
    origin: { board: board, card: body.card, original_status: "blocked", note: "original card was not rewritten, reopened, reset or completed, and is not a dependency" },
    worker: { verdict: "STOPPED", reason: "previous workers are stopped; safe to continue" },
    no_original_mutation: true,
  }, null, null, "kanban_continue");
}

function holdReceipt(board, card) {
  if (board === "denied") {
    return wfEnvelope("FAIL", board, card, null,
      "This client refuses mutations outside ATLAS_KANBAN_WRITE_BOARDS; it is read-only for this board",
      "Add the board to ATLAS_KANBAN_WRITE_BOARDS to permit writes", "kanban_hold");
  }
  return wfEnvelope("PASS", board, card, {
    state: "PASS",
    read_back: { state: "PASS", data: { id: card, title: "Attention blocked card", status: "blocked", assignee: "evo", block_reason: "held for review", workspace_path: null, branch_name: null, model_override: null, provider_override: null, created_at: now, started_at: null, completed_at: null } },
    worker_before: { overall: "STOPPED", running: 0, stopped: 1, unknown: 0, complete: true },
    worker_after: { overall: "STOPPED", running: 0, stopped: 1, unknown: 0, complete: true },
    warning: "Holding blocks dispatch, not the process. A live worker may still write its workspace.",
  }, null, null, "kanban_hold");
}

function handle(req, res) {
  const chunks = [];
  req.on("data", function (c) { chunks.push(c); });
  req.on("end", function () {
    handleRoutes(req, res, Buffer.concat(chunks).toString("utf8"));
  });
}

function handleRoutes(req, res, reqBody) {
  const u = new URL(req.url, "http://localhost");
  const p = u.pathname;
  const q = u.searchParams;
  const board = q.get("board") || "";

  if (p === "/") {
    const html = '<!doctype html><html><head><meta charset="utf-8"><title>kanban test</title><link rel="stylesheet" href="/style.css"></head><body><div id="root"></div><script src="/bundle.js"></script></body></html>';
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(html);
    return;
  }
  if (p === "/style.css") {
    const css = fs.readFileSync(STYLE);
    res.writeHead(200, { "Content-Type": "text/css" });
    res.end(css);
    return;
  }
  if (p === "/bundle.js") {
    const b = fs.readFileSync(bundlePath);
    res.writeHead(200, { "Content-Type": "application/javascript" });
    res.end(b);
    return;
  }

  if (p === `${API}/config`) return json(res, 200, { render_markdown: true });
  if (p === `${API}/boards`) return json(res, 200, {
    boards: [
      { slug: "evo", name: "EVO", total: 3 },
      { slug: "other", name: "Other", total: 1 },
      { slug: "local", name: "Local", total: 2 },
      { slug: "broken", name: "Broken", total: 0 },
      { slug: "stale", name: "Stale", total: 1 },
      { slug: "denied", name: "Denied", total: 1 },
      { slug: "held", name: "Held", total: 3 },
    ],
    current: "evo",
  });
  if (p === `${API}/board`) { state.hits.push("board"); return json(res, 200, localBoard()); }

  if (p === `${API}/evidence/context`) {
    state.hits.push("context:" + (board || ""));
    if (board === "local") return json(res, 200, { aligned: false, board: "local", hostname: "archie", reason: "this server is not the EVO host", observed_at: now });
    return json(res, 200, { aligned: true, board: board, hostname: "evo", reason: "aligned", observed_at: now });
  }

  if (p === `${API}/evidence/snapshot`) {
    state.hits.push("snapshot:" + board + (q.get("cursor") ? ":cursor" : ""));
    const cursor = q.get("cursor");
    const respond = function () {
      json(res, 200, snapshotFor(board, cursor));
    };
    if (board === "evo" && !cursor && state.evoDelayMs > 0) {
      const d = state.evoDelayMs;
      state.evoDelayMs = 0;
      setTimeout(respond, d);
    } else {
      respond();
    }
    return;
  }

  if (p === `${API}/evidence/worker`) {
    state.hits.push("worker:" + q.get("card"));
    return json(res, 200, workerFor(q.get("card")));
  }

  if (p === `${API}/evidence/page`) {
    const resource = q.get("resource");
    const card = q.get("card") || "";
    state.hits.push("page:" + resource + ":" + card + (q.get("cursor") ? ":cursor" : ""));
    const respond = function () {
      const data = pageData(board, resource, card, q.get("cursor"));
      const obs = Math.floor(Date.now() / 1000);
      json(res, 200, envelope("PASS", Object.assign({ board: board, observed_at: obs }, data)));
    };
    if (state.pageDelay && state.pageDelay.ms > 0 && state.pageDelay.card === card && state.pageDelay.resource === resource) {
      const d = state.pageDelay.ms;
      state.pageDelay = null;
      setTimeout(respond, d);
    } else {
      respond();
    }
    return;
  }

  const att = p.match(new RegExp("^" + API + "/attachments/(\\d+)$"));
  if (att) {
    state.hits.push("attachment:" + att[1]);
    if (att[1] === "7") {
      const body = "hello report";
      res.writeHead(200, { "Content-Type": "text/plain", "Content-Length": Buffer.byteLength(body) });
      res.end(body);
    } else {
      json(res, 404, { detail: "attachment not found" });
    }
    return;
  }

  const task = p.match(new RegExp("^" + API + "/tasks/([^/]+)$"));
  if (task) {
    const id = task[1];
    const titles = {
      t_run1: "Run card one", t_run2: "Run card two", t_done1: "Done card",
      t_attn1: "Attention blocked card", t_attn2: "Attention review card",
      t_attn3: "Attention triage card", t_deny1: "Denied board card", t_held1: "Held continuation card",
    };
    const statuses = {
      t_done1: "done", t_attn1: "blocked", t_attn2: "review", t_attn3: "triage",
      t_deny1: "blocked", t_held1: "blocked",
    };
    const title = titles[id] || "Task " + id;
    const status = statuses[id] || "running";
    const includeHistory = q.get("include_history") !== "false";
    const detail = { task: localTask(id, title, status), comments: [], links: { parents: [], children: [] }, child_results: [] };
    // Mirror the parent-owned detail-route contract: include_history=false
    // materialises no runs/events/attachments/diagnostics and reports
    // history_included=false + diagnostics_state=UNKNOWN.
    if (includeHistory) {
      detail.history_included = true;
      detail.diagnostics_state = "PASS";
      detail.events = [];
      detail.attachments = [];
      detail.runs = [];
    } else {
      detail.history_included = false;
      detail.diagnostics_state = "UNKNOWN";
      detail.events = [];
      detail.attachments = [];
      detail.runs = [];
    }
    return json(res, 200, detail);
  }

  if (p.startsWith(`${API}/tasks/`) && p.endsWith("/log")) {
    return json(res, 200, { exists: false });
  }
  if (p === `${API}/home-channels`) {
    return json(res, 200, { home_channels: [] });
  }

  if (p === `${API}/evidence/attention`) {
    state.hits.push("attention:" + board);
    return json(res, 200, wfEnvelope("PASS", board, null, attentionData(board, q.get("cursor")), null, null, "kanban_attention"));
  }
  if (p === `${API}/evidence/changes`) {
    state.hits.push("changes:" + board + (q.get("cursor") ? ":cursor" : ""));
    return json(res, 200, wfEnvelope("PASS", board, null, changesData(board, q.get("cursor")), null, null, "kanban_changes"));
  }
  if (p === `${API}/events/baseline`) {
    state.hits.push("events-baseline:" + board);
    if (state.eventsBaseline === "fail") {
      return json(res, 200, wfEnvelope("FAIL", board, null, null,
        "event baseline unavailable", "retry the baseline read", "kanban_events_baseline"));
    }
    if (state.eventsBaseline === "unknown") {
      return json(res, 200, wfEnvelope("UNKNOWN", board, null, null,
        "event baseline could not be resolved", "retry the baseline read", "kanban_events_baseline"));
    }
    if (state.eventsBaseline === "malformed") {
      // PASS but the baseline id is not a nonnegative integer: unknown.
      return json(res, 200, wfEnvelope("PASS", board, null, { board: board, baseline_id: -5 }, null, null, "kanban_events_baseline"));
    }
    if (state.eventsBaseline === "wrongboard") {
      // PASS but the evidence names a different board: exact-board gate fails.
      return json(res, 200, wfEnvelope("PASS", board, null, { board: "some-other-board", baseline_id: 874 }, null, null, "kanban_events_baseline"));
    }
    const base = eventsBaseline(board);
    if (!base) {
      // Absent/unreadable event table: UNKNOWN, never a synthetic baseline.
      return json(res, 200, wfEnvelope("UNKNOWN", board, null, null,
        "no event table for this board", "retry the baseline read", "kanban_events_baseline"));
    }
    return json(res, 200, wfEnvelope("PASS", board, null, base, null, null, "kanban_events_baseline"));
  }
  if (p === `${API}/evidence/timeline`) {
    const card = q.get("card") || "";
    state.hits.push("timeline:" + card);
    const respond = function () {
      json(res, 200, wfEnvelope("PASS", board, card, timelineData(board, card, q.get("cursor")), null, null, "kanban_timeline"));
    };
    if (state.timelineDelayMs > 0 && card === state.timelineDelayCard) {
      const d = state.timelineDelayMs;
      state.timelineDelayMs = 0;
      setTimeout(respond, d);
    } else {
      respond();
    }
    return;
  }
  if (p === `${API}/workflow/readiness`) {
    let body = {};
    try { body = reqBody ? JSON.parse(reqBody) : {}; } catch (_e) { body = {}; }
    state.hits.push("readiness:" + (body.card || ""));
    return json(res, 200, readinessReceipt(board, body.card));
  }
  if (p === `${API}/workflow/continuation-draft`) {
    let body = {};
    try { body = reqBody ? JSON.parse(reqBody) : {}; } catch (_e) { body = {}; }
    state.hits.push("draft:" + (body.card || ""));
    return json(res, 200, draftReceipt(board, body));
  }
  if (p === `${API}/workflow/continue`) {
    let body = {};
    try { body = reqBody ? JSON.parse(reqBody) : {}; } catch (_e) { body = {}; }
    state.hits.push("continue:" + (body.card || ""));
    return json(res, 200, continueReceipt(board, body));
  }
  if (p === `${API}/workflow/hold`) {
    let body = {};
    try { body = reqBody ? JSON.parse(reqBody) : {}; } catch (_e) { body = {}; }
    state.hits.push("hold:" + (body.card || ""));
    return json(res, 200, holdReceipt(board, body.card));
  }
  if (p === `${API}/dispatch`) {
    state.hits.push("dispatch");
    return json(res, 200, { dispatched: 0, reason: "fixture records only" });
  }

  // --- next-ten fixture doors (R1/R2/R4/R6/R8/R9) -------------------------
  const NOW_NT = Math.floor(Date.now() / 1000);
  if (p === `${API}/evidence/releases`) {
    state.hits.push("releases:" + board);
    if (board === "evo") {
      return json(res, 200, envelope("PASS", {
        board: "evo",
        adapter: { state: "PASS", revision: "a84a2b2c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a", source: "atlas-kanban installed release", version: "1.4.0" },
        observed_at: NOW_NT,
      }));
    }
    return json(res, 200, envelope("UNKNOWN", null, "helper unavailable"));
  }
  if (p === `${API}/evidence/browser-readiness`) {
    state.hits.push("browser-readiness:" + board);
    if (board === "evo") {
      return json(res, 200, {
        state: "PASS", board: "evo",
        evidence: { reachable: true, authenticated: true, board_readable: true, observed_at: NOW_NT },
        observed_at: NOW_NT,
      });
    }
    if (board === "denied") {
      return json(res, 200, {
        state: "FAIL", board: "denied",
        evidence: { reachable: true, authenticated: true, board_readable: false, observed_at: NOW_NT },
        reason: "board denied", remedy: "Check the board slug", observed_at: NOW_NT,
      });
    }
    return json(res, 200, {
      state: "UNKNOWN", board: board,
      evidence: { reachable: true, authenticated: true, board_readable: null, observed_at: NOW_NT },
      reason: "helper unavailable", remedy: "Install the helper", observed_at: NOW_NT,
    });
  }
  if (p === `${API}/evidence/acceptance-compare`) {
    state.hits.push("acceptance-compare:" + q.get("card"));
    return json(res, 200, envelope("PASS", {
      board: board, card: q.get("card"),
      current: { run_id: Number(q.get("current_run_id")), state: "PASS", release: { revision: "a84a2b2c0d1e", source: "served" }, kind: "machine validation" },
      previous: { run_id: Number(q.get("previous_run_id")), state: "UNKNOWN", release: null, kind: "parent attestation" },
      checks: [
        { name: "worker_stopped", current: "PASS", previous: "UNKNOWN", change: "reverified", kind: "machine validation" },
        { name: "parent_source_review", current: null, previous: "PASS", change: "unproved", kind: "parent attestation" },
      ],
      limitations: ["prior receipts absent before capture began"],
      observed_at: NOW_NT,
    }));
  }
  if (p === `${API}/evidence/reviewer-packet`) {
    state.hits.push("reviewer-packet:" + q.get("card"));
    return json(res, 200, envelope("PASS", {
      schema_version: 1, board: board, card: q.get("card"), observed_at: NOW_NT,
      packet: {
        identity: { adapter_revision: "a84a2b2c0d1e" },
        task: { id: q.get("card"), status: "blocked", assignee: "evo", title: "Bounded title" },
        worker: { overall: "STOPPED", running: 0 },
        runs: [{ id: 3, outcome: "completed" }],
        attachments: [{ id: 7, filename: "report.txt" }],
        acceptance: { receipts: 1, latest_state: "PASS" },
      },
      bounds: { runs: 10, attachments: 20, comments: 0, note: "bounded" },
      limitations: ["raw bodies excluded"],
    }));
  }
  if (p === `${API}/evidence/attachment-provenance`) {
    state.hits.push("provenance:" + q.get("attachment_id"));
    const attId = Number(q.get("attachment_id"));
    const data = attId === 7
      ? { board: board, card: q.get("card"), attachment_id: 7, accepted_run_id: 3, acceptance_state: "PASS", reason: "guarded receipt", observed_at: NOW_NT }
      : { board: board, card: q.get("card"), attachment_id: attId, accepted_run_id: null, acceptance_state: "UNKNOWN", reason: "no guarded acceptance receipt names this attachment; an attachment beside a done card is not acceptance", observed_at: NOW_NT };
    return json(res, 200, envelope("PASS", data));
  }
  if (p === `${API}/workflow/readiness-batch`) {
    let body = {};
    try { body = reqBody ? JSON.parse(reqBody) : {}; } catch (_e) { body = {}; }
    state.hits.push("readiness-batch:" + (body.cards || []).join(","));
    const items = (body.cards || []).map(function (card) {
      if (card === "t_attn3") {
        return {
          card: card, state: "UNKNOWN", receipt: null,
          repair_preview: [{ check: "card_presence", state: "UNKNOWN", action: "Open an existing card on this board.", reason: "not present" }],
        };
      }
      return {
        card: card, state: "FAIL",
        receipt: { checks: [
          { name: "board_permission", state: "PASS", reason: "writable" },
          { name: "python_interpreter", state: "FAIL", reason: "workspace .venv missing" },
        ] },
        repair_preview: [{ check: "python_interpreter", state: "FAIL", action: "Provision the workspace .venv (python -m venv .venv) with a supported interpreter.", reason: "workspace .venv missing" }],
      };
    });
    return json(res, 200, wfEnvelope("PASS", board, null, {
      board: board, items: items,
      requested: (body.cards || []).length, returned: items.length, omitted: 0,
      no_mutation_performed: true, observed_at: NOW_NT,
    }, null, null, "kanban_readiness_batch"));
  }

  json(res, 404, { detail: "not found: " + p });
}

// --- WebSocket fixture server (real upgrade + frames, no `ws` dependency) ---
// The IIFE opens a genuine WebSocket to /api/plugins/kanban/events via the
// SDK buildWsUrl shim; this server accepts that upgrade over the same HTTP
// server and lets scenarios push REAL frames over the wire. No source-grep,
// no callback invoked by hand. Server->client frames are unmasked text frames
// carrying the same {"events": [...], "cursor": N} shape the Python stream
// sends.

const wsConns = []; // { socket, board, since }

function wsAccept(key) {
  return crypto.createHash("sha1")
    .update(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
    .digest("base64");
}

function wsFrame(text) {
  const payload = Buffer.from(text, "utf8");
  const len = payload.length;
  let header;
  if (len < 126) {
    header = Buffer.from([0x81, len]);
  } else if (len < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 126;
    header.writeUInt16BE(len, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([header, payload]);
}

function registerWsUpgrade(server) {
  server.on("upgrade", function (req, socket) {
    const u = new URL(req.url, "http://localhost");
    if (u.pathname !== `${API}/events`) {
      socket.write("HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n");
      socket.destroy();
      return;
    }
    const key = req.headers["sec-websocket-key"];
    if (!key) { socket.destroy(); return; }
    socket.write(
      "HTTP/1.1 101 Switching Protocols\r\n" +
      "Upgrade: websocket\r\n" +
      "Connection: Upgrade\r\n" +
      "Sec-WebSocket-Accept: " + wsAccept(key) + "\r\n\r\n"
    );
    const conn = {
      socket: socket,
      board: u.searchParams.get("board") || "",
      since: u.searchParams.get("since") || "0",
    };
    wsConns.push(conn);
    socket.on("close", function () {
      const i = wsConns.indexOf(conn);
      if (i >= 0) wsConns.splice(i, 1);
    });
    socket.on("error", function () {
      const i = wsConns.indexOf(conn);
      if (i >= 0) wsConns.splice(i, 1);
    });
    // Ignore incoming (masked) client frames; the browser sends only a close
    // frame on shutdown and the TCP close handler above does the cleanup.
    socket.on("data", function () { /* ignore */ });
  });
}

function wsEvent(id, kind, taskId) {
  return { id: id, task_id: taskId, run_id: null, kind: kind, payload: {}, created_at: now - 1 };
}

// Push a real frame to every live connection for `board`. Returns the number
// of sockets that accepted the write. A closed socket (board switched) cannot
// receive it, which is the late-previous-board behavior under test.
function sendWsFrame(board, events) {
  const frame = wsFrame(JSON.stringify({
    events: events,
    cursor: events.length ? events[events.length - 1].id : 0,
  }));
  let sent = 0;
  for (const conn of wsConns) {
    if (conn.board !== board) continue;
    try {
      conn.socket.write(frame);
      sent += 1;
    } catch (_e) { /* socket already closed */ }
  }
  return sent;
}

async function waitForWs(board, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 3000);
  while (Date.now() < deadline) {
    if (wsConns.some(function (c) { return c.board === board; })) return;
    await new Promise(function (r) { setTimeout(r, 25); });
  }
  throw new Error("timed out waiting for WS connection on board " + board);
}

// --- build the bundle -------------------------------------------------------
let bundlePath;
let server;
let browser;

const results = [];
function check(name, ok, extra) {
  results.push({ name: name, ok: !!ok, extra: extra || "" });
}

async function main() {
  const esbuild = require("esbuild");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "kanban-browser-"));
  bundlePath = path.join(tmp, "bundle.js");
  await esbuild.build({
    entryPoints: [ENTRY],
    bundle: true,
    platform: "browser",
    format: "iife",
    outfile: bundlePath,
    logLevel: "silent",
  });

  server = http.createServer(handle);
  registerWsUpgrade(server);
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  const base = "http://127.0.0.1:" + port;

  browser = await chromium.launch({ executablePath: CHROME, headless: true });

  try {
    await scenarioAligned(base);
    await scenarioMissingFile(base);
    await scenarioNotAligned(base);
    await scenarioRaceAndRefresh(base);
    await scenarioMultiPage(base);
    await scenarioHelperUnavailable(base);
    await scenarioStale(base);
    await scenarioDrawerRace(base);
    await scenarioPhone(base);
    await scenarioWorkflowAttention(base);
    await scenarioWorkflowTimeline(base);
    await scenarioWorkflowReadiness(base);
    await scenarioWorkflowContinuation(base);
    await scenarioWorkflowStale(base);
    await scenarioWorkflowDenied(base);
    await scenarioWorkflowLateCard(base);
    await scenarioWorkflowPhone(base);
    await scenarioWorkflowNotices(base);
    await scenarioWorkflowNoticeBaselineUnknown(base);
    await scenarioWorkflowNoticeBoardSwitch(base);
    await scenarioWorkflowNoticePhone(base);
    await scenarioNextTenSupport(base);
    await scenarioNextTenDrawer(base);
    await scenarioNextTenBatch(base);
    await scenarioNextTenNotices(base);
    await scenarioNextTenPhone(base);
  } finally {
    await browser.close();
    server.close();
  }

  const failed = results.filter((r) => !r.ok);
  console.log("\n=== browser evidence test results ===");
  for (const r of results) {
    console.log((r.ok ? "PASS " : "FAIL ") + r.name + (r.extra ? "  [" + r.extra + "]" : ""));
  }
  console.log("\n" + (results.length - failed.length) + "/" + results.length + " checks passed");
  console.log("scratch bundle dir left at: " + tmp);
  if (failed.length > 0) process.exit(1);
  process.exit(0);
}

function newPage(base, selectedBoard, viewport) {
  const vp = viewport || { width: 1280, height: 900 };
  const ctx = browser.newContext({ viewport: vp });
  return ctx.then(function (c) {
    if (selectedBoard) {
      return c.addInitScript(function (slug) {
        localStorage.setItem("hermes.kanban.selectedBoard", slug);
      }, selectedBoard).then(function () { return c; });
    }
    return c;
  }).then(function (c) { return c.newPage(); });
}

async function scenarioAligned(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });

  // Grid is driven by the snapshot, not local /board.
  check("aligned: snapshot fetched, /board not fetched",
    state.hits.includes("snapshot:evo") && !state.hits.includes("board"),
    state.hits.join(","));

  await page.waitForSelector("text=Run card one", { timeout: 5000 });
  check("aligned: snapshot card title rendered in grid", await page.locator("text=Run card one").count() > 0);

  // Badges: t_run1 Running (observation), t_run2 Unknown (no observation).
  await page.waitForSelector(".hermes-kanban-evidence-badge >> text=Running", { timeout: 5000 });
  const badges = await page.locator(".hermes-kanban-evidence-badge").allTextContents();
  check("aligned: Running badge present", badges.some((t) => t.trim() === "Running"), JSON.stringify(badges));
  check("aligned: Unknown badge present", badges.some((t) => t.trim() === "Unknown"), JSON.stringify(badges));

  const banner = await page.locator(".hermes-kanban-evidence-banner").textContent();
  check("aligned: counts show total 3", /total 3/.test(banner), banner);
  check("aligned: counts show omitted 1", /omitted 1/.test(banner), banner);

  // Load more adds the paged card and clears the Load more affordance.
  await page.click("text=Load more");
  await page.waitForSelector("text=Done card", { timeout: 5000 });
  check("aligned: load more appended the paged card", await page.locator("text=Done card").count() > 0);
  check("aligned: load more gone after final page", await page.locator("text=Load more").count() === 0);

  // Open the running card drawer and check worker evidence + attachments.
  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("text=Worker evidence", { timeout: 5000 });
  check("aligned: drawer worker state Running", await page.locator("text=Worker evidence").locator("..").locator("text=Running").count() > 0);
  await page.waitForSelector("text=Evidence attachments", { timeout: 5000 });
  await page.waitForSelector("text=report.txt", { timeout: 5000 });

  // Download the attachment and assert content equality.
  const dlPromise = page.waitForEvent("download");
  await page.click("text=report.txt");
  const dl = await dlPromise;
  const dlPath = await dl.path();
  const content = fs.readFileSync(dlPath, "utf8");
  check("aligned: downloaded attachment content equality", content === "hello report", JSON.stringify(content));

  await page.close();
}

async function scenarioMissingFile(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });
  await page.locator("[data-task-id='t_run2']").first().click();
  await page.waitForSelector("text=Evidence attachments", { timeout: 5000 });
  await page.waitForSelector("text=gone.txt", { timeout: 5000 });
  await page.click("text=gone.txt");
  await page.waitForSelector("text=missing file:", { timeout: 5000 });
  check("missing-file: 404 surfaced distinctly from metadata", true);
  await page.close();
}

async function scenarioNotAligned(base) {
  resetState();
  const page = await (await newPage(base, "local"));
  await page.goto(base + "/");
  await page.waitForSelector("text=not the EVO evidence database", { timeout: 5000 });
  check("not-aligned: visible mismatch banner", await page.locator("text=Choose the EVO connection").count() > 0);
  check("not-aligned: local /board fetched", state.hits.includes("board"), state.hits.join(","));
  check("not-aligned: no snapshot fetched", !state.hits.some((h) => h.startsWith("snapshot:")), state.hits.join(","));
  check("not-aligned: no evidence badges", await page.locator(".hermes-kanban-evidence-badge").count() === 0);
  await page.close();
}

async function scenarioRaceAndRefresh(base) {
  resetState();
  state.evoDelayMs = 800; // first evo page-0 snapshot is slow
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  // Immediately switch to the fast "other" board while evo's snapshot is in flight.
  await page.waitForSelector('select[aria-label="Switch kanban board"]', { timeout: 5000 });
  await page.selectOption('select[aria-label="Switch kanban board"]', "other");
  await page.waitForSelector("text=Other board card", { timeout: 5000 });
  await page.waitForTimeout(1200);
  // Late evo response must have been dropped by the generation guard.
  check("race: late evo snapshot dropped (no 'Run card one')", await page.locator("text=Run card one").count() === 0);

  // Switch back to evo: this triggers a second snapshot with the updated card
  // and a removed observation, so the previously-Running card becomes Unknown.
  await page.selectOption('select[aria-label="Switch kanban board"]', "evo");
  await page.waitForSelector("text=Run card one UPDATED", { timeout: 5000 });
  const badges2 = await page.locator(".hermes-kanban-evidence-badge").allTextContents();
  check("refresh: running observation removed -> no Running badge", !badges2.some((t) => t.trim() === "Running"), JSON.stringify(badges2));
  check("refresh: card title updated on refresh", await page.locator("text=Run card one UPDATED").count() > 0);
  await page.close();
}

// K10: bounded drawer history/attachments with a visible Load more that
// appends rows, per resource, and clears after exhaustion.
async function scenarioMultiPage(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });
  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("text=Run history", { timeout: 5000 });

  // Runs page 1: total 3, two loaded rows, Load more with omitted 1.
  await page.waitForSelector("text=first run summary", { timeout: 5000 });
  check("multi: runs page 1 shows both loaded runs", await page.locator("text=second run summary").count() > 0);
  check("multi: runs header shows total 3", await page.locator("text=Run history (3)").count() > 0);
  const runsBtn = await page.locator('[data-evidence-resource="runs"]').textContent();
  check("multi: runs Load more shows omitted 1", /1 omitted/.test(runsBtn), runsBtn);

  await page.click('[data-evidence-resource="runs"]');
  await page.waitForSelector("text=third run summary", { timeout: 5000 });
  check("multi: runs load more appended third run", await page.locator("text=third run summary").count() > 0);
  check("multi: runs load more gone after exhaustion", await page.locator('[data-evidence-resource="runs"]').count() === 0);

  // Events pagination: page 1 (claimed) then page 2 (spawned) appended.
  await page.waitForSelector("text=claimed", { timeout: 5000 });
  await page.click('[data-evidence-resource="events"]');
  await page.waitForSelector("text=spawned", { timeout: 5000 });
  check("multi: events load more appended next event", await page.locator("text=spawned").count() > 0);

  // Attachments pagination: page 1 (report.txt) then page 2 (report2.txt).
  await page.waitForSelector("text=report.txt", { timeout: 5000 });
  await page.click('[data-evidence-resource="attachments"]');
  await page.waitForSelector("text=report2.txt", { timeout: 5000 });
  check("multi: attachments load more appended next attachment", await page.locator("text=report2.txt").count() > 0);
  check("multi: attachments load more gone after exhaustion", await page.locator('[data-evidence-resource="attachments"]').count() === 0);

  await page.close();
}

// K10: a FAIL snapshot (helper unavailable) is surfaced as an explicit banner,
// never a green board.
async function scenarioHelperUnavailable(base) {
  resetState();
  const page = await (await newPage(base, "broken"));
  await page.goto(base + "/");
  await page.waitForSelector("text=Worker evidence unavailable", { timeout: 5000 });
  check("helper-unavailable: visible banner", await page.locator("text=helper unavailable").count() > 0);
  check("helper-unavailable: remedy visible", await page.locator("text=Install the helper on the EVO host").count() > 0);
  await page.close();
}

// K10: an old observation (400s) crosses the red freshness threshold and is
// shown stale, never as fresh.
async function scenarioStale(base) {
  resetState();
  const page = await (await newPage(base, "stale"));
  await page.goto(base + "/");
  await page.waitForSelector(".hermes-kanban-evidence--stale-red", { timeout: 5000 });
  check("stale: red stale class present for old observation", true);
  const staleText = await page.locator(".hermes-kanban-evidence-stale").textContent();
  check("stale: observed age text present", /observed \d+s ago/.test(staleText), staleText);
  await page.close();
}

// K10: a card's runs page in flight must not leak into another card.
async function scenarioDrawerRace(base) {
  resetState();
  state.pageDelay = { ms: 800, card: "t_run1", resource: "runs" };
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });

  // Open t_run1 (its runs page is delayed 800ms), then close it and open
  // t_run2 before the delayed page resolves.
  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("text=Worker evidence", { timeout: 5000 });
  await page.click(".hermes-kanban-drawer-close");
  await page.locator("[data-task-id='t_run2']").first().click();
  await page.waitForSelector("text=RUN TWO A", { timeout: 5000 });
  await page.waitForTimeout(1200);

  check("drawer-race: card two shows its own run", await page.locator("text=RUN TWO A").count() > 0);
  check("drawer-race: late card-one runs page dropped", await page.locator("text=first run summary").count() === 0);
  await page.close();
}

// K10: phone width — click targets are actually visible and the columns strip
// scrolls horizontally.
async function scenarioPhone(base) {
  resetState();
  const page = await (await newPage(base, "evo", { width: 390, height: 844 }));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });
  // Wait for the board columns to actually render before measuring scroll,
  // otherwise the container may still be empty at the moment we evaluate.
  await page.waitForSelector("[data-task-id='t_run1']", { timeout: 5000 });
  const scrollInfo = await page.locator(".hermes-kanban-columns").first().evaluate(function (el) {
    return { scrollable: el.scrollWidth > el.clientWidth, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth };
  });
  check("phone: columns strip scrolls horizontally", scrollInfo.scrollable, JSON.stringify(scrollInfo));

  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("text=Worker evidence", { timeout: 5000 });
  check("phone: card click target visible and opens drawer", await page.locator("text=Worker evidence").count() > 0);

  await page.close();
}

// K2: bounded attention queue, Load-more paging, and exact drawer open.
async function scenarioWorkflowAttention(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.waitForSelector("text=Attention blocked card", { timeout: 5000 });

  check("attention: bounded queue shows both cards",
    await page.locator("[data-workflow-attention-card='t_attn1']").count() === 1 &&
    await page.locator("[data-workflow-attention-card='t_attn2']").count() === 1);
  check("attention: omitted count button present", await page.locator("[data-workflow-attention-more]").count() === 1);

  await page.click("[data-workflow-attention-more]");
  await page.waitForSelector("[data-workflow-attention-card='t_attn3']", { timeout: 5000 });
  check("attention: load more appended paged card", await page.locator("[data-workflow-attention-card='t_attn3']").count() === 1);
  check("attention: load more gone after exhaustion", await page.locator("[data-workflow-attention-more]").count() === 0);

  // Clicking a queue card opens the EXACT drawer for that card.
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector(".hermes-kanban-drawer-head", { timeout: 5000 });
  check("attention: exact drawer opens for t_attn1", await page.locator(".hermes-kanban-drawer-head").locator("text=t_attn1").count() > 0);

  await page.close();
}

// K4: card timeline — disjoint interval kinds, visible gaps, bounded paging.
async function scenarioWorkflowTimeline(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-timeline='true']", { timeout: 5000 });
  await page.waitForSelector("[data-workflow-timeline-interval='execution']", { timeout: 5000 });

  check("timeline: execution interval rendered", true);
  check("timeline: blocked interval rendered", await page.locator("[data-workflow-timeline-interval='blocked']").count() > 0);
  check("timeline: review interval rendered", await page.locator("[data-workflow-timeline-interval='review']").count() > 0);
  check("timeline: unknown interval rendered", await page.locator("[data-workflow-timeline-interval='unknown']").count() > 0);

  check("timeline: gap visible", await page.locator("[data-workflow-timeline-gap]").count() > 0);

  const totals = await page.locator(".hermes-kanban-workflow-totals").textContent();
  check("timeline: covered-window totals shown, never productivity", /covered window/.test(totals) && /not productivity/.test(totals), totals);

  await page.click("[data-workflow-timeline-more]");
  await page.waitForTimeout(300);
  check("timeline: load more appended second execution interval", await page.locator("[data-workflow-timeline-interval='execution']").count() === 2);
  check("timeline: load more gone after exhaustion", await page.locator("[data-workflow-timeline-more]").count() === 0);

  await page.close();
}

// K5: explicit readiness — POST only on click, separate permission check.
async function scenarioWorkflowReadiness(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-readiness='true']", { timeout: 5000 });

  check("readiness: no POST before click", !state.hits.some((h) => h.startsWith("readiness:")), state.hits.join(","));

  await page.click("[data-workflow-readiness-check]");
  await page.waitForSelector("[data-workflow-readiness-item='board_permission']", { timeout: 5000 });

  check("readiness: POST fired only on click", state.hits.some((h) => h.startsWith("readiness:")), state.hits.join(","));
  check("readiness: separate permission check rendered", await page.locator("[data-workflow-readiness-item='board_permission']").count() === 1);
  check("readiness: permission shows mutation authorized", await page.locator("[data-workflow-readiness-item='board_permission']").locator("text=mutation authorized").count() > 0);
  check("readiness: model check rendered", await page.locator("[data-workflow-readiness-item='model']").count() === 1);
  check("readiness: ready to release yes", await page.locator("text=ready to release: yes").count() > 0);

  await page.close();
}

// K6+K7: continuation draft (read-only) then a separate create click -> held.
async function scenarioWorkflowContinuation(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-continuation='true']", { timeout: 5000 });

  await page.fill("[data-workflow-cont-note]", "Synthetic continuation acceptance control.");
  await page.fill("[data-workflow-cont-passed]", "Parent source validation passed");
  await page.fill("[data-workflow-cont-remaining]", "Non-executing protocol acceptance control");
  await page.fill("[data-workflow-cont-acceptance]", "Create one held card; original history unchanged; never dispatch");
  await page.fill("[data-workflow-cont-workspace]", "/tmp/adapter-integration");
  await page.fill("[data-workflow-cont-profile]", "evo");
  await page.fill("[data-workflow-cont-provider]", "deepseek");
  await page.fill("[data-workflow-cont-model]", "deepseek-v4-pro");
  await page.fill("[data-workflow-cont-title]", "Non-executing continuation control");

  check("continuation: no create button before draft", await page.locator("[data-workflow-continue]").count() === 0);

  await page.click("[data-workflow-draft]");
  await page.waitForSelector("[data-workflow-draft-result]", { timeout: 5000 });

  check("continuation: draft hit recorded", state.hits.some((h) => h.startsWith("draft:")), state.hits.join(","));
  check("continuation: no continue hit during draft", !state.hits.some((h) => h.startsWith("continue:")), state.hits.join(","));
  check("continuation: fingerprint shown", await page.locator(".hermes-kanban-workflow-fingerprint").count() > 0);
  check("continuation: original labelled unverified", await page.locator("[data-workflow-draft-original]").locator("text=unverified source excerpt").count() > 0);
  check("continuation: entered remaining check echoed", await page.locator("[data-workflow-draft-result]").locator("text=Non-executing protocol acceptance control").count() > 0);

  await page.click("[data-workflow-continue]");
  await page.waitForSelector("[data-workflow-continue-result]", { timeout: 5000 });
  await page.waitForSelector("text=continuation held: t_held1", { timeout: 5000 });

  check("continuation: held result shown", await page.locator("[data-workflow-continue-result]").locator("text=t_held1").count() > 0);
  check("continuation: open new card affordance", await page.locator("[data-workflow-open-new]").count() === 1);
  check("continuation: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));

  await page.close();
}

// K7: stale fingerprint requires a re-draft, never a blind retry.
async function scenarioWorkflowStale(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-continuation='true']", { timeout: 5000 });

  await page.fill("[data-workflow-cont-note]", "stale draft test");
  await page.fill("[data-workflow-cont-passed]", "A");
  await page.fill("[data-workflow-cont-remaining]", "B");
  await page.fill("[data-workflow-cont-workspace]", "/tmp/ws");
  await page.fill("[data-workflow-cont-profile]", "evo");
  await page.fill("[data-workflow-cont-provider]", "deepseek");
  await page.fill("[data-workflow-cont-model]", "deepseek-v4-pro");

  await page.click("[data-workflow-draft]");
  await page.waitForSelector("[data-workflow-draft-result]", { timeout: 5000 });

  state.revision += 1; // simulate a concurrent card change after the draft

  await page.click("[data-workflow-continue]");
  await page.waitForSelector("[data-workflow-continue-result]", { timeout: 5000 });
  await page.waitForSelector("text=Stale draft", { timeout: 5000 });

  check("stale: re-draft required, no held card", await page.locator("text=Stale draft").count() > 0);
  check("stale: no new card shown", await page.locator("text=t_held1").count() === 0);

  await page.close();
}

// K5/K8 denied writes: board outside ATLAS_KANBAN_WRITE_BOARDS is read-only.
async function scenarioWorkflowDenied(base) {
  resetState();
  const page = await (await newPage(base, "denied"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-readiness='true']", { timeout: 5000 });

  await page.click("[data-workflow-readiness-check]");
  await page.waitForSelector("[data-workflow-readiness-item='board_permission']", { timeout: 5000 });
  check("denied: readiness permission FAIL visible", await page.locator("[data-workflow-readiness-item='board_permission']").locator("text=FAIL").count() > 0);
  check("denied: ready to release no", await page.locator("text=ready to release: no").count() > 0);
  check("denied: write-scope remedy visible", await page.locator("text=ATLAS_KANBAN_WRITE_BOARDS").count() > 0);

  await page.fill("[data-workflow-hold-reason]", "hold for review");
  await page.click("[data-workflow-hold-submit]");
  await page.waitForSelector("[data-workflow-hold-result]", { timeout: 5000 });
  check("denied: hold refused read-only", await page.locator("[data-workflow-hold-result]").locator("text=read-only").count() > 0);
  check("denied: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));

  await page.close();
}

// Late workflow response: a delayed timeline for card A never leaks into card B.
async function scenarioWorkflowLateCard(base) {
  resetState();
  state.timelineDelayMs = 800;
  state.timelineDelayCard = "t_attn1";
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });

  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-readiness='true']", { timeout: 5000 });
  await page.click(".hermes-kanban-drawer-close");
  await page.click("[data-workflow-attention-card='t_attn2']");
  await page.waitForSelector("[data-workflow-timeline-interval='execution']", { timeout: 5000 });
  await page.waitForTimeout(1200);

  check("late-card: card two shows its own execution interval", await page.locator("[data-workflow-timeline-interval='execution']").count() > 0);
  check("late-card: card one blocked interval dropped", await page.locator("[data-workflow-timeline-interval='blocked']").count() === 0);
  check("late-card: card one review interval dropped", await page.locator("[data-workflow-timeline-interval='review']").count() === 0);

  await page.close();
}

// 390px phone: workflow surface renders and the queue opens the drawer.
async function scenarioWorkflowPhone(base) {
  resetState();
  const page = await (await newPage(base, "evo", { width: 390, height: 844 }));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-attention='true']", { timeout: 5000 });
  await page.waitForSelector("[data-workflow-attention-card='t_attn1']", { timeout: 5000 });

  await page.click("[data-workflow-attention-card='t_attn1']");
  await page.waitForSelector("[data-workflow-continuation='true']", { timeout: 5000 });
  check("phone-workflow: queue card opens drawer with continuation", await page.locator("[data-workflow-continuation='true']").count() > 0);
  check("phone-workflow: readiness check button visible", await page.locator("[data-workflow-readiness-check]").count() === 1);
  check("phone-workflow: timeline section visible", await page.locator("[data-workflow-timeline='true']").count() > 0);

  await page.close();
}

// K7 notification region: baseline/replay zero alerts, newly arrived review
// and changes open the exact card, duplicate frames collapse, no dispatch.
// Frames are REAL WebSocket frames driven by the fixture server, never a
// callback invoked by hand.
async function scenarioWorkflowNotices(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice='true']", { timeout: 5000 });
  await page.waitForSelector("[data-workflow-notice-baseline='ready']", { timeout: 5000 });
  await page.waitForSelector("[data-workflow-notice-baseline-id='874']", { timeout: 5000 });
  check("notices: bounded /events/baseline established", state.hits.some((h) => h.startsWith("events-baseline:evo")), state.hits.join(","));

  await waitForWs("evo", 5000);

  // Historical / replay events (id <= baseline 874) must produce zero alerts.
  sendWsFrame("evo", [
    wsEvent(100, "review_requested", "t_old1"),
    wsEvent(200, "completed", "t_old2"),
  ]);
  await page.waitForTimeout(400);
  check("notices: historical replay suppressed (zero alerts)",
    await page.locator("[data-workflow-notice-item]").count() === 0);

  // Newly arrived review_requested + changes_requested surface exact cards.
  sendWsFrame("evo", [
    wsEvent(900, "review_requested", "t_run1"),
    wsEvent(901, "changes_requested", "t_run2"),
  ]);
  await page.waitForSelector("[data-workflow-notice-item='t_run1']", { timeout: 5000 });
  await page.waitForSelector("[data-workflow-notice-item='t_run2']", { timeout: 5000 });
  check("notices: review_requested surfaced for exact card",
    await page.locator("[data-workflow-notice-kind='review_requested']").count() === 1);
  check("notices: changes_requested surfaced for exact card",
    await page.locator("[data-workflow-notice-kind='changes_requested']").count() === 1);

  // A duplicate frame (same event ids) must not add a second row.
  sendWsFrame("evo", [
    wsEvent(900, "review_requested", "t_run1"),
    wsEvent(901, "changes_requested", "t_run2"),
  ]);
  await page.waitForTimeout(400);
  check("notices: duplicate frame collapsed to one row each",
    await page.locator("[data-workflow-notice-item]").count() === 2);

  // Clicking the review_requested row opens the exact drawer.
  await page.click("[data-workflow-notice-open-item='t_run1']");
  await page.waitForSelector(".hermes-kanban-drawer-head", { timeout: 5000 });
  check("notices: click opens exact card drawer",
    await page.locator(".hermes-kanban-drawer-head").locator("text=t_run1").count() > 0);

  check("notices: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));
  await page.close();
}

// K7: FAIL/UNKNOWN/malformed baseline suppresses historical notifications and
// offers a retry; a retry against a now-valid baseline recovers and new events
// flow. No historical replay in any of the failure shapes.
async function scenarioWorkflowNoticeBaselineUnknown(base) {
  // FAIL shape
  resetState();
  state.eventsBaseline = "fail";
  let page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='unknown']", { timeout: 5000 });
  check("notices-fail: baseline FAIL becomes unknown, retry offered",
    await page.locator("[data-workflow-notice-retry]").count() === 1);
  await waitForWs("evo", 5000);
  sendWsFrame("evo", [wsEvent(950, "completed", "t_run1")]);
  await page.waitForTimeout(400);
  check("notices-fail: historical notifications suppressed",
    await page.locator("[data-workflow-notice-item]").count() === 0);
  await page.close();

  // UNKNOWN shape
  resetState();
  state.eventsBaseline = "unknown";
  page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='unknown']", { timeout: 5000 });
  check("notices-unknown: baseline UNKNOWN -> unknown + retry",
    await page.locator("[data-workflow-notice-retry]").count() === 1);
  await page.close();

  // malformed shape (negative baseline id)
  resetState();
  state.eventsBaseline = "malformed";
  page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='unknown']", { timeout: 5000 });
  check("notices-malformed: non-integer/negative baseline treated unknown",
    await page.locator("[data-workflow-notice-retry]").count() === 1);
  await page.close();

  // wrong-board shape (exact-board gate fails)
  resetState();
  state.eventsBaseline = "wrongboard";
  page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='unknown']", { timeout: 5000 });
  check("notices-wrongboard: board mismatch treated unknown",
    await page.locator("[data-workflow-notice-retry]").count() === 1);
  await page.close();

  // Retry recovery: baseline fails once, then a retry (with the failure mode
  // cleared) establishes a valid baseline and new events flow.
  resetState();
  state.eventsBaseline = "fail";
  page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='unknown']", { timeout: 5000 });
  state.eventsBaseline = null; // recovery: next read is a valid baseline
  await page.click("[data-workflow-notice-retry]");
  await page.waitForSelector("[data-workflow-notice-baseline='ready']", { timeout: 5000 });
  await waitForWs("evo", 5000);
  sendWsFrame("evo", [wsEvent(960, "review_requested", "t_run2")]);
  await page.waitForSelector("[data-workflow-notice-item='t_run2']", { timeout: 5000 });
  check("notices-retry: retry establishes baseline and new events flow",
    await page.locator("[data-workflow-notice-item='t_run2']").count() === 1);
  check("notices-retry: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));
  await page.close();
}

// K7: per-board isolation. Switching boards clears notices and re-establishes
// a per-board baseline; a frame on the new board flows, a late frame on the
// OLD board's (now-closed) connection is ignored.
async function scenarioWorkflowNoticeBoardSwitch(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='ready']", { timeout: 5000 });
  await waitForWs("evo", 5000);

  // A valid evo event surfaces.
  sendWsFrame("evo", [wsEvent(900, "review_requested", "t_run1")]);
  await page.waitForSelector("[data-workflow-notice-item='t_run1']", { timeout: 5000 });

  // Switch to "other": its own baseline (150), fresh notice set.
  await page.selectOption('select[aria-label="Switch kanban board"]', "other");
  await page.waitForSelector("[data-workflow-notice-baseline-id='150']", { timeout: 5000 });
  await waitForWs("other", 5000);

  // A late frame on the OLD evo connection must not surface (the IIFE closed
  // that socket on board switch, and the board guard drops it regardless).
  sendWsFrame("evo", [wsEvent(905, "changes_requested", "t_run2")]);
  await page.waitForTimeout(400);
  check("notices-switch: late previous-board frame ignored",
    await page.locator("[data-workflow-notice-item='t_run2']").count() === 0);
  check("notices-switch: prior board notices cleared",
    await page.locator("[data-workflow-notice-item='t_run1']").count() === 0);

  // A new-board event surfaces on its own board.
  sendWsFrame("other", [wsEvent(300, "completed", "t_other1")]);
  await page.waitForSelector("[data-workflow-notice-item='t_other1']", { timeout: 5000 });
  check("notices-switch: new board isolated and receives its own event",
    await page.locator("[data-workflow-notice-item='t_other1']").count() === 1);

  check("notices-switch: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));
  await page.close();
}

// 390px phone: the in-page notification region renders and its click target
// opens the exact drawer.
async function scenarioWorkflowNoticePhone(base) {
  resetState();
  const page = await (await newPage(base, "evo", { width: 390, height: 844 }));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='ready']", { timeout: 5000 });
  await waitForWs("evo", 5000);

  sendWsFrame("evo", [wsEvent(900, "review_requested", "t_run1")]);
  await page.waitForSelector("[data-workflow-notice-item='t_run1']", { timeout: 5000 });
  check("phone-notices: notification row visible at 390px",
    await page.locator("[data-workflow-notice-item='t_run1']").count() === 1);

  await page.click("[data-workflow-notice-open-item='t_run1']");
  await page.waitForSelector(".hermes-kanban-drawer-head", { timeout: 5000 });
  check("phone-notices: click opens exact card drawer",
    await page.locator(".hermes-kanban-drawer-head").locator("text=t_run1").count() > 0);
  check("phone-notices: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));
  await page.close();
}

main().catch(function (e) {
  console.error("RUNNER ERROR:", e && e.stack ? e.stack : e);
  if (browser) browser.close().catch(function () {});
  if (server) server.close();
  process.exit(2);
});

// --- next-ten scenarios (R1-R10 user paths) ---------------------------------

// R1/R2/R7: support panel — expandable, fetches only on expand, shows
// adapter release identity + local frontend stamp, browser readiness
// separate from reachability, refresh timing and process-check counts.
async function scenarioNextTenSupport(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("text=EVO evidence", { timeout: 5000 });

  // Collapsed by default; no release reads before expand.
  check("support: collapsed by default", await page.locator("[data-support-panel-body]").count() === 0);
  check("support: no release fetch before expand", !state.hits.some((h) => h.startsWith("releases:")), state.hits.join(","));

  await page.click("[data-support-panel-toggle]");
  await page.waitForSelector("[data-support-panel-body]", { timeout: 5000 });
  await page.waitForSelector("[data-support-adapter]", { timeout: 5000 });

  check("support: adapter identity fetched on expand", state.hits.includes("releases:evo"), state.hits.join(","));
  const adapterText = await page.locator("[data-support-adapter]").textContent();
  check("support: adapter revision shown", /a84a2b2c0d1e/.test(adapterText), adapterText);
  check("support: frontend build stamp shown locally",
    await page.locator("[data-support-releases]").locator("text=frontend build:").count() > 0);

  // R2: readiness line with three separate facts.
  await page.waitForSelector("[data-support-readiness-line]", { timeout: 5000 });
  const readyText = await page.locator("[data-support-readiness-line]").textContent();
  check("support: readiness separate from reachability",
    /reachable: yes/.test(readyText) && /authenticated: yes/.test(readyText) && /board readable: yes/.test(readyText), readyText);
  check("support: browser-readiness endpoint used", state.hits.includes("browser-readiness:evo"), state.hits.join(","));

  // R7: refresh timing + process checks.
  const refreshText = await page.locator("[data-support-refresh]").textContent();
  check("support: refresh interval shown", /refresh interval: 15s/.test(refreshText), refreshText);
  check("support: process checks line present",
    await page.locator("[data-support-process-checks]").count() === 1);

  await page.close();
}

// R4/R6/R8: drawer release-evidence section — acceptance compare renders
// labelled kinds, packet download actually downloads bounded JSON,
// provenance shows the accepted run for attachment 7.
async function scenarioNextTenDrawer(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-task-id='t_run1']", { timeout: 5000 });
  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("[data-next-ten-drawer]", { timeout: 5000 });

  // R8: attachment 7 provenance (guarded receipt -> run 3).
  await page.waitForSelector("[data-attachment-provenance-line]", { timeout: 5000 });
  const provText = await page.locator("[data-attachment-provenance-line]").textContent();
  check("drawer: attachment provenance accepted run shown", /accepted by run 3/.test(provText), provText);

  // R4: compare acceptance between runs 2 and 3 (from the runs page).
  await page.waitForSelector("[data-acceptance-compare-btn]", { timeout: 5000 });
  await page.click("[data-acceptance-compare-btn]");
  await page.waitForSelector("[data-acceptance-compare-result]", { timeout: 5000 });
  check("drawer: acceptance compare fetched", state.hits.some((h) => h.startsWith("acceptance-compare:")), state.hits.join(","));
  const cmpText = await page.locator("[data-acceptance-compare-result]").textContent();
  check("drawer: compare shows run transition", /run 1/.test(cmpText) && /run 2/.test(cmpText), cmpText);
  check("drawer: parent attestation stays labelled",
    await page.locator("[data-acceptance-check='parent_source_review']").locator("text=parent attestation").count() > 0);
  check("drawer: reverified change shown",
    (await page.locator("[data-acceptance-check='worker_stopped']").textContent()).includes("reverified"));

  // R6: reviewer packet download actually lands as a file.
  const dlPromise = page.waitForEvent("download");
  await page.click("[data-reviewer-packet-btn]");
  const dl = await dlPromise;
  const dlPath = await dl.path();
  const packetRaw = fs.readFileSync(dlPath, "utf8");
  const packet = JSON.parse(packetRaw);
  check("drawer: reviewer packet downloaded as JSON",
    packet.schema_version === 1 && packet.packet && packet.packet.task.id === "t_run1", packetRaw.slice(0, 80));
  check("drawer: packet carries bounds and limitations",
    typeof packet.bounds === "object" && Array.isArray(packet.limitations));
  const banned = ["stored_path", "argv", "environ", "body"];
  check("drawer: packet carries no raw evidence channels",
    !banned.some(function (k) { return packetRaw.includes(k); }));
  await page.waitForSelector("[data-reviewer-packet-result]", { timeout: 5000 });

  await page.close();
}

// R9: batch readiness preview — held cards selectable, preview-only, no
// release affordance, per-card failures and repair previews visible. The
// "held" board carries blocked/triage snapshot cards.
async function scenarioNextTenBatch(base) {
  // Empty-pool honest state on the evo board (running cards only).
  resetState();
  let page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-readiness-batch]", { timeout: 5000 });
  check("batch: pool empty message when no held cards in page",
    await page.locator("[data-readiness-batch]").locator("text=No held (blocked/triage) cards").count() > 0);
  check("batch: run disabled with zero selected",
    (await page.locator("[data-readiness-batch-run]").getAttribute("disabled")) !== null);
  check("batch: no batch POST fired", !state.hits.some((h) => h.startsWith("readiness-batch:")), state.hits.join(","));
  check("batch: no release affordance",
    await page.locator("[data-readiness-batch]").locator("button", { hasText: /release/i }).count() === 0);
  await page.close();

  // Positive path on the held board: select two, preview, per-card results.
  resetState();
  page = await (await newPage(base, "held"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-readiness-batch-card='t_hold1']", { timeout: 5000 });
  check("batch: held pool lists blocked and triage cards",
    await page.locator("[data-readiness-batch-card='t_hold1']").count() === 1 &&
    await page.locator("[data-readiness-batch-card='t_hold3']").count() === 1);

  await page.click("[data-readiness-batch-card='t_hold1'] input, [data-readiness-batch-card='t_hold1']");
  await page.click("[data-readiness-batch-card='t_hold2'] input, [data-readiness-batch-card='t_hold2']");
  await page.waitForTimeout(200);
  await page.click("[data-readiness-batch-run]");
  await page.waitForSelector("[data-readiness-batch-result]", { timeout: 5000 });

  check("batch: POST fired once with selected cards",
    state.hits.filter((h) => h.startsWith("readiness-batch:")).length === 1 &&
    state.hits.some((h) => h === "readiness-batch:t_hold1,t_hold2"), state.hits.join(","));
  check("batch: per-card item rows visible",
    await page.locator("[data-readiness-batch-item='t_hold1']").count() === 1 &&
    await page.locator("[data-readiness-batch-item='t_hold2']").count() === 1);
  check("batch: repair preview text shown",
    (await page.locator("[data-readiness-batch-repair]").first().textContent()).includes(".venv"));
  check("batch: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));

  await page.close();
}

// R5: meaningful-intervention notifications. An unchanged repeated blocked
// event is quiet; a changed reason notifies; a recovery (completed) then a
// recurrence notifies again.
async function scenarioNextTenNotices(base) {
  resetState();
  const page = await (await newPage(base, "evo"));
  await page.goto(base + "/");
  await page.waitForSelector("[data-workflow-notice-baseline='ready']", { timeout: 5000 });
  await waitForWs("evo", 5000);

  // First blocked event for t_run1 surfaces.
  sendWsFrame("evo", [wsEvent(900, "blocked", "t_run1")]);
  await page.waitForSelector("[data-workflow-notice-item='t_run1']", { timeout: 5000 });
  check("notices-r5: first intervention surfaces",
    await page.locator("[data-workflow-notice-item='t_run1']").count() === 1);

  // UNCHANGED repeat (new event id, same task/kind, same reason/remedy/run):
  // quiet — no second row.
  sendWsFrame("evo", [wsEvent(901, "blocked", "t_run1")]);
  await page.waitForTimeout(400);
  check("notices-r5: unchanged repeat is quiet",
    await page.locator("[data-workflow-notice-item='t_run1']").count() === 1);

  // CHANGED reason (payload carries reason): notifies again.
  sendWsFrame("evo", [Object.assign(wsEvent(902, "blocked", "t_run1"), { reason: "disk full now" })]);
  await page.waitForTimeout(400);
  check("notices-r5: changed reason notifies",
    await page.locator("[data-workflow-notice-item='t_run1']").count() === 2);

  // Recovery clears the fingerprint.
  sendWsFrame("evo", [wsEvent(903, "completed", "t_run1")]);
  await page.waitForTimeout(400);
  // Recurrence after recovery notifies fresh.
  sendWsFrame("evo", [wsEvent(904, "blocked", "t_run1")]);
  await page.waitForTimeout(400);
  const rows = await page.locator("[data-workflow-notice-item='t_run1']").count();
  check("notices-r5: recurrence after recovery notifies", rows >= 3, "rows=" + rows);

  check("notices-r5: no dispatch call", !state.hits.includes("dispatch"), state.hits.join(","));
  await page.close();
}

// 390px phone: support panel and drawer next-ten section render.
async function scenarioNextTenPhone(base) {
  resetState();
  const page = await (await newPage(base, "evo", { width: 390, height: 844 }));
  await page.goto(base + "/");
  await page.waitForSelector("[data-support-panel]", { timeout: 5000 });
  await page.click("[data-support-panel-toggle]");
  await page.waitForSelector("[data-support-readiness-line]", { timeout: 5000 });
  check("phone-next-ten: support panel expands at 390px",
    await page.locator("[data-support-readiness-line]").count() === 1);

  await page.locator("[data-task-id='t_run1']").first().click();
  await page.waitForSelector("[data-next-ten-drawer]", { timeout: 5000 });
  check("phone-next-ten: drawer release-evidence section visible",
    await page.locator("[data-reviewer-packet-btn]").count() === 1);

  await page.close();
}
