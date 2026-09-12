// Browser-mounted test for the kanban dashboard IIFE evidence layer.
//
// Exercises the real IIFE (bundled with a minimal React SDK shim) against a
// temporary local HTTP fixture server whose /evidence/* responses match
// EVIDENCE-CONTRACTS.json. Network boundary mocking only: fetchJSON/authedFetch
// hit the fixture server over the real fetch(). Headless Chromium only, closed
// in finally.
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

const API = "/api/plugins/kanban";
const now = Math.floor(Date.now() / 1000);

// --- fixture state (reset between scenarios) --------------------------------
const state = {
  hits: [],
  evoSnapshotCalls: 0,
  evoDelayMs: 0, // delay applied to the NEXT evo page-0 snapshot call
};

function resetState() {
  state.hits = [];
  state.evoSnapshotCalls = 0;
  state.evoDelayMs = 0;
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

function attachmentsFor(card) {
  if (card === "t_run1") return [{ id: 7, filename: "report.txt", size: 12, content_type: "text/plain" }];
  if (card === "t_run2") return [{ id: 999, filename: "gone.txt", size: 5, content_type: "text/plain" }];
  return [];
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
    const html = '<!doctype html><html><head><meta charset="utf-8"><title>kanban test</title></head><body><div id="root"></div><script src="/bundle.js"></script></body></html>';
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(html);
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
    state.hits.push("page:" + q.get("resource") + ":" + (q.get("card") || ""));
    const items = attachmentsFor(q.get("card"));
    const obs = Math.floor(Date.now() / 1000);
    return json(res, 200, envelope("PASS", { items: items, returned: items.length, has_more: false, next_cursor: null, total: items.length, incomplete: false, omitted: 0, remaining_after_page: 0, high_water_rowid: 99, evolving_view: "live", bounded: { limit: 50, max_limit: 100 }, observed_at: obs }));
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
    return json(res, 200, { task: localTask(id, title, status), comments: [], events: [], attachments: [], links: { parents: [], children: [] }, runs: [], child_results: [] });
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

function newPage(base, selectedBoard) {
  const ctx = browser.newContext({ viewport: { width: 1280, height: 900 } });
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

main().catch(function (e) {
  console.error("RUNNER ERROR:", e && e.stack ? e.stack : e);
  if (browser) browser.close().catch(function () {});
  if (server) server.close();
  process.exit(2);
});
