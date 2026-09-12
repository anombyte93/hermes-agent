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
};

function resetState() {
  state.hits = [];
  state.evoSnapshotCalls = 0;
  state.evoDelayMs = 0;
  state.pageDelay = null;
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

function handle(req, res) {
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
    const title = id === "t_run1" ? "Run card one" : id === "t_run2" ? "Run card two" : "Done card";
    const status = id === "t_done1" ? "done" : "running";
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

  json(res, 404, { detail: "not found: " + p });
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

main().catch(function (e) {
  console.error("RUNNER ERROR:", e && e.stack ? e.stack : e);
  if (browser) browser.close().catch(function () {});
  if (server) server.close();
  process.exit(2);
});
