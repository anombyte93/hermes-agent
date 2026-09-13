/**
 * Hermes Kanban — Dashboard Plugin
 *
 * Board view for the multi-agent collaboration board backed by
 * ~/.hermes/kanban.db. Calls the plugin's backend at /api/plugins/kanban/
 * and tails task_events over a WebSocket for live updates.
 *
 * Plain IIFE, no build step. Uses window.__HERMES_PLUGIN_SDK__ for React +
 * shadcn primitives; HTML5 drag-and-drop for card movement on desktop and
 * a pointer-based fallback for touch.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const {
    Card, CardContent,
    Badge, Button, Input, Label, Select, SelectOption,
  } = SDK.components;
  const { useState, useEffect, useCallback, useMemo, useRef } = SDK.hooks;
  const { cn, timeAgo } = SDK.utils;

  // Newer host dashboards expose a DS-styled Checkbox on the plugin SDK.
  // Fall back to a native <input type="checkbox"> shim so older hosts that
  // predate the design-system rollout still render. The shim normalises
  // Radix's onCheckedChange(checked) signature to native onChange(event).
  const Checkbox = SDK.components.Checkbox || function (props) {
    const { checked, onCheckedChange, className, onClick, ...rest } = props;
    return h("input", Object.assign({
      type: "checkbox",
      checked: !!checked,
      className: className,
      onClick: onClick,
      onChange: function (e) {
        if (onCheckedChange) onCheckedChange(e.target.checked);
      },
    }, rest));
  };

  // useI18n is a hook each component calls locally. Older host dashboards
  // may not expose it yet; fall back to a shim so the bundle still renders
  // English against an older host SDK. English fallback strings live
  // alongside each call site (passed as the third arg of tx()).
  const useI18n = SDK.useI18n || function () { return { t: { kanban: null }, locale: "en" }; };

  // Resolve a translation by dotted path under the kanban namespace
  // (e.g. "columnLabels.triage"); fall back to the English string passed in.
  function tx(t, path, fallback, vars) {
    let node = t && t.kanban;
    if (node) {
      const parts = path.split(".");
      for (let i = 0; i < parts.length; i++) {
        if (node && typeof node === "object" && parts[i] in node) {
          node = node[parts[i]];
        } else { node = null; break; }
      }
    }
    let str = (typeof node === "string") ? node : fallback;
    if (vars) {
      for (const k in vars) {
        str = str.replace(new RegExp("\\{" + k + "\\}", "g"), vars[k]);
      }
    }
    return str;
  }

  // ``fetchJSON`` throws ``Error("<status>: <raw body>")`` on non-2xx, and
  // FastAPI bodies look like ``{"detail":"<message>"}``.  Pull the
  // human-readable message out so banners/toasts don't have to leak HTTP
  // plumbing at the user (e.g. ``409: {"detail":"…"}``).  See #26744.
  function parseApiErrorMessage(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = m ? m[2] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && parsed.detail && typeof parsed.detail.message === "string") {
        return parsed.detail.message;
      }
    } catch (_e) { /* not JSON — fall through to raw body */ }
    return body || raw;
  }

  // Board column display order; any backend status not listed here renders after these.
  const COLUMN_ORDER = ["triage", "todo", "ready", "running", "blocked", "review", "done"];
  // English fallback dictionaries — used when the i18n catalog is missing
  // a key, and as defaults for the get*() helpers below so callers running
  // outside any React component (where there's no `t`) still get sane text.
  const FALLBACK_COLUMN_LABEL = {
    triage: "Triage",
    todo: "Todo",
    ready: "Ready",
    running: "In Progress",
    blocked: "Blocked",
    review: "Review",
    done: "Done",
    archived: "Archived",
  };
  const FALLBACK_COLUMN_HELP = {
    triage: "Raw ideas — a specifier will flesh out the spec",
    todo: "Waiting on dependencies or unassigned",
    ready: "Dependencies satisfied; assign a profile to dispatch",
    running: "Claimed by a worker — in-flight",
    blocked: "Worker asked for human input",
    review: "Implementation complete — awaiting review",
    done: "Completed",
    archived: "Archived",
  };
  const FALLBACK_DESTRUCTIVE = {
    done: "Mark this task as done? The worker's claim is released and dependent children become ready.",
    archived: "Archive this task? It disappears from the default board view.",
    blocked: "Mark this task as blocked? The worker's claim is released.",
  };
  // Pluralized variants used by getDestructiveConfirm() when count > 1.
  // Each entry may use {n} as a placeholder for the count.
  const FALLBACK_DESTRUCTIVE_MANY = {
    done: "Mark {n} tasks as done? The workers' claims are released and dependent children become ready.",
    archived: "Archive {n} tasks? They disappear from the default board view.",
    blocked: "Mark {n} tasks as blocked? The workers' claims are released.",
  };
  const FALLBACK_DIAGNOSTIC_EVENT_LABELS = {
    completion_blocked_hallucination: "⚠ Completion blocked — phantom card ids",
    suspected_hallucinated_references: "⚠ Prose referenced phantom card ids",
  };
  const FALLBACK_TRASH = {
    label: "Trash",
    title: "Drag a card here to permanently delete it",
    confirm: "Permanently delete this task? This cannot be undone.",
    dropHint: "Drop to delete",
  };
  const DIAGNOSTIC_EVENT_KIND_KEYS = {
    completion_blocked_hallucination: "completionBlockedHallucination",
    suspected_hallucinated_references: "suspectedHallucinatedReferences",
  };
  const DESTRUCTIVE_KEYS = {
    done: "confirmDone",
    archived: "confirmArchive",
    blocked: "confirmBlocked",
  };

  function getColumnLabel(t, status) {
    return tx(t, "columnLabels." + status, FALLBACK_COLUMN_LABEL[status] || status);
  }
  function getColumnHelp(t, status) {
    return tx(t, "columnHelp." + status, FALLBACK_COLUMN_HELP[status] || "");
  }
  function getDestructiveConfirm(t, status, count) {
    const key = DESTRUCTIVE_KEYS[status];
    if (!key) return null;
    // For bulk operations, use the *Many variant of the i18n key so the
    // copy pluralizes correctly ("Mark 3 tasks as done?" instead of
    // "Mark this task as done?"). Falls back to the singular English
    // string if a translation for the *Many key isn't shipped.
    if (count && count > 1) {
      const manyKey = key + "Many";
      const manyFallback = FALLBACK_DESTRUCTIVE_MANY[status] || FALLBACK_DESTRUCTIVE[status];
      return tx(t, manyKey, manyFallback, { n: count });
    }
    return tx(t, key, FALLBACK_DESTRUCTIVE[status]);
  }
  function getDiagnosticEventLabel(t, kind) {
    const key = DIAGNOSTIC_EVENT_KIND_KEYS[kind];
    if (!key) return null;
    return tx(t, key, FALLBACK_DIAGNOSTIC_EVENT_LABELS[kind]);
  }

  const COLUMN_DOT = {
    triage: "hermes-kanban-dot-triage",
    todo: "hermes-kanban-dot-todo",
    ready: "hermes-kanban-dot-ready",
    running: "hermes-kanban-dot-running",
    blocked: "hermes-kanban-dot-blocked",
    review: "hermes-kanban-dot-review",
    done: "hermes-kanban-dot-done",
    archived: "hermes-kanban-dot-archived",
  };

  function isDiagnosticEvent(kind) {
    return Object.prototype.hasOwnProperty.call(FALLBACK_DIAGNOSTIC_EVENT_LABELS, kind);
  }

  function phantomIdsFromEvent(ev) {
    if (!ev || !ev.payload) return [];
    const p = ev.payload;
    return p.phantom_cards || p.phantom_refs || [];
  }

  // Helpers for the dialog state machine used by `useKanbanDialogs` below.
  // The dialog API is Promise-based so call sites can preserve their
  // synchronous-ish flow: ``await kanbanDialogs.request(...)`` and then
  // continue with the optimistic UI + PATCH. See #50547.
  function dialogLabelForCount(count, t) {
    return count && count > 1 ? tx(t, "selectedTasks", "{n} selected tasks", { n: count }) : tx(t, "thisTask", "this task");
  }

  /**
   * Hook owning the kanban plugin's modal dialog state. Returns
   *   - `request(req)` — imperative API. Resolves to
   *     `{ confirmed: false }` if the user cancels, or
   *     `{ confirmed: true, summary?: string }` if they confirm.
   *   - `dialogState` — current dialog descriptor for rendering, or null.
   *   - `dialogProps` — onConfirm/onCancel handlers bound to the current
   *     request.
   *
   * `req` shapes:
   *   { kind: "confirm", title, description, confirmLabel, destructive }
   *
   * The "completion" kind (textarea prompt) is deferred: the host's
   * ConfirmDialog hardcodes onClick → unmount, preventing validation-
   * state retention. See KanbanDialogs doc comment.
   */
  function useKanbanDialogs(t) {
    const [dialogState, setDialogState] = React.useState(null);
    const resolverRef = React.useRef(null);

    const request = React.useCallback(function (req) {
      return new Promise(function (resolve) {
        resolverRef.current = resolve;
        setDialogState(req);
      });
    }, []);

    const close = React.useCallback(function (confirmed, extras) {
      const resolve = resolverRef.current;
      resolverRef.current = null;
      setDialogState(null);
      if (resolve) {
        resolve(Object.assign({ confirmed: confirmed }, extras || {}));
      }
    }, []);

    const onConfirm = React.useCallback(function (maybeSummary) {
      close(true, maybeSummary ? { summary: maybeSummary } : null);
    }, [close]);
    const onCancel = React.useCallback(function () { close(false, null); }, [close]);

    // Wrap the ConfirmDialog props so call sites can hand them straight
    // to <ConfirmDialog {...props} />. Title/description/confirmLabel are
    // sourced from the current dialog state. For "completion" the dialog
    // body (textarea + dual-validation) is rendered separately.
    const dialogProps = React.useMemo(function () {
      if (!dialogState) return null;
      return {
        open: true,
        title: dialogState.title || "",
        description: dialogState.description,
        confirmLabel: dialogState.confirmLabel || (dialogState.kind === "completion"
          ? tx(t, "confirm", "Confirm")
          : tx(t, "ok", "OK")),
        destructive: !!dialogState.destructive,
        onConfirm: function () { onConfirm(); },
        onCancel: onCancel,
      };
    }, [dialogState, t, onConfirm, onCancel]);

    return { dialogState: dialogState, dialogProps: dialogProps, request: request };
  }

  const API = "/api/plugins/kanban";
  const MIME_TASK = "text/x-hermes-task";

  // Docs link — surfaced as a `?` icon next to the board switcher and as
  // `title=` hints on unlabelled controls. Kept in one place so rebrands or
  // path changes are a single edit.
  const DOCS_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban";
  const DOCS_TUTORIAL_URL = "https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban-tutorial";

  // localStorage key for the user's selected board. Independent of the
  // CLI's on-disk ``<root>/kanban/current`` pointer so browser users
  // can inspect any board without shifting the CLI's active board out
  // from under a terminal they left open.
  const LS_BOARD_KEY = "hermes.kanban.selectedBoard";

  function readSelectedBoard() {
    try {
      const v = window.localStorage.getItem(LS_BOARD_KEY);
      return (v || "").trim() || null;
    } catch (_e) { return null; }
  }

  function writeSelectedBoard(slug) {
    try {
      // Persist the user's dashboard-side board pin even for "default".
      // Previously this stripped "default" to keep localStorage empty,
      // but the fetch layer read that absence as "no opinion" and fell
      // through to the server-side ``current`` file — which the board
      // switcher also writes. Result: selecting the default tab after
      // creating a new board with "switch" checked showed the new
      // board's (wrong) data because the URL omitted ``?board=`` and
      // the backend happily returned whichever board was "current".
      // Persisting every selection keeps the dashboard's board opinion
      // independent of the CLI's active board, which was the original
      // design intent. Regression: #20879.
      if (slug) window.localStorage.setItem(LS_BOARD_KEY, slug);
      else window.localStorage.removeItem(LS_BOARD_KEY);
    } catch (_e) { /* ignore quota / private mode */ }
  }

  function withBoard(url, board) {
    // Always append ?board=<slug> when we have one picked — including
    // "default". Omitting the param would fall through to the backend's
    // resolution chain (env var → ``current`` file → default), which
    // means the dashboard's tab selection gets silently overridden by
    // whatever board the CLI or "switch" checkbox last activated.
    // Regression: #20879.
    if (!board) return url;
    const sep = url.indexOf("?") >= 0 ? "&" : "?";
    return `${url}${sep}board=${encodeURIComponent(board)}`;
  }

  // -------------------------------------------------------------------------
  // Read-only shared-evidence bridge (/evidence/*) — the IIFE consumer.
  //
  // These helpers read the EVO host's shared board evidence through the
  // released atlas-kanban-call helper, proxied by the plugin backend. Every
  // read is gated on /evidence/context reporting that the SELECTED board's
  // local DB IS the same physical EVO database: the bridge always reads EVO,
  // so attaching EVO badges to a local board that merely shares a slug would
  // be a lie. When alignment is false (or unresolved) the original local UI
  // renders unchanged, with no EVO badges, downloads or paging — but the
  // mismatch is VISIBLE, never silent.
  //
  // The bridge envelope is {state: PASS|FAIL|UNKNOWN, evidence: <helper
  // data>, reason?, remedy?, board, observed_at?, helper:{tool,
  // execution_host}, request, timing}. ``evidence`` carries the helper's
  // per-tool ``data`` directly (no nested helper envelope). A non-PASS
  // envelope is never presented as green.
  // -------------------------------------------------------------------------

  const EVIDENCE_STATE_LABEL = {
    running: "Running",
    stopped: "Stopped",
    unknown: "Unknown",
    unavailable: "Unavailable",
  };
  const EVIDENCE_STATE_TONE = {
    running: "#34d399",
    stopped: "var(--ui-text-tertiary, #9ca3af)",
    unknown: "#fbbf24",
    unavailable: "var(--ui-text-quaternary, #6b7280)",
  };
  // Seconds after observed_at before evidence is shown as stale (amber, then
  // red). The poll interval (15s) sits comfortably under amber so a healthy
  // board never shows stale; a dead helper drifts into amber then red.
  const EVIDENCE_STALE_AMBER_S = 60;
  const EVIDENCE_STALE_RED_S = 300;
  const EVIDENCE_POLL_MS = 15000;
  // R1: identity of the EXACT JS bytes this IIFE was served in. The Hermes
  // asset door prepends a wrapper statement to the served response that
  // publishes {sha256, bytes} on the per-script registry BEFORE this code
  // runs; capturing it here (at IIFE execution, not later) binds the
  // identity to the response that actually executed. No wrapper (older
  // backend, plain file serving) means honest UNKNOWN — never a hardcoded
  // stamp and never a re-read of mutable current bytes.
  const LOADED_ASSET_IDENTITY = (function () {
    try {
      const reg = globalThis.__HERMES_PLUGIN_ASSET_ID__;
      const m = reg && reg["kanban/dist/index.js"];
      if (m && typeof m.sha256 === "string" && /^[a-f0-9]{64}$/.test(m.sha256)) {
        return {
          state: "PASS",
          revision: m.sha256.slice(0, 12),
          source: "served asset bytes (sha256 of the original dist/index.js payload; wrapper bytes excluded)",
          bytes: typeof m.bytes === "number" ? m.bytes : null,
        };
      }
    } catch (_e) { /* registry absent — UNKNOWN below */ }
    return {
      state: "UNKNOWN",
      revision: null,
      source: "served asset digest unavailable (older backend or direct file load); upgrade the Hermes dashboard server to expose the loaded asset identity",
      bytes: null,
    };
  })();
  // R3: repair preview actions, text only, keyed by readiness check name.
  // Mirrors the plugin backend's fixed table; never executable shell.
  const REPAIR_PREVIEW_ACTIONS = {
    board_permission: "Add the board to the server's ATLAS_KANBAN_WRITE_BOARDS and restart the dashboard process.",
    profile_exists: "Create the missing profile with `hermes profile add` before releasing this card.",
    workspace_exists: "Recreate or re-provision the card's workspace directory; readiness never invents a path.",
    expected_revision: "Commit or reset the card workspace so git HEAD matches the commissioned revision.",
    python_interpreter: "Provision the workspace .venv (python -m venv .venv) with a supported interpreter.",
    required_modules: "Install the workspace's declared dependencies into its .venv.",
    context_files: "Restore the missing brief/context files named by the card's commission.",
    ram_available: "Free memory on the EVO host before releasing; readiness reports the real available figure.",
    parents: "Complete or rework the card's incomplete parent dependencies before release.",
    model: "Resolve the model/provider configuration on the card (check_model was requested).",
  };
  const REPAIR_PREVIEW_DEFAULT = "Inspect the failed readiness check on the EVO host; no automatic repair exists for it.";
  const EVIDENCE_PAGE_CARD_LIMIT = 100;
  // Per-resource page size for the drawer's bounded RUNS/EVENTS/ATTACHMENTS
  // reads through /evidence/page (K10). Kept well under the 200 max.
  const EVIDENCE_PAGE_RESOURCE_LIMIT = 50;

  // Build a query string from an object, skipping null/undefined/empty values.
  function evQuery(params) {
    const qs = new URLSearchParams();
    Object.keys(params).forEach(function (k) {
      const v = params[k];
      if (v == null || v === "") return;
      qs.set(k, String(v));
    });
    return qs.toString();
  }

  // All evidence fetchers return a Promise that resolves to a normalised
  // envelope. A network/HTTP failure (endpoint absent on an older backend,
  // session error) resolves to an UNKNOWN envelope with a safe reason and
  // remedy — never throws, so callers can treat it uniformly.
  function evidenceEnvelope(promise) {
    return promise.then(function (res) {
      return (res && typeof res === "object") ? res : { state: "UNKNOWN", reason: "empty evidence response" };
    }).catch(function (err) {
      return {
        state: "UNKNOWN",
        reason: parseApiErrorMessage(err) || "evidence endpoint unreachable",
        remedy: "Worker evidence is unavailable on this server.",
      };
    });
  }

  function fetchEvidenceContext(board) {
    return evidenceEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/context`, board)));
  }

  function fetchEvidenceSnapshot(board, status, cardLimit, cursor) {
    const qs = evQuery({
      status: status || "all",
      card_limit: cardLimit || EVIDENCE_PAGE_CARD_LIMIT,
      cursor: cursor || undefined,
    });
    const url = qs ? `${API}/evidence/snapshot?${qs}` : `${API}/evidence/snapshot`;
    return evidenceEnvelope(SDK.fetchJSON(withBoard(url, board)));
  }

  function fetchEvidencePage(board, resource, card, status, limit, cursor) {
    const qs = evQuery({
      resource: resource,
      card: card || undefined,
      status: status || undefined,
      limit: limit || undefined,
      cursor: cursor || undefined,
    });
    return evidenceEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/page?${qs}`, board)));
  }

  function fetchEvidenceWorker(board, card) {
    return evidenceEnvelope(SDK.fetchJSON(
      withBoard(`${API}/evidence/worker?card=${encodeURIComponent(card)}`, board)));
  }

  function fetchEvidenceCard(board, card) {
    return evidenceEnvelope(SDK.fetchJSON(
      withBoard(`${API}/evidence/card?card=${encodeURIComponent(card)}`, board)));
  }

  // One observation is a live running worker only when EVERY positive check
  // passes: state PASS, a process present now, and both the workspace and the
  // run-start match the card. process_present alone is NOT running (the older
  // React resolver's bug).
  function resolveObservationState(o) {
    if (!o || typeof o !== "object") return "unknown";
    if (
      o.state === "PASS" &&
      o.process_present === true &&
      o.workspace_matches === true &&
      o.run_start_matches === true
    ) {
      return "running";
    }
    return "unknown";
  }

  // Reduce a /evidence/worker helper ``data`` payload to one honest state.
  //
  //   running — a live observation passing all four positive checks.
  //   stopped — aggregate.complete === true AND aggregate.running === 0 AND
  //             aggregate.unknown === 0 AND aggregate.stopped > 0. A
  //             completion_records-only aggregate is bookkeeping, not
  //             stopped-worker evidence, so it is never Stopped.
  //   unknown — a missing or incomplete observation. Never stopped.
  //
  // The real aggregate lives NESTED under data.aggregate; the older React
  // resolver read a top-level running/complete/unknown the helper never
  // emits, which is why its Stopped/Running answers were wrong.
  function resolveWorkerState(data) {
    if (!data || typeof data !== "object") return "unknown";
    const obs = Array.isArray(data.observations) ? data.observations : [];
    for (let i = 0; i < obs.length; i++) {
      if (resolveObservationState(obs[i]) === "running") return "running";
    }
    const agg = (data.aggregate && typeof data.aggregate === "object") ? data.aggregate : null;
    if (agg) {
      const running = typeof agg.running === "number" ? agg.running : 0;
      const unknown = typeof agg.unknown === "number" ? agg.unknown : 0;
      const stopped = typeof agg.stopped === "number" ? agg.stopped : 0;
      if (agg.complete === true && running === 0 && unknown === 0 && stopped > 0) {
        return "stopped";
      }
    }
    return "unknown";
  }

  // Map a snapshot's worker_observations (running run records on the current
  // page) to {cardId: {state, observation}}.
  function buildWorkerStateMap(workerObservations) {
    const map = {};
    (workerObservations || []).forEach(function (o) {
      if (!o || typeof o !== "object") return;
      const id = o.task_id || o.card_id || o.id;
      if (!id) return;
      map[id] = { state: resolveObservationState(o), observation: o };
    });
    return map;
  }

  // Freshness: the envelope's validated observed_at (epoch seconds) is the
  // single source of truth for how old the evidence is.
  function evidenceObservedAt(envelope) {
    if (!envelope || typeof envelope !== "object") return null;
    if (typeof envelope.observed_at === "number") return envelope.observed_at;
    const data = envelope.evidence;
    if (data && typeof data.observed_at === "number") return data.observed_at;
    return null;
  }

  function evidenceAgeSeconds(envelope) {
    const at = evidenceObservedAt(envelope);
    if (at == null) return null;
    return Math.max(0, (Date.now() / 1000) - at);
  }

  function evidenceStaleClass(age) {
    if (age == null) return "";
    if (age >= EVIDENCE_STALE_RED_S) return "hermes-kanban-evidence--stale-red";
    if (age >= EVIDENCE_STALE_AMBER_S) return "hermes-kanban-evidence--stale-amber";
    return "";
  }

  // -------------------------------------------------------------------------
  // useKanbanEvidence — one snapshot per aligned refresh, generation-guarded.
  //
  // Drives: identity alignment, the bounded snapshot (cards + counts + load
  // more), the per-card worker map, and evidence freshness. Every async
  // response is tagged with the generation at request time and dropped when a
  // board/filter switch has advanced the generation — so a slow response from
  // the previous board can never overwrite the new board's evidence.
  // -------------------------------------------------------------------------
  function useKanbanEvidence(board) {
    const [context, setContext] = useState(null);   // raw /evidence/context payload
    const [ctxErr, setCtxErr] = useState(false);    // context fetch failed outright
    const [snapshot, setSnapshot] = useState(null); // latest snapshot envelope
    const [workerMap, setWorkerMap] = useState({});
    const [counts, setCounts] = useState(null);
    const [observedAt, setObservedAt] = useState(null);
    const [cards, setCards] = useState([]);          // stable bounded pages
    const [cursor, setCursor] = useState(null);
    const [hasMore, setHasMore] = useState(false);
    const [omitted, setOmitted] = useState(null);
    const [loadingMore, setLoadingMore] = useState(false);
    const genRef = useRef(0);
    const pollRef = useRef(null);
    const cardsRef = useRef([]);      // mirrors the `cards` state (for dedupe)
    const firstPageLenRef = useRef(0); // page-0 slice length (poll replaces it)
    const pagingTotalRef = useRef(null); // R10: total the appended pages were loaded under
    const pagingFirstIdsRef = useRef(null); // R10: first-page card ids of the load generation

    const aligned = !!(context && context.aligned === true);
    // R10: stable evolving pagination. Appended pages carry the generation
    // (observed_at + total) they were loaded under; when a page-0 refresh
    // shows a changed total while appended pages exist, the loaded list is a
    // mixture of generations and the UI offers RESTART rather than blending
    // stale cards silently.
    const [pagingDrift, setPagingDrift] = useState(null);

    const reset = useCallback(function () {
      genRef.current += 1;
      setSnapshot(null);
      setWorkerMap({});
      setCounts(null);
      setObservedAt(null);
      setCards([]);
      cardsRef.current = [];
      firstPageLenRef.current = 0;
      setCursor(null);
      setHasMore(false);
      setOmitted(null);
      setPagingDrift(null);
      pagingTotalRef.current = null;
      pagingFirstIdsRef.current = null;
    }, []);

    // Apply a snapshot envelope. page 0 (append=false) seeds cards only when
    // nothing is loaded yet; on a poll it refreshes the counts/worker map/
    // freshness but keeps the already-loaded pages stable (they must not
    // silently vanish on polling). append=true merges the next page for Load
    // more, deduped by id.
    const applySnapshot = useCallback(function (env, opts) {
      if (!env || typeof env !== "object") return;
      setSnapshot(env);
      if (env.state !== "PASS") return;
      const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
      setObservedAt(evidenceObservedAt(env));
      if (data.counts && typeof data.counts === "object") setCounts(data.counts);
      const pageCards = Array.isArray(data.cards) ? data.cards : [];
      const omitNum = typeof data.omitted === "number" ? data.omitted
        : (data.counts && typeof data.counts.omitted === "number" ? data.counts.omitted : null);

      if (opts && opts.append) {
        // Load-more page: append (deduped), advance the paging state.
        const seen = {};
        const merged = cardsRef.current.concat(pageCards).filter(function (c) {
          if (!c || seen[c.id]) return false;
          seen[c.id] = true;
          return true;
        });
        cardsRef.current = merged;
        setCards(merged);
        setHasMore(data.has_more === true);
        if (data.next_cursor != null) setCursor(data.next_cursor);
        else if (!data.has_more) setCursor(null);
        setOmitted(omitNum);
      } else {
        // Page-0 refresh: REPLACE the worker map (a removed observation must
        // not linger as a stale RUNNING with a fresh timestamp) and replace
        // the page-0 card slice (title/status changes become visible), while
        // keeping appended pages stable. Paging state is set only on the
        // initial seed, so a poll never resets cursor/omitted after Load more.
        setWorkerMap(buildWorkerStateMap(data.worker_observations));
        const isInitial = firstPageLenRef.current === 0 && cardsRef.current.length === 0;
        const appended = firstPageLenRef.current > 0
          ? cardsRef.current.slice(firstPageLenRef.current)
          : [];
        const next = pageCards.concat(appended);
        firstPageLenRef.current = pageCards.length;
        cardsRef.current = next;
        setCards(next);
        if (isInitial) {
          setHasMore(data.has_more === true);
          if (data.next_cursor != null) setCursor(data.next_cursor);
          else if (!data.has_more) setCursor(null);
          setOmitted(omitNum);
          pagingFirstIdsRef.current = pageCards.map(function (c) { return c && c.id; });
        } else if (appended.length > 0) {
          // R10: a refresh under appended pages. The loaded list is a
          // mixture of generations whenever the board changed under paging:
          // a different total, OR the same total with different card ids
          // (an insert+delete swap keeps the count constant). In both cases
          // say so and offer a restart instead of blending silently.
          const total = (data.counts && typeof data.counts.total === "number")
            ? data.counts.total : null;
          const loaded = pageCards.length + appended.length;
          const countChanged = total != null && total !== loaded && pagingTotalRef.current != null
            && total !== pagingTotalRef.current;
          const knownIds = {};
          let k = cardsRef.current.length;
          while (k--) knownIds[cardsRef.current[k] && cardsRef.current[k].id] = true;
          let compositionChanged = false;
          if (pagingFirstIdsRef.current) {
            // The first page's previous id set vs the fresh one: any id
            // appearing or disappearing with the SAME total is a swap.
            const prevIds = pagingFirstIdsRef.current;
            if (pageCards.length === prevIds.length) {
              const prevSet = {};
              for (let j = 0; j < prevIds.length; j++) prevSet[prevIds[j]] = true;
              for (let j = 0; j < pageCards.length; j++) {
                const id = pageCards[j] && pageCards[j].id;
                if (!prevSet[id]) { compositionChanged = true; break; }
              }
            }
          }
          if (countChanged || compositionChanged) {
            setPagingDrift({
              loaded: loaded,
              total: total,
              previousTotal: pagingTotalRef.current,
              sameCount: compositionChanged && !countChanged,
            });
          }
          pagingTotalRef.current = total;
          pagingFirstIdsRef.current = pageCards.map(function (c) { return c && c.id; });
        }
      }
    }, []);

    // (Re)load /evidence/context whenever the board changes.
    useEffect(function () {
      let alive = true;
      reset();
      setContext(null);
      setCtxErr(false);
      const gen = genRef.current;
      fetchEvidenceContext(board).then(function (ctx) {
        if (!alive || gen !== genRef.current) return;
        setContext(ctx);
      }).catch(function () {
        if (!alive || gen !== genRef.current) return;
        setCtxErr(true);
      });
      return function () { alive = false; };
    }, [board, reset]);

    // Single snapshot refresh (page 0), generation-guarded. This is the one
    // owning evidence data path: the poll interval, the WebSocket event
    // callback (via loadBoard) and board actions all route through it when
    // the board is aligned.
    const refresh = useCallback(function () {
      const gen = genRef.current;
      return fetchEvidenceSnapshot(board, "all", EVIDENCE_PAGE_CARD_LIMIT, null).then(function (env) {
        if (gen !== genRef.current) return;
        applySnapshot(env, { append: false });
      }).catch(function (err) {
        if (gen !== genRef.current) return;
        setSnapshot({ state: "UNKNOWN", reason: parseApiErrorMessage(err) });
      });
    }, [board, applySnapshot]);

    // Poll the snapshot only when aligned. One snapshot per refresh, never a
    // local /board plus snapshot. The initial load is performed by loadBoard
    // (the single owning data path); this effect only schedules the interval.
    useEffect(function () {
      if (!aligned) return undefined;
      pollRef.current = setInterval(refresh, EVIDENCE_POLL_MS);
      return function () {
        if (pollRef.current) clearInterval(pollRef.current);
      };
    }, [aligned, refresh]);

    const loadMore = useCallback(function () {
      if (loadingMore || !aligned || !hasMore || !cursor) return Promise.resolve();
      setLoadingMore(true);
      const gen = genRef.current;
      return fetchEvidenceSnapshot(board, "all", EVIDENCE_PAGE_CARD_LIMIT, cursor).then(function (env) {
        if (gen !== genRef.current) return; // late response: board switched
        setLoadingMore(false);
        applySnapshot(env, { append: true });
      }).catch(function () {
        if (gen !== genRef.current) return;
        setLoadingMore(false);
      });
    }, [loadingMore, aligned, hasMore, cursor, board, applySnapshot]);

    // R10: restart paging — drop appended pages, return to a clean page-0
    // view of the CURRENT generation. Explicit, never automatic, because
    // silently dropping cards the user can see is worse than offering the
    // restart.
    const restartPaging = useCallback(function () {
      cardsRef.current = [];
      firstPageLenRef.current = 0;
      setCards([]);
      setCursor(null);
      setHasMore(false);
      setOmitted(null);
      setPagingDrift(null);
      pagingTotalRef.current = null;
      pagingFirstIdsRef.current = null;
      return refresh();
    }, [refresh]);

    return {
      aligned: aligned,
      context: context,
      ctxErr: ctxErr,
      snapshot: snapshot,
      workerMap: workerMap,
      counts: counts,
      observedAt: observedAt,
      cards: cards,
      cursor: cursor,
      hasMore: hasMore,
      omitted: omitted,
      loadingMore: loadingMore,
      loadMore: loadMore,
      refresh: refresh,
      pagingDrift: pagingDrift,
      restartPaging: restartPaging,
    };
  }

  // -------------------------------------------------------------------------
  // useEvidenceResourcePage — one bounded /evidence/page cursor per drawer
  // resource (runs | events | attachments), K10.
  //
  // Each resource owns its own stable cursor, omissions rollup and Load-more
  // affordance. Load more APPENDS visible rows (deduped by id) instead of
  // only flipping a flag, so the drawer grows real details. Generation is
  // bumped on board/card/resource change so a slow page for the previous card
  // can never leak into the current one. When ``enabled`` is false (board not
  // aligned) the hook clears itself and callers fall back to the legacy
  // canonical detail read (runs/events/attachments off /tasks/:id).
  // -------------------------------------------------------------------------
  function useEvidenceResourcePage(boardSlug, cardId, resource, enabled) {
    const [items, setItems] = useState([]);
    const [envelope, setEnvelope] = useState(null);
    const [loading, setLoading] = useState(false);
    const [loadingMore, setLoadingMore] = useState(false);
    const [hasMore, setHasMore] = useState(false);
    const [cursor, setCursor] = useState(null);
    const [omitted, setOmitted] = useState(null);
    const [total, setTotal] = useState(null);
    const [returned, setReturned] = useState(null);
    const [error, setError] = useState(null);
    const genRef = useRef(0);
    const itemsRef = useRef([]);

    const loadFirst = useCallback(function () {
      genRef.current += 1;
      const gen = genRef.current;
      setLoading(true);
      setError(null);
      setHasMore(false);
      setCursor(null);
      setOmitted(null);
      setTotal(null);
      setReturned(null);
      return fetchEvidencePage(boardSlug, resource, cardId, null, EVIDENCE_PAGE_RESOURCE_LIMIT, null).then(function (env) {
        if (gen !== genRef.current) return;
        setEnvelope(env);
        setLoading(false);
        if (!env || env.state !== "PASS") {
          itemsRef.current = [];
          setItems([]);
          setError((env && env.reason) || "evidence unavailable");
          return;
        }
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const pageItems = Array.isArray(data.items) ? data.items : [];
        itemsRef.current = pageItems;
        setItems(pageItems);
        setHasMore(data.has_more === true);
        setCursor(data.next_cursor != null ? data.next_cursor : null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
        setTotal(typeof data.total === "number" ? data.total : null);
        setReturned(typeof data.returned === "number" ? data.returned : null);
      }).catch(function (e) {
        if (gen !== genRef.current) return;
        setLoading(false);
        setError(String((e && e.message) || e));
      });
    }, [boardSlug, cardId, resource]);

    const loadMore = useCallback(function () {
      if (loadingMore || !hasMore || !cursor) return Promise.resolve();
      setLoadingMore(true);
      const gen = genRef.current;
      return fetchEvidencePage(boardSlug, resource, cardId, null, EVIDENCE_PAGE_RESOURCE_LIMIT, cursor).then(function (env) {
        if (gen !== genRef.current) return; // late page: card/board switched
        setLoadingMore(false);
        setEnvelope(env);
        if (!env || env.state !== "PASS") {
          setError((env && env.reason) || "evidence unavailable");
          return;
        }
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const pageItems = Array.isArray(data.items) ? data.items : [];
        const seen = {};
        const merged = itemsRef.current.concat(pageItems).filter(function (it) {
          if (!it || it.id == null) return false;
          if (seen[it.id]) return false;
          seen[it.id] = true;
          return true;
        });
        itemsRef.current = merged;
        setItems(merged);
        setHasMore(data.has_more === true);
        if (data.next_cursor != null) setCursor(data.next_cursor);
        else if (!data.has_more) setCursor(null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
        setTotal(typeof data.total === "number" ? data.total : null);
        setReturned(typeof data.returned === "number" ? data.returned : null);
      }).catch(function (e) {
        if (gen !== genRef.current) return;
        setLoadingMore(false);
        setError(String((e && e.message) || e));
      });
    }, [loadingMore, hasMore, cursor, boardSlug, cardId, resource]);

    useEffect(function () {
      if (!enabled) {
        itemsRef.current = [];
        setItems([]);
        setEnvelope(null);
        setLoading(false);
        setLoadingMore(false);
        setHasMore(false);
        setCursor(null);
        setOmitted(null);
        setTotal(null);
        setReturned(null);
        setError(null);
        return undefined;
      }
      loadFirst();
      return undefined;
    }, [enabled, boardSlug, cardId, resource, loadFirst]);

    return {
      items: items,
      envelope: envelope,
      loading: loading,
      loadingMore: loadingMore,
      hasMore: hasMore,
      cursor: cursor,
      omitted: omitted,
      total: total,
      returned: returned,
      error: error,
      loadMore: loadMore,
    };
  }

  // Shared Load-more affordance for the drawer's bounded evidence pages
  // (RUNS / EVENTS / ATTACHMENTS). Shows the omissions rollup from the
  // helper so a partial page is never silently presented as complete.
  function EvidenceLoadMoreButton(props) {
    if (!props.hasMore) return null;
    return h("button", {
      type: "button",
      className: "hermes-kanban-edit-link",
      "data-evidence-resource": props.resource || undefined,
      disabled: !!props.loadingMore,
      style: { marginTop: "4px" },
      onClick: props.onLoadMore,
    }, props.loadingMore
      ? "Loading…"
      : "Load more" + (props.omitted != null ? " (" + props.omitted + " omitted)" : ""));
  }

  // Compact per-card worker-evidence badge (K3). ``state`` is one of the
  // four EVIDENCE_STATE_LABEL keys; tone rendered inline so no stylesheet
  // change is required.
  function EvidenceBadge(props) {
    const state = props.state || "unknown";
    const tone = EVIDENCE_STATE_TONE[state] || EVIDENCE_STATE_TONE.unknown;
    const label = EVIDENCE_STATE_LABEL[state] || "Unknown";
    return h("span", {
      className: "hermes-kanban-evidence-badge",
      title: props.title || label,
      style: { color: tone },
    },
      h("span", { className: "hermes-kanban-evidence-dot", style: { backgroundColor: tone } }),
      label);
  }

  // Board-level evidence strip (K3/K4/K10): identity alignment, freshness,
  // snapshot counts, the bounded card pages with Load more, and the visible
  // stale / helper-unavailable / alignment-remedy surfaces. Rendered inside
  // the existing KanbanPage flow (between toolbar and board), not a second UI.
  function EvidenceBanner(props) {
    const ev = props.evidence;
    if (!ev) return null;
    const bar = { padding: "6px 10px", borderRadius: "6px", fontSize: "12px", lineHeight: "1.5" };

    if (ev.ctxErr) {
      return h("div", { className: "hermes-kanban-evidence-banner", style: Object.assign({}, bar, { background: "var(--muted, rgba(0,0,0,0.04))" }) },
        h("span", { style: { color: EVIDENCE_STATE_TONE.unavailable } },
          "Worker evidence unavailable: could not reach the evidence endpoint."),
        h("span", { style: { color: "var(--muted-foreground, #6b7280)", marginLeft: "6px" } },
          "Choose the EVO connection to see worker evidence."));
    }

    if (ev.context && ev.context.aligned !== true) {
      return h("div", { className: "hermes-kanban-evidence-banner", style: Object.assign({}, bar, { background: "var(--muted, rgba(0,0,0,0.04))" }) },
        h("span", { style: { color: EVIDENCE_STATE_TONE.unavailable } },
          "This board is not the EVO evidence database."),
        h("span", { style: { color: "var(--muted-foreground, #6b7280)", marginLeft: "6px" } },
          "Choose the EVO connection to see worker evidence."));
    }

    if (!ev.aligned && !ev.ctxErr) {
      return h("div", { className: "hermes-kanban-evidence-banner", style: Object.assign({}, bar, { background: "var(--muted, rgba(0,0,0,0.04))" }) },
        "Checking EVO connection\u2026");
    }

    if (!ev.snapshot) {
      return h("div", { className: "hermes-kanban-evidence-banner", style: Object.assign({}, bar, { background: "var(--muted, rgba(0,0,0,0.04))" }) },
        "Loading evidence\u2026");
    }

    const age = evidenceAgeSeconds(ev.snapshot);
    const staleCls = evidenceStaleClass(age);

    if (ev.snapshot.state !== "PASS") {
      const reason = ev.snapshot.reason || "helper reported " + ev.snapshot.state;
      return h("div", { className: "hermes-kanban-evidence-banner", style: Object.assign({}, bar, { background: "var(--muted, rgba(0,0,0,0.04))" }) },
        h("span", { style: { color: EVIDENCE_STATE_TONE.unavailable, fontWeight: "600" } },
          "Worker evidence " + EVIDENCE_STATE_LABEL.unavailable.toLowerCase() + ":"),
        h("span", { style: { marginLeft: "6px" } }, reason),
        ev.snapshot.remedy
          ? h("span", { style: { color: "var(--muted-foreground, #6b7280)", marginLeft: "6px" } }, ev.snapshot.remedy)
          : null);
    }

    const c = ev.counts || {};
    const total = typeof c.total === "number" ? c.total : null;
    const inPage = typeof c.in_page === "number" ? c.in_page : null;
    const omitted = ev.omitted;
    const byStatus = c.by_status || {};
    const statusChips = Object.keys(byStatus).map(function (k) {
      return k + " " + byStatus[k];
    }).join(" \u00b7 ");

    // Compact strip only: identity, counts, freshness and Load more. The
    // actual cards (with per-card worker badges) live in BoardColumns, driven
    // by the same snapshot. A per-card list here would duplicate the board and
    // push it below the fold, so it is intentionally not rendered.
    return h("div", {
      className: "hermes-kanban-evidence-banner",
      style: Object.assign({}, bar, {
        background: "var(--muted, rgba(0,0,0,0.04))",
        display: "flex",
        flexDirection: "column",
        gap: "3px",
      }),
    },
      h("div", { style: { display: "flex", alignItems: "center", flexWrap: "wrap" } },
        h("span", { style: { fontWeight: "600" } }, "EVO evidence"),
        h("span", { style: { color: "var(--muted-foreground, #6b7280)", marginLeft: "8px" } },
          (total != null ? "total " + total : "") +
          (inPage != null ? " \u00b7 in page " + inPage : "") +
          (omitted != null ? " \u00b7 omitted " + omitted : "") +
          (statusChips ? " \u00b7 " + statusChips : "")),
        h("span", {
          className: staleCls ? "hermes-kanban-evidence-stale " + staleCls : "hermes-kanban-evidence-stale",
          title: age != null ? "evidence observed " + Math.round(age) + "s ago" : "freshness unknown",
          style: { color: staleCls ? EVIDENCE_STATE_TONE.unknown : EVIDENCE_STATE_TONE.running, marginLeft: "8px" },
        }, age != null ? "observed " + Math.round(age) + "s ago" : "freshness unknown"),
        ev.hasMore
          ? h("button", {
              type: "button",
              className: "hermes-kanban-edit-link",
              disabled: ev.loadingMore,
              style: { marginLeft: "12px" },
              onClick: function () { ev.loadMore(); },
            }, ev.loadingMore ? "Loading\u2026" : "Load more (" + (omitted != null ? omitted : "") + " omitted)")
          : null),
      ev.pagingDrift
        ? h("div", {
            className: "hermes-kanban-evidence-drift",
            "data-evidence-paging-drift": "true",
            "data-paging-drift-same-count": ev.pagingDrift.sameCount ? "true" : undefined,
            style: { color: EVIDENCE_STATE_TONE.unknown },
          },
          ev.pagingDrift.sameCount
            ? "Board changed under paging with the same total (" + ev.pagingDrift.total +
              "; the list may now mix card generations from a swap)."
            : "Board changed under paging (loaded " + ev.pagingDrift.loaded +
              " of a new total " + ev.pagingDrift.total + "; previous total " +
              ev.pagingDrift.previousTotal + "). The list may mix generations.",
          h("button", {
            type: "button",
            className: "hermes-kanban-edit-link",
            "data-evidence-paging-restart": "true",
            style: { marginLeft: "8px" },
            onClick: function () { if (ev.restartPaging) ev.restartPaging(); },
          }, "Restart paging"))
        : null);
  }

  // Drawer worker-evidence panel (K9): reads /evidence/worker for the open
  // card, gated on alignment. Renders the resolved state plus the raw
  // observations and the aggregate block, all readable, never a bare green.
  function WorkerEvidenceSection(props) {
    const [envelope, setEnvelope] = useState(null);
    const [loading, setLoading] = useState(true);
    useEffect(function () {
      let alive = true;
      setLoading(true);
      fetchEvidenceWorker(props.boardSlug, props.cardId).then(function (env) {
        if (!alive) return;
        setEnvelope(env);
        setLoading(false);
      });
      return function () { alive = false; };
    }, [props.boardSlug, props.cardId]);

    const head = "Worker evidence";

    if (loading) {
      return h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" }, head),
        h("div", { className: "text-xs text-muted-foreground" }, "Loading evidence\u2026"));
    }

    if (!envelope || envelope.state !== "PASS") {
      const reason = (envelope && envelope.reason) || "Evidence is unavailable for this card.";
      return h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" }, head),
        h("div", { className: "text-xs" },
          h("span", { style: { color: EVIDENCE_STATE_TONE.unavailable, fontWeight: "600" } },
            EVIDENCE_STATE_LABEL.unavailable + ":"),
          h("span", { style: { marginLeft: "6px", color: "var(--muted-foreground, #6b7280)" } }, reason)),
        envelope && envelope.remedy
          ? h("div", { className: "text-xs text-muted-foreground" }, envelope.remedy)
          : null);
    }

    const data = envelope.evidence || {};
    const state = resolveWorkerState(data);
    const tone = EVIDENCE_STATE_TONE[state];
    const observations = Array.isArray(data.observations) ? data.observations : [];
    const agg = data.aggregate;

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" }, head),
      h("div", { className: "flex items-center gap-2 text-sm" },
        h("span", { className: "hermes-kanban-evidence-dot", style: { backgroundColor: tone } }),
        h("span", { style: { color: tone, fontWeight: "600" } }, EVIDENCE_STATE_LABEL[state]),
        evidenceObservedAt(envelope) != null
          ? h("span", { className: "text-xs text-muted-foreground" },
              "observed " + timeAgo(evidenceObservedAt(envelope)))
          : null),
      observations.length > 0
        ? observations.map(function (o, i) {
            const oState = resolveObservationState(o);
            return h("div", { key: o.run_id || o.task_id || i, className: "text-xs flex items-baseline gap-2 py-0.5" },
              h("span", { className: "hermes-kanban-evidence-dot",
                style: { backgroundColor: EVIDENCE_STATE_TONE[oState] } }),
              h("span", { style: { color: EVIDENCE_STATE_TONE[oState] } },
                (o.state || "unknown") + (o.classification ? " \u00b7 " + o.classification : "")),
              o.reason
                ? h("span", { className: "text-muted-foreground", style: { wordBreak: "break-word" } }, o.reason)
                : null);
          })
        : h("div", { className: "text-xs text-muted-foreground" },
            "No worker observation for this card: treated as unknown, not stopped."),
      agg && typeof agg === "object"
        ? h("div", { className: "text-xs text-muted-foreground", style: { marginTop: "4px" } },
            "aggregate: running " + (agg.running != null ? agg.running : "?") +
            " \u00b7 stopped " + (agg.stopped != null ? agg.stopped : "?") +
            " \u00b7 unknown " + (agg.unknown != null ? agg.unknown : "?") +
            " \u00b7 complete " + (agg.complete === true ? "yes" : "no"))
        : null);
  }

  // -------------------------------------------------------------------------
  // Workflow bridge (K2/K3/K4/K5/K6/K7/K8): attention queue, board changes,
  // card timeline, explicit readiness, continuation draft -> continue, and
  // review hold. Gated on /evidence/context alignment. Reads use the evidence
  // envelope; every write is an explicit user click, never automatic, never a
  // dispatch. Tests mock the network layer only.
  // -------------------------------------------------------------------------

  const WORKFLOW_CHANGES_POLL_MS = 30000;

  // K7 notification region: event kinds that surface an actionable in-page
  // notification on the EXISTING /events stream (extended onmessage, never a
  // second WebSocket / poll notifier). review_requested and changes_requested
  // are the review-feedback kinds; the rest are the existing terminal kinds
  // the gateway already notifies on.
  const NOTICE_KINDS = {
    review_requested: "Review requested",
    changes_requested: "Changes requested",
    completed: "Completed",
    blocked: "Blocked",
    gave_up: "Gave up",
    crashed: "Crashed",
    timed_out: "Timed out",
  };
  const NOTICE_MAX = 20;
  // R5: which notice kinds are "interventions" (a human/operator action is
  // needed on the task) versus "recoveries" (the task left its held state).
  // An unchanged repeated intervention is quiet; a recovery clears the
  // stored fingerprint so a later recurrence notifies again.
  const NOTICE_INTERVENTION_KINDS = {
    blocked: true,
    gave_up: true,
    crashed: true,
    timed_out: true,
  };
  const NOTICE_RECOVERY_KINDS = {
    completed: true,
    review_requested: true,
    changes_requested: true,
  };

  // Workflow responses use the standard evidence envelope; the helper's own
  // receipt (readiness / draft / continue / hold) lives under ``evidence``.
  // This normalizer mirrors evidenceEnvelope but never claims the "worker
  // evidence unavailable" remedy (workflow projections are not worker
  // evidence), so it cannot collide with the worker-evidence UI surface.
  function workflowEnvelope(promise) {
    return promise.then(function (res) {
      return (res && typeof res === "object") ? res : { state: "UNKNOWN", reason: "empty workflow response" };
    }).catch(function (err) {
      return {
        state: "UNKNOWN",
        reason: parseApiErrorMessage(err) || "workflow endpoint unreachable",
        remedy: "Workflow is unavailable on this server.",
      };
    });
  }

  function fetchEvidenceAttention(board, limit, cursor) {
    const qs = evQuery({ limit: limit || 50, cursor: cursor || undefined });
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/attention?${qs}`, board)));
  }
  function fetchEvidenceChanges(board, limit, cursor) {
    const qs = evQuery({ limit: limit || 50, cursor: cursor || undefined });
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/changes?${qs}`, board)));
  }
  function fetchEvidenceTimeline(board, card, limit, cursor) {
    const qs = evQuery({ limit: limit || 50, cursor: cursor || undefined });
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/timeline?card=${encodeURIComponent(card)}&${qs}`, board)));
  }
  // K7 notification baseline: one MAX(id) read from the same selected-server
  // DB as the existing /events stream (works local and EVO alike), never a
  // remote worker claim and never a full/board poll. The changes panel keeps
  // its own /evidence/changes cursor; this endpoint feeds ONLY the notice
  // region's historical-flood guard.
  function fetchEventsBaseline(board) {
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}/events/baseline`, board)));
  }
  function postWorkflow(board, path, body) {
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}${path}`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  }

  // -------------------------------------------------------------------------
  // Next-ten fetchers (R1/R2/R4/R6/R8/R9). All read the same /api boundary;
  // every one resolves to the standard envelope shape (never throws).
  // -------------------------------------------------------------------------
  function fetchReleases(board) {
    return evidenceEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/releases`, board)));
  }
  function fetchBrowserReadiness(board) {
    return evidenceEnvelope(SDK.fetchJSON(withBoard(`${API}/evidence/browser-readiness`, board)));
  }
  function fetchAcceptanceCompare(board, card, currentRunId, previousRunId) {
    const qs = evQuery({
      card: card,
      current_run_id: currentRunId,
      previous_run_id: previousRunId,
    });
    return evidenceEnvelope(SDK.fetchJSON(
      withBoard(`${API}/evidence/acceptance-compare?${qs}`, board)));
  }
  function fetchReviewerPacket(board, card) {
    return evidenceEnvelope(SDK.fetchJSON(
      withBoard(`${API}/evidence/reviewer-packet?card=${encodeURIComponent(card)}`, board)));
  }
  function fetchAttachmentProvenance(board, card, attachmentId) {
    const qs = evQuery({ card: card, attachment_id: attachmentId });
    return evidenceEnvelope(SDK.fetchJSON(
      withBoard(`${API}/evidence/attachment-provenance?${qs}`, board)));
  }
  function postReadinessBatch(board, cards, checkModel) {
    return workflowEnvelope(SDK.fetchJSON(withBoard(`${API}/workflow/readiness-batch`, board), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cards: cards, check_model: !!checkModel }),
    }));
  }

  // R6: download the bounded reviewer packet as a real JSON file via a blob
  // URL link click (the plugin has no file channel; the browser download IS
  // the user path).
  function downloadReviewerPacket(board, card) {
    return fetchReviewerPacket(board, card).then(function (env) {
      if (!env || env.state !== "PASS" || !env.evidence) {
        return { ok: false, envelope: env };
      }
      const payload = JSON.stringify(env.evidence, null, 2);
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "reviewer-packet-" + card + ".json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
      return { ok: true, envelope: env };
    });
  }

  // R5: one fingerprint per (task, intervention) so an UNCHANGED repeated
  // intervention stays quiet while a changed reason/remedy or a new run id
  // notifies again. Recovery (status leaving the held/blocked set) clears
  // the stored fingerprint so a recurrence is fresh again.
  function interventionFingerprint(evt) {
    const reason = typeof evt.reason === "string" ? evt.reason : "";
    const remedy = typeof evt.remedy === "string" ? evt.remedy : "";
    const run = evt.run_id != null ? String(evt.run_id) : "";
    return [evt.task_id || "", evt.kind || "", reason, remedy, run].join("|");
  }
  function noticeFingerprint(n) {
    return interventionFingerprint({
      task_id: n.task_id,
      kind: n.kind,
      reason: n.reason,
      remedy: n.remedy,
      run_id: n.run_id,
    });
  }

  function workflowStateTone(state) {
    if (state === "PASS") return EVIDENCE_STATE_TONE.running;
    if (state === "FAIL") return "#ef4444";
    if (state === "UNKNOWN") return EVIDENCE_STATE_TONE.unknown;
    return EVIDENCE_STATE_TONE.unavailable;
  }

  function splitNonEmpty(text) {
    return String(text || "").split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return s + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    return (s / 3600).toFixed(1) + "h";
  }

  function WorkflowStateLine(props) {
    // A single envelope state + reason + remedy line, never presented as green
    // when the envelope is not PASS.
    const env = props.envelope;
    if (!env) return null;
    if (env.state === "PASS") return null;
    const tone = workflowStateTone(env.state || "UNKNOWN");
    return h("div", { className: "text-xs", style: { marginTop: "4px" } },
      h("span", { style: { color: tone, fontWeight: "600" } }, env.state || "UNKNOWN"),
      h("span", { style: { marginLeft: "6px", color: "var(--muted-foreground, #6b7280)" } },
        env.reason || "workflow response unavailable"),
      env.remedy
        ? h("span", { style: { marginLeft: "6px", color: "var(--muted-foreground, #6b7280)" } }, env.remedy)
        : null);
  }

  // K7 — actionable, deduplicated change notifications. Driven exclusively by
  // the EXISTING /events WebSocket stream (the onmessage is extended, never a
  // second notifier). Each row opens the exact card; the baseline (bounded
  // baseline-now /evidence/changes read) gates what may notify, so historical
  // events never flood. Unknown baseline pauses notifications and offers a
  // retry rather than replaying history.
  function WorkflowNoticeRegion(props) {
    const baselineState = props.baselineState; // null | "unknown" | number
    const notices = props.notices || [];
    const stateLabel = baselineState === "unknown" ? "unknown"
      : (typeof baselineState === "number" ? "ready" : "pending");
    const rows = notices.map(function (n) {
      const label = NOTICE_KINDS[n.kind] || n.kind;
      return h("div", {
        key: n.id,
        className: "hermes-kanban-workflow-notice",
        "data-workflow-notice-item": n.task_id || "",
        "data-workflow-notice-kind": n.kind || "",
      },
        h("button", {
          type: "button",
          className: "hermes-kanban-workflow-notice-open",
          "data-workflow-notice-open-item": n.task_id || "",
          onClick: function () { if (props.onOpen && n.task_id) props.onOpen(n.task_id); },
        },
          h("span", { className: "hermes-kanban-workflow-notice-kind" }, label),
          h("span", { className: "hermes-kanban-workflow-notice-task" }, n.task_id || ""),
          n.created_at != null
            ? h("span", { className: "hermes-kanban-comment-ago" }, timeAgo ? timeAgo(n.created_at) : "")
            : null),
        h("button", {
          type: "button",
          className: "hermes-kanban-workflow-notice-dismiss",
          "aria-label": "dismiss notification",
          onClick: function () { if (props.onDismiss) props.onDismiss(n.id); },
        }, "×"));
    });
    return h("div", {
      className: "hermes-kanban-section",
      "data-workflow-notice": "true",
      "data-workflow-notice-baseline": stateLabel,
      "data-workflow-notice-baseline-id": typeof baselineState === "number" ? String(baselineState) : "",
    },
      h("div", { className: "hermes-kanban-section-head" }, "Notifications"),
      baselineState === "unknown"
        ? h("div", { className: "hermes-kanban-workflow-notice-paused" },
            h("span", { className: "text-xs text-muted-foreground" },
              "Change notifications are paused: the change baseline could not be established."),
            h("button", {
              type: "button",
              className: "hermes-kanban-workflow-btn",
              "data-workflow-notice-retry": "true",
              onClick: props.onRetry,
            }, "Retry baseline"))
        : null,
      baselineState === null
        ? h("div", { className: "text-xs text-muted-foreground" }, "Establishing change baseline…")
        : null,
      rows.length > 0 ? h("div", { className: "hermes-kanban-workflow-list" }, rows) : null,
      rows.length > 0
        ? h("button", {
            type: "button",
            className: "hermes-kanban-workflow-btn",
            "data-workflow-notice-clear": "true",
            onClick: props.onClear,
          }, "Clear")
        : null);
  }

  // K2 — board attention queue: bounded, pageable, exact card opens.
  function WorkflowAttentionSection(props) {
    const [envelope, setEnvelope] = useState(null);
    const [items, setItems] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [hasMore, setHasMore] = useState(false);
    const [cursor, setCursor] = useState(null);
    const [omitted, setOmitted] = useState(null);
    const genRef = useRef(0);

    const loadFirst = useCallback(function () {
      genRef.current += 1;
      const gen = genRef.current;
      setLoading(true);
      return fetchEvidenceAttention(props.boardSlug, 50, null).then(function (env) {
        if (gen !== genRef.current) return;
        setLoading(false);
        setEnvelope(env);
        if (!env || env.state !== "PASS") { setItems([]); return; }
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const cards = Array.isArray(data.cards) ? data.cards : [];
        setItems(cards);
        setHasMore(data.has_more === true);
        setCursor(data.next_cursor != null ? data.next_cursor : null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
      });
    }, [props.boardSlug]);

    useEffect(function () { loadFirst(); }, [loadFirst]);

    const loadMore = useCallback(function () {
      if (loadingMore || !hasMore || !cursor) return Promise.resolve();
      setLoadingMore(true);
      const gen = genRef.current;
      return fetchEvidenceAttention(props.boardSlug, 50, cursor).then(function (env) {
        if (gen !== genRef.current) return;
        setLoadingMore(false);
        setEnvelope(env);
        if (!env || env.state !== "PASS") return;
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const cards = Array.isArray(data.cards) ? data.cards : [];
        const seen = {};
        const merged = items.concat(cards).filter(function (c) {
          if (!c || seen[c.id]) return false;
          seen[c.id] = true;
          return true;
        });
        setItems(merged);
        setHasMore(data.has_more === true);
        setCursor(data.next_cursor != null ? data.next_cursor : null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
      });
    }, [loadingMore, hasMore, cursor, items, props.boardSlug]);

    const rows = items.map(function (card) {
      const reasons = Array.isArray(card.reasons) ? card.reasons.join("; ") : "";
      const authority = card.operator_authority_needed != null
        ? (" · operator authority: " + card.operator_authority_needed)
        : "";
      return h("button", {
        key: card.id,
        type: "button",
        className: "hermes-kanban-workflow-attention-card",
        "data-workflow-attention-card": card.id,
        onClick: function () { if (props.onOpen) props.onOpen(card.id); },
      },
        h("span", { className: "hermes-kanban-workflow-attention-id" }, card.id),
        h("span", { className: "hermes-kanban-workflow-attention-title" }, card.title || "(untitled)"),
        h("span", { className: "hermes-kanban-workflow-attention-status" }, card.status || ""),
        reasons ? h("span", { className: "hermes-kanban-workflow-attention-reasons" }, reasons) : null,
        card.next_action ? h("span", { className: "hermes-kanban-workflow-attention-action" }, card.next_action) : null,
        authority ? h("span", { className: "hermes-kanban-workflow-attention-action" }, authority) : null);
    });

    return h("div", { className: "hermes-kanban-section", "data-workflow-attention": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Attention"),
      loading ? h("div", { className: "text-xs text-muted-foreground" }, "Loading attention\u2026") : null,
      rows.length > 0 ? h("div", { className: "hermes-kanban-workflow-list" }, rows) :
        (!loading && (!envelope || envelope.state === "PASS")
          ? h("div", { className: "text-xs text-muted-foreground" }, "No cards need attention.")
          : null),
      hasMore
        ? h("button", {
            type: "button",
            className: "hermes-kanban-edit-link",
            "data-workflow-attention-more": "true",
            disabled: loadingMore,
            onClick: loadMore,
          }, loadingMore ? "Loading\u2026" : "More attention" + (omitted != null ? " (" + omitted + " omitted)" : ""))
        : null,
      h(WorkflowStateLine, { envelope: envelope }));
  }

  // K3 — board change tail: baseline-now first read, bounded paging, 30s poll.
  function WorkflowChangesSection(props) {
    const [envelope, setEnvelope] = useState(null);
    const [events, setEvents] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [hasMore, setHasMore] = useState(false);
    const [cursor, setCursor] = useState(null);
    const [omitted, setOmitted] = useState(null);
    const [anchor, setAnchor] = useState(null);
    const genRef = useRef(0);
    const cursorRef = useRef(null);

    const apply = useCallback(function (env, append) {
      setEnvelope(env);
      if (!env || env.state !== "PASS") { setEvents([]); return; }
      const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
      const page = Array.isArray(data.events) ? data.events : [];
      if (append) {
        const seen = {};
        setEvents(function (prev) {
          return prev.concat(page).filter(function (e) {
            if (!e || e.id == null || seen[e.id]) return false;
            seen[e.id] = true;
            return true;
          });
        });
      } else {
        setEvents(page);
      }
      setHasMore(data.has_more === true);
      const next = data.next_cursor != null ? data.next_cursor : null;
      setCursor(next);
      cursorRef.current = next;
      setOmitted(typeof data.omitted === "number" ? data.omitted : null);
      if (data.anchor_state) setAnchor(data.anchor_state);
    }, []);

    const loadFirst = useCallback(function () {
      genRef.current += 1;
      const gen = genRef.current;
      setLoading(true);
      return fetchEvidenceChanges(props.boardSlug, 50, null).then(function (env) {
        if (gen !== genRef.current) return;
        setLoading(false);
        apply(env, false);
      });
    }, [props.boardSlug, apply]);

    const loadMore = useCallback(function () {
      if (loadingMore || !hasMore || !cursorRef.current) return Promise.resolve();
      setLoadingMore(true);
      const gen = genRef.current;
      return fetchEvidenceChanges(props.boardSlug, 50, cursorRef.current).then(function (env) {
        if (gen !== genRef.current) return;
        setLoadingMore(false);
        apply(env, true);
      });
    }, [loadingMore, hasMore, props.boardSlug, apply]);

    useEffect(function () { loadFirst(); }, [loadFirst]);

    // Bounded 30s poll while visible; uses the latest consumed cursor so it is
    // an incremental tail, never a historical flood.
    useEffect(function () {
      const id = setInterval(function () { loadMore(); }, WORKFLOW_CHANGES_POLL_MS);
      return function () { clearInterval(id); };
    }, [loadMore]);

    const data = (envelope && envelope.evidence && typeof envelope.evidence === "object") ? envelope.evidence : {};
    const eventRows = events.map(function (e) {
      return h("div", { key: e.id, className: "hermes-kanban-workflow-change-event", "data-workflow-changes-event": e.id },
        h("span", { className: "hermes-kanban-event-kind" }, e.kind || "event"),
        e.task_id ? h("span", { className: "hermes-kanban-workflow-change-task" }, e.task_id) : null,
        h("span", { className: "hermes-kanban-comment-ago" }, timeAgo ? timeAgo(e.created_at) : ""));
    });

    return h("div", { className: "hermes-kanban-section", "data-workflow-changes": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Changes"),
      loading ? h("div", { className: "text-xs text-muted-foreground" }, "Loading changes\u2026") : null,
      !loading && envelope && envelope.state === "PASS" && events.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" },
            anchor === "baseline" ? "No observed changes since the baseline." : "No observed changes.")
        : null,
      eventRows.length > 0 ? h("div", { className: "hermes-kanban-workflow-list" }, eventRows) : null,
      hasMore
        ? h("button", {
            type: "button",
            className: "hermes-kanban-edit-link",
            "data-workflow-changes-more": "true",
            disabled: loadingMore,
            onClick: loadMore,
          }, loadingMore ? "Loading\u2026" : "More changes" + (omitted != null ? " (" + omitted + " omitted)" : ""))
        : null,
      envelope && envelope.state === "PASS"
        ? h("div", { className: "hermes-kanban-workflow-changes-fresh text-xs text-muted-foreground" },
            data.first_read_policy ? data.first_read_policy : "",
            evidenceObservedAt(envelope) != null
              ? " · observed " + (timeAgo ? timeAgo(evidenceObservedAt(envelope)) : "")
              : "")
        : null,
      h(WorkflowStateLine, { envelope: envelope }));
  }

  // K4 — card timeline: disjoint execution/blocked/review/unknown intervals,
  // covered-window totals (never productivity), visible gaps, bounded paging.
  function WorkflowTimelineSection(props) {
    const [envelope, setEnvelope] = useState(null);
    const [intervals, setIntervals] = useState([]);
    const [totals, setTotals] = useState(null);
    const [gaps, setGaps] = useState([]);
    const [loading, setLoading] = useState(true);
    const [loadingMore, setLoadingMore] = useState(false);
    const [hasMore, setHasMore] = useState(false);
    const [cursor, setCursor] = useState(null);
    const [omitted, setOmitted] = useState(null);
    const genRef = useRef(0);

    const loadFirst = useCallback(function () {
      genRef.current += 1;
      const gen = genRef.current;
      setLoading(true);
      return fetchEvidenceTimeline(props.boardSlug, props.cardId, 50, null).then(function (env) {
        if (gen !== genRef.current) return;
        setLoading(false);
        setEnvelope(env);
        if (!env || env.state !== "PASS") { setIntervals([]); return; }
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        setIntervals(Array.isArray(data.intervals) ? data.intervals : []);
        setTotals((data.totals && typeof data.totals === "object") ? data.totals : null);
        setGaps((data.coverage && Array.isArray(data.coverage.gaps)) ? data.coverage.gaps : []);
        setHasMore(data.has_more === true);
        setCursor(data.next_cursor != null ? data.next_cursor : null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
      });
    }, [props.boardSlug, props.cardId]);

    useEffect(function () { loadFirst(); }, [loadFirst]);

    const loadMore = useCallback(function () {
      if (loadingMore || !hasMore || !cursor) return Promise.resolve();
      setLoadingMore(true);
      const gen = genRef.current;
      return fetchEvidenceTimeline(props.boardSlug, props.cardId, 50, cursor).then(function (env) {
        if (gen !== genRef.current) return;
        setLoadingMore(false);
        setEnvelope(env);
        if (!env || env.state !== "PASS") return;
        const data = (env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const page = Array.isArray(data.intervals) ? data.intervals : [];
        const seen = {};
        setIntervals(function (prev) {
          return prev.concat(page).filter(function (it) {
            const key = it.kind + ":" + it.start + ":" + it.end;
            if (seen[key]) return false;
            seen[key] = true;
            return true;
          });
        });
        setHasMore(data.has_more === true);
        setCursor(data.next_cursor != null ? data.next_cursor : null);
        setOmitted(typeof data.omitted === "number" ? data.omitted : null);
      });
    }, [loadingMore, hasMore, cursor, props.boardSlug, props.cardId]);

    const covered = totals && totals.covered_window ? totals.covered_window : null;
    const intervalRows = intervals.map(function (it, i) {
      return h("div", {
        key: it.start + ":" + it.end + ":" + i,
        className: "hermes-kanban-workflow-interval",
        "data-workflow-timeline-interval": it.kind,
      },
        h("span", { className: "hermes-kanban-workflow-interval-kind" }, it.kind || "unknown"),
        h("span", { className: "hermes-kanban-workflow-interval-dur" }, fmtDuration(it.duration_seconds)),
        h("span", { className: "hermes-kanban-comment-ago" }, timeAgo ? timeAgo(it.start) : ""));
    });

    return h("div", { className: "hermes-kanban-section", "data-workflow-timeline": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Timeline"),
      loading ? h("div", { className: "text-xs text-muted-foreground" }, "Loading timeline\u2026") : null,
      covered
        ? h("div", { className: "hermes-kanban-workflow-totals text-xs text-muted-foreground" },
            "covered window · execution " + fmtDuration(covered.execution_seconds) +
            " · blocked " + fmtDuration(covered.blocked_seconds) +
            " · review " + fmtDuration(covered.review_wait_seconds) +
            " · unknown " + fmtDuration(covered.unknown_seconds) +
            " (wall-clock observation, not productivity)")
        : null,
      gaps.length > 0
        ? h("div", { className: "hermes-kanban-workflow-gaps text-xs" },
            h("span", { style: { color: workflowStateTone("UNKNOWN"), fontWeight: "600" } }, gaps.length + " gap(s):"),
            gaps.map(function (g, i) {
              return h("span", {
                key: i,
                className: "hermes-kanban-workflow-gap",
                "data-workflow-timeline-gap": "true",
              }, " " + fmtDuration(g.duration_seconds || (g.end - g.start)));
            }))
        : null,
      intervalRows.length > 0 ? h("div", { className: "hermes-kanban-workflow-list" }, intervalRows) :
        (!loading && (!envelope || envelope.state === "PASS")
          ? h("div", { className: "text-xs text-muted-foreground" }, "No timeline intervals.")
          : null),
      hasMore
        ? h("button", {
            type: "button",
            className: "hermes-kanban-edit-link",
            "data-workflow-timeline-more": "true",
            disabled: loadingMore,
            onClick: loadMore,
          }, loadingMore ? "Loading\u2026" : "More timeline" + (omitted != null ? " (" + omitted + " omitted)" : ""))
        : null,
      h(WorkflowStateLine, { envelope: envelope }));
  }

  // R9 — selected held-card batch readiness PREVIEW. Selecting held cards
  // enables a preview-only readiness check through POST /workflow/
  // readiness-batch; per-card failures, omissions and repair previews are
  // each visible, and there is NO bulk release affordance anywhere here.
  function ReadinessBatchSection(props) {
    const board = props.boardSlug;
    const pool = props.heldCards || [];
    const [selected, setSelected] = useState(() => ({}));
    const [envelope, setEnvelope] = useState(null);
    const [running, setRunning] = useState(false);

    const selectedIds = Object.keys(selected).filter(function (id) { return selected[id]; });
    const canRun = selectedIds.length >= 1 && selectedIds.length <= 10 && !running;

    function toggle(id) {
      setSelected(function (prev) {
        const next = Object.assign({}, prev);
        next[id] = !prev[id];
        return next;
      });
    }

    function runPreview() {
      if (!canRun) return;
      setRunning(true);
      setEnvelope(null);
      postReadinessBatch(board, selectedIds, false).then(function (env) {
        setRunning(false);
        setEnvelope(env);
      });
    }

    const ev = (envelope && envelope.state === "PASS" && envelope.evidence
      && typeof envelope.evidence === "object") ? envelope.evidence : null;
    const items = ev && Array.isArray(ev.items) ? ev.items : [];

    return h("div", { className: "hermes-kanban-section", "data-readiness-batch": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Batch readiness (preview only)"),
      h("div", { className: "text-xs text-muted-foreground" },
        "Select held cards for a readiness preview. This never releases, dispatches or mutates anything."),
      pool.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" }, "No held (blocked/triage) cards on the current page.")
        : h("div", { className: "hermes-kanban-workflow-list", "data-readiness-batch-pool": "true" },
            pool.map(function (c) {
              return h("label", {
                key: c.id,
                className: "hermes-kanban-workflow-check",
                "data-readiness-batch-card": c.id,
              },
                h(Checkbox, {
                  checked: !!selected[c.id],
                  onCheckedChange: function (v) { toggle(c.id); },
                }),
                h("span", { className: "hermes-kanban-workflow-check-name" }, c.title),
                h("span", { className: "hermes-kanban-workflow-check-reason" }, c.status));
            })),
      h("button", {
        type: "button",
        className: "hermes-kanban-workflow-btn",
        "data-readiness-batch-run": "true",
        disabled: !canRun,
        onClick: runPreview,
      }, running ? "Previewing\u2026" : "Preview readiness (" + selectedIds.length + " selected)"),
      envelope && envelope.state !== "PASS"
        ? h("div", { className: "text-xs", style: { color: workflowStateTone(envelope.state) } },
            envelope.reason || ("batch " + envelope.state))
        : null,
      items.length > 0
        ? h("div", { className: "hermes-kanban-workflow-list", "data-readiness-batch-result": "true" },
            items.map(function (item) {
              const previews = Array.isArray(item.repair_preview) ? item.repair_preview : [];
              return h("div", {
                key: item.card,
                className: "hermes-kanban-workflow-check",
                "data-readiness-batch-item": item.card,
              },
                h("span", { className: "hermes-kanban-workflow-check-name" }, item.card),
                h("span", {
                  className: "hermes-kanban-workflow-check-state",
                  style: { color: workflowStateTone(item.state) },
                }, item.state || "UNKNOWN"),
                previews.slice(0, 1).map(function (p, i) {
                  return h("span", {
                    key: i,
                    className: "hermes-kanban-workflow-check-reason",
                    "data-readiness-batch-repair": "true",
                  }, p.action);
                }));
            }),
            ev && typeof ev.omitted === "number" && ev.omitted > 0
              ? h("div", { className: "text-xs text-muted-foreground" },
                  ev.omitted + " requested card(s) omitted — check each card id.")
              : null)
        : null);
  }

  // R1/R2/R7 — concise expandable support panel: release identities (the
  // LOADED frontend asset captured at execution, the adapter's served
  // identity and this Hermes backend's release metadata), browser readiness
  // shown VISIBLY SEPARATE from reachability, and refresh timing with the
  // snapshot's actual worker-observation coverage. Collapsed by default so
  // it never competes with the board; every fetch happens only on expand.
  function SupportPanel(props) {
    const [open, setOpen] = useState(false);
    const [releases, setReleases] = useState(null);
    const [readiness, setReadiness] = useState(null);
    // Board+request generation: a fetch that starts for one board can never
    // publish another board's identity, and a FAILED/absent fetch never
    // marks the board loaded — reopening retries instead of stranding.
    const loadedGenRef = useRef(0);
    const [reloadTick, setReloadTick] = useState(0);

    const board = props.boardSlug;
    useEffect(function () {
      if (!open) return undefined;
      loadedGenRef.current += 1;
      const gen = loadedGenRef.current;
      let alive = true;
      setReleases(null);
      setReadiness(null);
      fetchReleases(board).then(function (env) { if (alive && gen === loadedGenRef.current) setReleases(env); });
      fetchBrowserReadiness(board).then(function (env) { if (alive && gen === loadedGenRef.current) setReadiness(env); });
      return function () { alive = false; };
    }, [open, board, reloadTick]);

    // The identity lines read whatever evidence the envelope carried, even
    // when the overall state is FAIL/UNKNOWN (the backend identity is still
    // real); only a missing evidence object falls back to honest UNKNOWN.
    const relData = (releases && releases.evidence && typeof releases.evidence === "object")
      ? releases.evidence : null;
    const rel = (releases && releases.state === "PASS" && relData) ? relData : null;
    const adapter = (relData && relData.adapter) ? relData.adapter : null;
    const backend = (relData && relData.backend) ? relData.backend : null;
    const ready = (readiness && readiness.evidence) ? readiness.evidence : null;
    // R7: the snapshot's OWN measured timing, not the bridge subprocess
    // round-trip alone.
    const snapEv = (props.snapshot && props.snapshot.evidence && typeof props.snapshot.evidence === "object")
      ? props.snapshot.evidence : {};
    const snapTiming = (snapEv.timing && typeof snapEv.timing === "object") ? snapEv.timing : {};
    const timing = (props.snapshot && props.snapshot.timing) ? props.snapshot.timing : {};
    const roundtrip = typeof timing.helper_roundtrip_ms === "number"
      ? Math.round(timing.helper_roundtrip_ms) + "ms" : "unknown";
    const querySeconds = typeof snapTiming.query_seconds === "number" ? snapTiming.query_seconds : null;
    const collectionSeconds = typeof snapTiming.collection_seconds === "number" ? snapTiming.collection_seconds : null;
    // R7: actual snapshot facts. The adapter emits worker_observations (a
    // bounded list), worker_observation_cap and worker_observations_capped —
    // there is no process_checks field, so counts derive only from what was
    // actually examined; capped-away observations are shown as the omission
    // they are, never as healthy.
    const obsList = Array.isArray(snapEv.worker_observations) ? snapEv.worker_observations : null;
    const obsCap = typeof snapEv.worker_observation_cap === "number" ? snapEv.worker_observation_cap : null;
    const obsCapped = typeof snapEv.worker_observations_capped === "number" ? snapEv.worker_observations_capped : null;
    const pending = releases == null || readiness == null;

    return h("div", { className: "hermes-kanban-section", "data-support-panel": "true" },
      h("button", {
        type: "button",
        className: "hermes-kanban-section-head hermes-kanban-support-toggle",
        "data-support-panel-toggle": "true",
        onClick: function () { setOpen(!open); },
      }, (open ? "▾ " : "▸ ") + "Support info"),
      open ? h("div", { className: "hermes-kanban-support-body", "data-support-panel-body": "true" },
        // R1: all three identities — loaded asset, adapter, backend.
        h("div", { className: "text-xs", "data-support-releases": "true" },
          h("div", { style: { fontWeight: "600" } }, "Releases"),
          h("div", { "data-support-frontend": "true" },
            "frontend (this asset): " + (LOADED_ASSET_IDENTITY.state === "PASS"
              ? LOADED_ASSET_IDENTITY.revision +
                (LOADED_ASSET_IDENTITY.bytes != null ? " (" + LOADED_ASSET_IDENTITY.bytes + " bytes)" : "") +
                " — " + LOADED_ASSET_IDENTITY.source
              : "UNKNOWN — " + LOADED_ASSET_IDENTITY.source)),
          adapter
            ? h("div", { "data-support-adapter": "true" },
                "adapter: " + (adapter.state || "UNKNOWN") +
                (adapter.revision ? " @ " + String(adapter.revision).slice(0, 12) : "") +
                (adapter.version ? " (" + adapter.version + ")" : "") +
                (adapter.source ? " — " + adapter.source : ""))
            : h("div", { style: { color: EVIDENCE_STATE_TONE.unknown }, "data-support-adapter": "true" },
                "adapter: " + ((releases && releases.reason) || "identity unavailable")),
          backend
            ? h("div", { "data-support-backend": "true", style: backend.state !== "PASS" ? { color: EVIDENCE_STATE_TONE.unknown } : undefined },
                "backend: " + (backend.state || "UNKNOWN") +
                (backend.revision ? " @ " + backend.revision : "") +
                (backend.source ? " — " + backend.source : ""))
            : h("div", { style: { color: EVIDENCE_STATE_TONE.unknown }, "data-support-backend": "true" },
                "backend: UNKNOWN — this server does not report its release identity"),
          rel && rel.adapter && rel.adapter.state !== "PASS"
            ? h("div", { style: { color: EVIDENCE_STATE_TONE.unknown } },
                "adapter identity " + rel.adapter.state + (rel.adapter.state === "UNKNOWN" ? " — absent or invalid identity is UNKNOWN, never assumed" : ""))
            : null,
          pending
            ? h("div", { className: "text-xs text-muted-foreground", "data-support-pending": "true" }, "loading identities…")
            : null,
          releases && releases.state !== "PASS"
            ? h("button", {
                type: "button",
                className: "hermes-kanban-edit-link",
                "data-support-retry": "true",
                onClick: function () { setReloadTick(function (n) { return n + 1; }); },
              }, "retry identity read")
            : null),
        // R2: browser readiness separate from reachability.
        h("div", { className: "text-xs", "data-support-readiness": "true", style: { marginTop: "6px" } },
          h("div", { style: { fontWeight: "600" } }, "Browser readiness"),
          ready
            ? h("div", { "data-support-readiness-line": "true" },
                "reachable: " + (ready.reachable ? "yes" : "no") +
                " · authenticated: " + (ready.authenticated ? "yes" : "no") +
                " · board readable: " + (ready.board_readable === true ? "yes"
                  : ready.board_readable === false ? "no"
                  : "unknown"))
            : h("div", { style: { color: EVIDENCE_STATE_TONE.unknown } },
                "readiness: " + ((readiness && readiness.reason) || "unknown")),
          readiness && readiness.state !== "PASS" && readiness.remedy
            ? h("div", { style: { color: "var(--muted-foreground, #6b7280)" } }, readiness.remedy)
            : null,
          h("div", { style: { color: "var(--muted-foreground, #6b7280)" } },
            "A reachable login page never implies board access; these are separate facts.")),
        // R7: refresh timing + actual worker-observation coverage.
        h("div", { className: "text-xs", "data-support-refresh": "true", style: { marginTop: "6px" } },
          h("div", { style: { fontWeight: "600" } }, "Refresh"),
          h("div", null,
            "snapshot query: " + (querySeconds != null ? (Math.round(querySeconds * 1000) + "ms") : "unknown") +
            " · collection: " + (collectionSeconds != null ? (Math.round(collectionSeconds * 1000) + "ms") : "unknown") +
            " · bridge round-trip: " + roundtrip +
            " · refresh interval: " + Math.round(EVIDENCE_POLL_MS / 1000) + "s"),
          h("div", { "data-support-process-checks": "true" },
            "worker observations: " +
            (obsList != null
              ? obsList.length + " examined" +
                (obsCap != null ? " (cap " + obsCap + ")" : "")
              : "count unknown — snapshot carried no observation list") +
            (obsCapped != null && obsCapped > 0
              ? " · " + obsCapped + " capped away (not examined, never assumed healthy)"
              : "")),
          h("div", { style: { color: "var(--muted-foreground, #6b7280)" } },
            "A slow refresh is transport delay or intentionally limited coverage, never hidden work.")))
      : null);
  }

  // K5 — explicit readiness: POST only on click; separate permission check.
  function WorkflowReadinessSection(props) {
    const [receipt, setReceipt] = useState(null);
    const [loading, setLoading] = useState(false);

    function doCheck() {
      setLoading(true);
      setReceipt(null);
      return postWorkflow(props.boardSlug, "/workflow/readiness", {
        card: props.cardId,
        check_model: true,
      }).then(function (env) {
        setLoading(false);
        setReceipt(env);
      });
    }

    const ev = (receipt && receipt.evidence && typeof receipt.evidence === "object") ? receipt.evidence : null;
    const checks = (ev && Array.isArray(ev.checks)) ? ev.checks : [];
    // R3: the NEXT repair preview, derived only from failed/unknown checks.
    // Actions are bounded text previews; this panel never triggers repairs.
    const repairPreviews = [];
    checks.forEach(function (c) {
      if (c.state !== "FAIL" && c.state !== "UNKNOWN") return;
      const action = REPAIR_PREVIEW_ACTIONS[c.name] || REPAIR_PREVIEW_DEFAULT;
      repairPreviews.push({ check: c.name, state: c.state, action: action, reason: c.reason || "" });
    });
    const nextRepair = repairPreviews.length > 0 ? repairPreviews[0] : null;
    const checkRows = checks.map(function (c) {
      const isPermission = c.name === "board_permission";
      return h("div", {
        key: c.name,
        className: "hermes-kanban-workflow-check",
        "data-workflow-readiness-item": c.name,
      },
        h("span", { className: "hermes-kanban-workflow-check-name" },
          isPermission ? "Permission" : c.name),
        h("span", {
          className: "hermes-kanban-workflow-check-state",
          style: { color: workflowStateTone(c.state) },
        }, c.state || "UNKNOWN"),
        c.reason
          ? h("span", { className: "hermes-kanban-workflow-check-reason" }, c.reason)
          : null,
        isPermission && typeof c.mutation_authorized === "boolean"
          ? h("span", { className: "hermes-kanban-workflow-check-reason" },
              "mutation " + (c.mutation_authorized ? "authorized" : "not authorized"))
          : null);
    });

    return h("div", { className: "hermes-kanban-section", "data-workflow-readiness": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Readiness"),
      h("div", { className: "text-xs text-muted-foreground" },
        "Readiness is never automatic. A board permission check alone is never readiness."),
      h("button", {
        type: "button",
        className: "hermes-kanban-workflow-btn",
        "data-workflow-readiness-check": "true",
        disabled: loading,
        onClick: doCheck,
      }, loading ? "Checking\u2026" : "Check readiness"),
      checkRows.length > 0 ? h("div", { className: "hermes-kanban-workflow-list" }, checkRows) : null,
      nextRepair
        ? h("div", {
            className: "hermes-kanban-workflow-repair-preview",
            "data-workflow-repair-preview": "true",
            "data-workflow-repair-check": nextRepair.check,
          },
          h("div", { className: "text-xs", style: { fontWeight: "600" } }, "Next repair (preview only)"),
          h("div", { className: "text-xs" },
            h("span", { style: { color: workflowStateTone(nextRepair.state) } },
              nextRepair.check + ": " + nextRepair.state),
            nextRepair.reason
              ? h("span", { style: { color: "var(--muted-foreground, #6b7280)", marginLeft: "6px" } }, nextRepair.reason)
              : null),
          h("div", { className: "text-xs", "data-workflow-repair-action": "true" }, nextRepair.action),
          h("div", { className: "text-xs", style: { color: "var(--muted-foreground, #6b7280)" } },
            "Previews suggest the next manual step; they never trigger repairs."))
        : null,
      ev && typeof ev.ready_to_release === "boolean"
        ? h("div", {
            className: "hermes-kanban-workflow-verdict text-xs",
            style: { color: workflowStateTone(ev.ready_to_release ? "PASS" : "FAIL") },
          }, ev.ready_to_release
            ? "ready to release: yes"
            : "ready to release: no")
        : null,
      receipt && receipt.state && receipt.state !== "PASS"
        ? h("div", { className: "text-xs", style: { color: workflowStateTone(receipt.state) } },
            receipt.reason || ("readiness " + receipt.state))
        : null);
  }

  // K6 + K7 — continuation draft (read-only) then a separate create click.
  function WorkflowContinuationSection(props) {
    const task = props.task || {};
    const [note, setNote] = useState("");
    const [passedText, setPassedText] = useState("");
    const [remainingText, setRemainingText] = useState("");
    const [evidenceText, setEvidenceText] = useState("");
    const [acceptanceText, setAcceptanceText] = useState("");
    const [title, setTitle] = useState("");
    const [workspace, setWorkspace] = useState(task.workspace_path ? String(task.workspace_path) : "");
    const [profile, setProfile] = useState(task.assignee ? String(task.assignee) : "");
    const [model, setModel] = useState(task.model_override ? String(task.model_override) : "");
    const [provider, setProvider] = useState(task.provider_override ? String(task.provider_override) : "");
    const [draft, setDraft] = useState(null);
    const [draftErr, setDraftErr] = useState(null);
    const [drafting, setDrafting] = useState(false);
    const [result, setResult] = useState(null);
    const [continuing, setContinuing] = useState(false);
    const genRef = useRef(0);

    // Any change to a draft input invalidates the current draft and any prior
    // mutation result, so a stale draft is never continued and a late mutation
    // reply is discarded (the generation bump is checked in doDraft/doContinue).
    useEffect(function () {
      genRef.current += 1;
      setDraft(null);
      setDraftErr(null);
      setResult(null);
    }, [note, passedText, remainingText, evidenceText, acceptanceText, title, workspace, profile, model, provider]);

    function buildBody() {
      const passed = splitNonEmpty(passedText);
      const names = splitNonEmpty(remainingText);
      const evs = splitNonEmpty(evidenceText);
      const accs = splitNonEmpty(acceptanceText);
      const remaining = names.map(function (name, i) {
        return { check: name, evidence: evs[i] || "", acceptance: accs[i] || "" };
      });
      const body = {
        card: props.cardId,
        passed_checks: passed,
        remaining_checks: remaining,
        verification_note: note.trim(),
      };
      if (workspace.trim()) body.workspace = workspace.trim();
      if (profile.trim()) body.profile = profile.trim();
      if (provider.trim()) body.provider = provider.trim();
      if (model.trim()) body.model = model.trim();
      if (title.trim()) body.title = title.trim();
      return body;
    }

    function doDraft() {
      setDrafting(true);
      setDraft(null);
      setDraftErr(null);
      setResult(null);
      const gen = genRef.current;
      return postWorkflow(props.boardSlug, "/workflow/continuation-draft", buildBody()).then(function (env) {
        if (gen !== genRef.current) return; // late draft reply: inputs changed
        setDrafting(false);
        const ev = (env && env.evidence && typeof env.evidence === "object") ? env.evidence : null;
        if (!env || env.state !== "PASS" || !ev || !ev.fingerprint) {
          setDraftErr((env && env.reason) || "continuation draft unavailable");
          return;
        }
        setDraft(ev);
      });
    }

    function doContinue() {
      if (!draft || !draft.fingerprint) return Promise.resolve();
      setContinuing(true);
      setResult(null);
      const gen = genRef.current;
      // Continue sends the EXACT reviewed draft: the draft's own commission,
      // passed/remaining checks and note, never a rebuild from live inputs
      // (which the invalidation effect has already discarded as stale).
      const comm = (draft.commission && typeof draft.commission === "object") ? draft.commission : {};
      const body = {
        card: props.cardId,
        passed_checks: draft.passed_checks || [],
        remaining_checks: draft.remaining_checks || [],
        verification_note: draft.verification_note || "",
        fingerprint: draft.fingerprint,
      };
      if (comm.workspace) body.workspace = comm.workspace;
      if (comm.profile) body.profile = comm.profile;
      if (comm.provider) body.provider = comm.provider;
      if (comm.model) body.model = comm.model;
      if (comm.title) body.title = comm.title;
      return postWorkflow(props.boardSlug, "/workflow/continue", body).then(function (env) {
        if (gen !== genRef.current) return; // late mutation reply: discarded
        setContinuing(false);
        setResult(env);
      });
    }

    function draftOriginal(d) {
      const orig = d.original || {};
      return h("div", { className: "hermes-kanban-workflow-draft-original", "data-workflow-draft-original": "true" },
        h("div", { className: "hermes-kanban-section-head" }, "Original card"),
        h("div", { className: "text-xs" }, "status: " + (orig.status || "?") + " · assignee: " + (orig.assignee || "?")),
        h("div", { className: "text-xs", style: { color: workflowStateTone("UNKNOWN") } },
          (orig.source || "unverified source excerpt")),
        orig.result_excerpt
          ? h("div", { className: "hermes-kanban-workflow-excerpt" }, "result (unverified): " + orig.result_excerpt)
          : null,
        orig.latest_run_summary_excerpt
          ? h("div", { className: "hermes-kanban-workflow-excerpt" }, "latest run (unverified): " + orig.latest_run_summary_excerpt)
          : null);
    }

    function draftChecks(d) {
      const remaining = Array.isArray(d.remaining_checks) ? d.remaining_checks : [];
      return h("div", { className: "hermes-kanban-workflow-draft-checks" },
        h("div", { className: "text-xs", style: { fontWeight: "600" } },
          "passed: " + (Array.isArray(d.passed_checks) ? d.passed_checks.join("; ") : "")),
        remaining.map(function (rc, i) {
          return h("div", { key: i, className: "hermes-kanban-workflow-remaining" },
            h("span", { className: "hermes-kanban-workflow-remaining-check" }, rc.check || "(check)"),
            rc.acceptance
              ? h("span", { className: "hermes-kanban-workflow-remaining-acceptance" }, rc.acceptance)
              : null);
        }));
    }

    let resultBlock = null;
    if (result) {
      const rev = (result.evidence && typeof result.evidence === "object") ? result.evidence : null;
      if (result.state === "PASS" && rev) {
        resultBlock = h("div", { className: "hermes-kanban-workflow-result", "data-workflow-continue-result": "true" },
          h("div", { className: "text-xs", style: { color: workflowStateTone("PASS") } },
            "continuation held: " + rev.new_card + " (" + (rev.new_card_status || "held") + ")"),
          h("button", {
            type: "button",
            className: "hermes-kanban-workflow-btn",
            "data-workflow-open-new": "true",
            onClick: function () { if (props.onOpenTask && rev.new_card) props.onOpenTask(rev.new_card); },
          }, "Open new card"));
      } else if (result.state === "UNKNOWN") {
        resultBlock = h("div", { className: "hermes-kanban-workflow-result", "data-workflow-continue-result": "true" },
          h("div", { className: "text-xs", style: { color: workflowStateTone("UNKNOWN") } },
            "UNKNOWN: " + (result.reason || "inspect the result before retrying") + ". Inspect before retrying."));
      } else if (result.fingerprint_mismatch === true || /fingerprint|stale|re-draft/i.test(result.reason || "")) {
        resultBlock = h("div", { className: "hermes-kanban-workflow-result", "data-workflow-continue-result": "true" },
          h("div", { className: "text-xs", style: { color: workflowStateTone("FAIL") } },
            "Stale draft: " + (result.reason || "the fingerprint no longer matches") + ". Re-draft before continuing."));
      } else {
        resultBlock = h("div", { className: "hermes-kanban-workflow-result", "data-workflow-continue-result": "true" },
          h("div", { className: "text-xs", style: { color: workflowStateTone("FAIL") } },
            (result.reason || "continuation failed")),
          result.remedy ? h("div", { className: "text-xs text-muted-foreground" }, result.remedy) : null);
      }
    }

    return h("div", { className: "hermes-kanban-section", "data-workflow-continuation": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Continuation"),
      h("div", { className: "text-xs text-muted-foreground" },
        "Draft is read-only. Creating the held continuation card is a separate click; it never dispatches."),
      h("textarea", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-cont-note": "true",
        rows: 2,
        placeholder: "Verification note (parent-supplied)",
        value: note,
        onChange: function (e) { setNote(e.target.value); },
      }),
      h("textarea", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-cont-passed": "true",
        rows: 2,
        placeholder: "Passed checks (one per line)",
        value: passedText,
        onChange: function (e) { setPassedText(e.target.value); },
      }),
      h("textarea", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-cont-remaining": "true",
        rows: 2,
        placeholder: "Remaining checks (one per line)",
        value: remainingText,
        onChange: function (e) { setRemainingText(e.target.value); },
      }),
      h("textarea", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-cont-evidence": "true",
        rows: 1,
        placeholder: "Evidence per remaining check (one per line, optional)",
        value: evidenceText,
        onChange: function (e) { setEvidenceText(e.target.value); },
      }),
      h("textarea", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-cont-acceptance": "true",
        rows: 1,
        placeholder: "Acceptance per remaining check (one per line, optional)",
        value: acceptanceText,
        onChange: function (e) { setAcceptanceText(e.target.value); },
      }),
      h("div", { className: "hermes-kanban-workflow-comm" },
        h("input", {
          className: "hermes-kanban-workflow-input",
          "data-workflow-cont-workspace": "true",
          placeholder: "workspace",
          value: workspace,
          onChange: function (e) { setWorkspace(e.target.value); },
        }),
        h("input", {
          className: "hermes-kanban-workflow-input",
          "data-workflow-cont-profile": "true",
          placeholder: "profile",
          value: profile,
          onChange: function (e) { setProfile(e.target.value); },
        }),
        h("input", {
          className: "hermes-kanban-workflow-input",
          "data-workflow-cont-provider": "true",
          placeholder: "provider",
          value: provider,
          onChange: function (e) { setProvider(e.target.value); },
        }),
        h("input", {
          className: "hermes-kanban-workflow-input",
          "data-workflow-cont-model": "true",
          placeholder: "model",
          value: model,
          onChange: function (e) { setModel(e.target.value); },
        }),
        h("input", {
          className: "hermes-kanban-workflow-input",
          "data-workflow-cont-title": "true",
          placeholder: "title (optional)",
          value: title,
          onChange: function (e) { setTitle(e.target.value); },
        })),
      h("button", {
        type: "button",
        className: "hermes-kanban-workflow-btn",
        "data-workflow-draft": "true",
        disabled: drafting,
        onClick: doDraft,
      }, drafting ? "Drafting\u2026" : "Draft continuation"),
      draftErr ? h("div", { className: "text-xs", style: { color: workflowStateTone("FAIL") } }, draftErr) : null,
      draft
        ? h("div", { className: "hermes-kanban-workflow-draft", "data-workflow-draft-result": "true" },
            h("div", { className: "text-xs" },
              h("span", { style: { fontWeight: "600" } }, "fingerprint "),
              h("code", { className: "hermes-kanban-workflow-fingerprint" }, String(draft.fingerprint).slice(0, 16) + "\u2026")),
            h("div", { className: "text-xs" },
              "worker: " + ((draft.worker && draft.worker.verdict) || "unknown") +
              ((draft.worker && draft.worker.reason) ? " · " + draft.worker.reason : "")),
            draftOriginal(draft),
            draftChecks(draft),
            draft.no_mutation_performed
              ? h("div", { className: "text-xs text-muted-foreground" }, "no mutation performed")
              : null,
            h("button", {
              type: "button",
              className: "hermes-kanban-workflow-btn",
              "data-workflow-continue": "true",
              disabled: continuing,
              onClick: doContinue,
            }, continuing ? "Creating\u2026" : "Create continuation"))
        : null,
      resultBlock);
  }

  // K8 — review hold: separate explicit click; preserves history, never stops
  // a live worker.
  function WorkflowHoldSection(props) {
    const [reason, setReason] = useState("");
    const [receipt, setReceipt] = useState(null);
    const [loading, setLoading] = useState(false);

    function doHold() {
      if (!reason.trim()) return;
      setLoading(true);
      setReceipt(null);
      return postWorkflow(props.boardSlug, "/workflow/hold", {
        card: props.cardId,
        reason: reason.trim(),
      }).then(function (env) {
        setLoading(false);
        setReceipt(env);
      });
    }

    return h("div", { className: "hermes-kanban-section", "data-workflow-hold": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Hold for review"),
      h("div", { className: "text-xs text-muted-foreground" },
        "Holding blocks dispatch, not the process. A live worker may still write its workspace."),
      h("input", {
        className: "hermes-kanban-workflow-input",
        "data-workflow-hold-reason": "true",
        placeholder: "Hold reason",
        value: reason,
        onChange: function (e) { setReason(e.target.value); },
      }),
      h("button", {
        type: "button",
        className: "hermes-kanban-workflow-btn",
        "data-workflow-hold-submit": "true",
        disabled: loading || !reason.trim(),
        onClick: doHold,
      }, loading ? "Holding\u2026" : "Hold card"),
      receipt
        ? h("div", { className: "hermes-kanban-workflow-result", "data-workflow-hold-result": "true" },
            receipt.state === "PASS"
              ? h("div", { className: "text-xs", style: { color: workflowStateTone("PASS") } },
                  "held: " + props.cardId + " (preserves history; does not stop a live worker)")
              : h("div", { className: "text-xs", style: { color: workflowStateTone(receipt.state) } },
                  (receipt.state || "UNKNOWN") + ": " + (receipt.reason || "hold did not complete")),
            receipt.evidence && receipt.evidence.warning
              ? h("div", { className: "text-xs text-muted-foreground" }, receipt.evidence.warning)
              : null)
        : null);
  }

  // The SDK's Select component fires ``onValueChange(value)`` directly
  // (it's a shadcn-style popup, not a native <select>). Older plugin
  // code calls ``onChange({target: {value}})`` which silently never
  // fires. This helper wires both signatures so a setter works with
  // either API — use it as:
  //
  //   h(Select, {..., ...selectChangeHandler(setState), ...})
  function selectChangeHandler(setter) {
    return {
      onValueChange: function (v) { setter(v == null ? "" : v); },
      onChange: function (e) {
        const v = e && e.target ? e.target.value : e;
        setter(v == null ? "" : v);
      },
    };
  }

  // -------------------------------------------------------------------------
  // Minimal safe markdown renderer.
  //
  // Recognises a small subset (headings, bold, italic, inline code, fenced
  // code, links, bullet lists, paragraphs). HTML escaping first, then
  // inline replacements against the escaped string — no raw HTML from the
  // user is ever executed.
  // -------------------------------------------------------------------------

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function renderInline(esc) {
    // Fenced code has already been extracted before this runs; process
    // inline replacements on the escaped string.
    return esc
      // inline code
      .replace(/`([^`\n]+)`/g, (_m, c) => `<code>${c}</code>`)
      // bold
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      // italic
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      // safe links — only http(s) and mailto
      .replace(
        /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|mailto:[^\s)]+)\)/g,
        (_m, text, href) =>
          `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`,
      );
  }
  function renderMarkdown(src) {
    if (!src) return "";
    // Split out fenced code blocks first so their contents aren't mangled.
    const blocks = [];
    let working = String(src).replace(/```([\s\S]*?)```/g, (_m, code) => {
      blocks.push(code);
      return `\u0000CODE${blocks.length - 1}\u0000`;
    });
    const escaped = escapeHtml(working);
    const lines = escaped.split(/\r?\n/);
    const out = [];
    let inList = false;
    for (const raw of lines) {
      const line = raw;
      const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (bullet) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${renderInline(bullet[1])}</li>`);
        continue;
      }
      if (inList) { out.push("</ul>"); inList = false; }
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      } else if (line.trim() === "") {
        out.push("");
      } else {
        out.push(`<p>${renderInline(line)}</p>`);
      }
    }
    if (inList) out.push("</ul>");
    let html = out.join("\n");
    // Re-insert fenced code blocks.
    html = html.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) =>
      `<pre class="hermes-kanban-md-code"><code>${escapeHtml(blocks[Number(i)])}</code></pre>`,
    );
    return html;
  }
  const MARKDOWN_ALLOWED_TAGS = new Set([
    "a",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "li",
    "p",
    "pre",
    "strong",
    "ul",
  ]);
  function escapeAttribute(value) {
    return escapeHtml(value).replace(/`/g, "&#96;");
  }
  function sanitizeMarkdownAttrs(tag, attrs) {
    if (tag === "a") {
      const hrefMatch =
        /\shref=(["'])(.*?)\1/i.exec(attrs) ||
        /\shref=([^\s>]+)/i.exec(attrs);
      const href = hrefMatch ? (hrefMatch[2] || hrefMatch[1] || "").trim() : "";
      if (!/^(https?:\/\/|mailto:)/i.test(href)) return "";
      return ` href="${escapeAttribute(href)}" target="_blank" rel="noopener noreferrer"`;
    }
    if (tag === "pre" && /\sclass=(["'])hermes-kanban-md-code\1/i.test(attrs)) {
      return ' class="hermes-kanban-md-code"';
    }
    return "";
  }
  function sanitizeMarkdownHtml(html) {
    return String(html || "").replace(
      /<\/?([a-zA-Z][A-Za-z0-9-]*)([^>]*)>/g,
      (match, rawTag, attrs) => {
        const tag = rawTag.toLowerCase();
        if (!MARKDOWN_ALLOWED_TAGS.has(tag)) return "";
        if (/^<\s*\//.test(match)) return `</${tag}>`;
        return `<${tag}${sanitizeMarkdownAttrs(tag, attrs || "")}>`;
      },
    );
  }

  function MarkdownBlock(props) {
    const enabled = props.enabled !== false;
    if (!enabled) {
      return h("pre", { className: "hermes-kanban-pre" }, props.source || "");
    }
    return h("div", {
      className: "hermes-kanban-md",
      dangerouslySetInnerHTML: { __html: sanitizeMarkdownHtml(renderMarkdown(props.source || "")) },
    });
  }

  // -------------------------------------------------------------------------
  // Touch drag-drop helper.
  //
  // HTML5 DnD is desktop-only. On touch devices we attach a pointerdown
  // handler that simulates a drag proxy and fires a custom event on the
  // column under the finger when released. Columns listen for both the
  // standard `drop` event and our `hermes-kanban:drop` event.
  // -------------------------------------------------------------------------

  function attachTouchDrag(el, taskId) {
    if (!el) return;
    function onDown(e) {
      if (e.pointerType !== "touch") return;
      e.preventDefault();
      const proxy = el.cloneNode(true);
      proxy.classList.add("hermes-kanban-touch-proxy");
      document.body.appendChild(proxy);
      let lastTarget = null;

      function move(ev) {
        proxy.style.left = `${ev.clientX - proxy.offsetWidth / 2}px`;
        proxy.style.top = `${ev.clientY - 24}px`;
        proxy.style.display = "none";
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        proxy.style.display = "";
        const col = under && under.closest && under.closest("[data-kanban-column]");
        const trash = under && under.closest && under.closest("[data-kanban-trash]");
        const target = col || trash;
        if (target !== lastTarget) {
          if (lastTarget) lastTarget.classList.remove("hermes-kanban-column--drop");
          if (target) target.classList.add("hermes-kanban-column--drop");
          lastTarget = target;
        }
      }
      function up() {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        document.removeEventListener("pointercancel", up);
        if (lastTarget) {
          lastTarget.classList.remove("hermes-kanban-column--drop");
          const status = lastTarget.getAttribute("data-kanban-column");
          const isTrash = lastTarget.hasAttribute("data-kanban-trash");
          if (isTrash) {
            lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:delete", {
              detail: { taskId },
              bubbles: true,
            }));
          } else if (status) {
            lastTarget.dispatchEvent(new CustomEvent("hermes-kanban:drop", {
              detail: { taskId, status },
              bubbles: true,
            }));
          }
        }
        proxy.remove();
      }
      // Kick off proxy at the pointer origin.
      proxy.style.position = "fixed";
      proxy.style.pointerEvents = "none";
      proxy.style.opacity = "0.85";
      proxy.style.zIndex = "9999";
      proxy.style.width = `${el.offsetWidth}px`;
      proxy.style.left = `${e.clientX - el.offsetWidth / 2}px`;
      proxy.style.top = `${e.clientY - 24}px`;
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
      document.addEventListener("pointercancel", up);
    }
    el.addEventListener("pointerdown", onDown);
    return function () { el.removeEventListener("pointerdown", onDown); };
  }

  // -------------------------------------------------------------------------
  // Error boundary
  // -------------------------------------------------------------------------

  // Wrap the boundary's fallback in a tiny function component so we can
  // call useI18n() — class components can't use hooks directly.
  function ErrorBoundaryFallback(props) {
    const { t } = useI18n();
    return h(Card, null,
      h(CardContent, { className: "p-6 text-sm" },
        h("div", { className: "text-destructive font-semibold mb-1" },
          tx(t, "renderingError", "Kanban tab hit a rendering error")),
        h("div", { className: "text-muted-foreground text-xs mb-3" },
          props.message),
        h(Button, {
          onClick: props.onReset,
          size: "sm",
        }, tx(t, "reloadView", "Reload view")),
      ),
    );
  }

  class ErrorBoundary extends React.Component {
    constructor(props) { super(props); this.state = { error: null }; }
    static getDerivedStateFromError(error) { return { error }; }
    componentDidCatch(error, info) {
      // eslint-disable-next-line no-console
      console.error("Kanban plugin crashed:", error, info);
    }
    render() {
      if (this.state.error) {
        return h(ErrorBoundaryFallback, {
          message: String(this.state.error && this.state.error.message || this.state.error),
          onReset: () => this.setState({ error: null }),
        });
      }
      return this.props.children;
    }
  }

  // -------------------------------------------------------------------------
  // Dialog renderer
  // -------------------------------------------------------------------------

  /**
   * Single component that owns the kanban plugin's modal dialog UI. Renders
   * whichever dialog `useKanbanDialogs` is currently requesting, or nothing
   * if no dialog is open.
   *
   * Currently supports one dialog kind:
   *   - "confirm"  → standard ConfirmDialog (title + description + buttons)
   *
   * The "completion" kind (Mark Done → textarea prompt) is not yet wired
   * because the host's ConfirmDialog hardcodes `onClick → unmount`, which
   * prevents keeping the dialog open across a validation failure. See
   * issue #50547 followups. Completion summaries triggered from the
   * side-drawer use a documented carve-out (`withCompletionSummary` in
   * TaskDetail) until that lands.
   */
  function KanbanDialogs(props) {
    const { dialogProps, dialogState } = props;
    if (!dialogState || !dialogProps) return null;
    const ConfirmDialog = SDK.components.ConfirmDialog;
    if (!ConfirmDialog) return null;
    return h(ConfirmDialog, dialogProps);
  }

  // -------------------------------------------------------------------------
  // Root page
  // -------------------------------------------------------------------------

  function KanbanPage() {
    const { t } = useI18n();
    const kanbanDialogs = useKanbanDialogs(t);
    const [board, setBoard] = useState(() => readSelectedBoard() || null);
    const [boardList, setBoardList] = useState([]);      // [{slug, name, counts, ...}]
    const [showNewBoard, setShowNewBoard] = useState(false);
    const [showBoardSettings, setShowBoardSettings] = useState(false);

    const [kanbanBoard, setKanbanBoard] = useState(null);  // the grid data

    // Read-only shared-evidence layer (K3/K4/K9/K10): identity alignment,
    // bounded snapshot + counts + load more, per-card worker map, freshness.
    // Gated on /evidence/context alignment, generation-guarded per board.
    const evidence = useKanbanEvidence(board);
    const evidenceAligned = evidence.aligned;
    // When the board is identity-aligned with the EVO database the snapshot
    // is the single source of truth: the grid is derived from the bounded
    // snapshot cards, and the local /board grid data is not read. When not
    // aligned, the original local /board grid is unchanged.
    const snapshotBoard = useMemo(function () {
      if (!evidenceAligned) return null;
      const byStatus = {};
      (evidence.cards || []).forEach(function (c) {
        const s = c.status || "todo";
        (byStatus[s] = byStatus[s] || []).push(c);
      });
      return {
        columns: COLUMN_ORDER.map(function (name) {
          return { name: name, tasks: byStatus[name] || [] };
        }),
        latest_event_id: 0,
        assignees: [],
      };
    }, [evidenceAligned, evidence.cards]);

    // R9: held cards (blocked/triage) currently visible in the snapshot,
    // offered as the batch-readiness selection pool. Preview only — never
    // bulk release.
    const heldCards = useMemo(function () {
      if (!evidenceAligned) return [];
      return (evidence.cards || []).filter(function (c) {
        return c && (c.status === "blocked" || c.status === "triage");
      }).map(function (c) { return { id: c.id, title: c.title || c.id, status: c.status }; });
    }, [evidenceAligned, evidence.cards]);

    // Alias so the rest of the function can keep using `board` semantically
    // for the grid data (card columns + tenants + assignees) without
    // colliding with the selected-board slug above. History: the old
    // component had `const [board, setBoard]` for the grid data. We
    // renamed the grid data to `kanbanBoard` so the more useful name
    // (`board`) belongs to the selected slug.
    const boardData = evidenceAligned ? snapshotBoard : kanbanBoard;
    const setBoardData = setKanbanBoard;
    const [config, setConfig] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    const [tenantFilter, setTenantFilter] = useState("");
    const [assigneeFilter, setAssigneeFilter] = useState("");
    const [includeArchived, setIncludeArchived] = useState(false);
    const [search, setSearch] = useState("");
    const [laneByProfile, setLaneByProfile] = useState(true);
    const [configApplied, setConfigApplied] = useState(false);

    const [selectedTaskId, setSelectedTaskId] = useState(null);
    const [selectedIds, setSelectedIds] = useState(() => new Set());
    const [lastSelectedId, setLastSelectedId] = useState(null);
    const [failedIds, setFailedIds] = useState(() => new Set());
    const [draggingTaskId, setDraggingTaskId] = useState(null);
    const handleDragStart = useCallback(function (taskId) { setDraggingTaskId(taskId); }, []);
    const handleDragEnd = useCallback(function () { setDraggingTaskId(null); }, []);
    // Per-task event counter incremented whenever the WS stream reports
    // a new event for that task id. TaskDrawer useEffect-depends on its
    // own task's counter so it reloads itself on live events instead of
    // showing stale data.
    const [taskEventTick, setTaskEventTick] = useState({});

    // K7 notification region state. The WebSocket onmessage feeds
    // ingestNoticeEvents; a per-board ref holds the baseline + dedup set so
    // switching boards isolates notifications and duplicate frames collapse.
    const [workflowNotices, setWorkflowNotices] = useState([]);
    const [noticeBaselineState, setNoticeBaselineState] = useState(null); // null | "unknown" | number
    const noticeRef = useRef({ board: null, baseline: null, seen: {}, interventions: {} });
    const noticeGenRef = useRef(0);

    const resetNotices = useCallback(function (slug) {
      noticeGenRef.current += 1;
      noticeRef.current = { board: slug || null, baseline: null, seen: {}, interventions: {} };
      setWorkflowNotices([]);
      setNoticeBaselineState(null);
    }, []);

    // Establish the notification baseline from GET /events/baseline (one
    // MAX(id) read from the same selected-server DB as the existing /events
    // stream). Strict gate: PASS state, exact board, and a nonnegative integer
    // baseline id. Anything else (FAIL/UNKNOWN/malformed baseline or a wrong
    // board) is "unknown": historical notifications are suppressed and the UI
    // offers a retry, it never replays history.
    const establishNoticeBaseline = useCallback(function (slug) {
      noticeGenRef.current += 1;
      const gen = noticeGenRef.current;
      noticeRef.current = { board: slug, baseline: null, seen: {}, interventions: {} };
      setWorkflowNotices([]);
      setNoticeBaselineState(null);
      return fetchEventsBaseline(slug).then(function (env) {
        if (gen !== noticeGenRef.current) return; // board switched mid-flight
        const data = (env && env.evidence && typeof env.evidence === "object") ? env.evidence : {};
        const stateOk = env && env.state === "PASS";
        const boardOk = data.board === slug;
        const baselineId = data.baseline_id;
        const baselineOk = typeof baselineId === "number" && Number.isFinite(baselineId)
          && baselineId >= 0 && Math.floor(baselineId) === baselineId;
        if (stateOk && boardOk && baselineOk) {
          noticeRef.current.baseline = baselineId;
          setNoticeBaselineState(baselineId);
        } else {
          noticeRef.current.baseline = "unknown";
          setNoticeBaselineState("unknown");
        }
      }).catch(function () {
        if (gen !== noticeGenRef.current) return;
        noticeRef.current.baseline = "unknown";
        setNoticeBaselineState("unknown");
      });
    }, []);

    // Feed WS events into the notice region. Only events for the current board,
    // after a valid baseline, of a notification-worthy kind, and not already
    // seen, are surfaced. Replayed/historical and duplicate ids are dropped.
    // R5: an UNCHANGED repeated intervention (same task+kind+reason+remedy+
    // run) stays quiet; a changed reason/remedy or a new run notifies again.
    // Recovery events (status changes away from held states) clear the stored
    // fingerprint so a recurrence is fresh again.
    const ingestNoticeEvents = useCallback(function (slug, events) {
      const nr = noticeRef.current;
      if (!nr || nr.board !== slug) return;      // late frame from another board
      if (nr.baseline == null || nr.baseline === "unknown") return; // suppressed
      const baseline = nr.baseline;
      if (!Array.isArray(events)) return;
      const fresh = [];
      for (let i = 0; i < events.length; i += 1) {
        const e = events[i];
        if (!e || e.id == null) continue;
        if (!Object.prototype.hasOwnProperty.call(NOTICE_KINDS, e.kind)) continue;
        if (e.id <= baseline) continue;          // historical / replay
        if (nr.seen[e.id]) continue;             // duplicate frame
        nr.seen[e.id] = true;
        // R5 fingerprint gate: quiet on unchanged repeat, notify on change.
        if (Object.prototype.hasOwnProperty.call(NOTICE_KINDS, e.kind) && NOTICE_INTERVENTION_KINDS[e.kind]) {
          const fp = interventionFingerprint(e);
          const prevFp = nr.interventions && nr.interventions[e.task_id || ""];
          if (nr.interventions) nr.interventions[e.task_id || ""] = fp;
          if (prevFp === fp) continue;           // unchanged repeat: quiet
        } else if (nr.interventions && e.task_id && NOTICE_RECOVERY_KINDS[e.kind]) {
          // Recovery clears the stored fingerprint for that task.
          delete nr.interventions[e.task_id];
        }
        fresh.push({ id: e.id, kind: e.kind, task_id: e.task_id, created_at: e.created_at,
                     reason: e.reason, remedy: e.remedy, run_id: e.run_id });
      }
      if (fresh.length === 0) return;
      setWorkflowNotices(function (prev) {
        const merged = fresh.concat(prev);
        const seenIds = {};
        const out = [];
        for (let i = 0; i < merged.length; i += 1) {
          const n = merged[i];
          if (seenIds[n.id]) continue;
          seenIds[n.id] = true;
          out.push(n);
        }
        return out.slice(0, NOTICE_MAX);
      });
    }, []);

    const dismissNotice = useCallback(function (id) {
      setWorkflowNotices(function (prev) { return prev.filter(function (n) { return n.id !== id; }); });
    }, []);
    const clearNotices = useCallback(function () {
      setWorkflowNotices([]);
    }, []);

    const cursorRef = useRef(0);
    const reloadTimerRef = useRef(null);
    const wsRef = useRef(null);
    const wsBackoffRef = useRef(1000);
    const wsClosedRef = useRef(false);

    // --- load config once ---------------------------------------------------
    useEffect(function () {
      SDK.fetchJSON(withBoard(`${API}/config`, board))
        .then(function (c) {
          setConfig(c);
          if (!configApplied) {
            if (c.default_tenant) setTenantFilter(c.default_tenant);
            if (typeof c.lane_by_profile === "boolean") setLaneByProfile(c.lane_by_profile);
            if (typeof c.include_archived_by_default === "boolean") setIncludeArchived(c.include_archived_by_default);
            setConfigApplied(true);
          }
        })
        .catch(function () { setConfig({ render_markdown: true }); });
    }, []);  // eslint-disable-line react-hooks/exhaustive-deps

    // --- fetch full board ---------------------------------------------------
    const loadBoard = useCallback(() => {
      if (evidenceAligned) {
        // Aligned: the snapshot is the single source of truth. The original
        // WebSocket subscription stays live and its event callback (via
        // scheduleReload) routes here, refreshing the snapshot instead of
        // the local /board. Board actions call this same callback.
        return evidence.refresh().finally(function () { setLoading(false); });
      }
      if (!evidence.context && !evidence.ctxErr) {
        // Alignment still unresolved: hold off fetching the local /board so an
        // aligned EVO board never performs a /board read that would be
        // discarded the moment /evidence/context resolves.
        return Promise.resolve();
      }
      const qs = new URLSearchParams();
      if (tenantFilter) qs.set("tenant", tenantFilter);
      if (includeArchived) qs.set("include_archived", "true");
      const url = qs.toString() ? `${API}/board?${qs}` : `${API}/board`;
      return SDK.fetchJSON(withBoard(url, board))
        .then(function (data) {
          setBoardData(data);
          cursorRef.current = data.latest_event_id || 0;
          setError(null);
        })
        .catch(function (err) {
          setError(String(err && err.message ? err.message : err));
        })
        .finally(function () { setLoading(false); });
    }, [tenantFilter, includeArchived, board, evidenceAligned, evidence.context, evidence.ctxErr, evidence.refresh]);

    // --- load list of boards for the switcher ------------------------------
    const loadBoardList = useCallback(function () {
      return SDK.fetchJSON(withBoard(`${API}/boards`, board))
        .then(function (data) {
          const boards = (data && data.boards) || [];
          const storedBoard = readSelectedBoard();
          setBoardList(boards);
          if (!storedBoard && !board && data && data.current) {
            setBoard(data.current);
            return;
          }
          // If the stored slug isn't in the list any longer (board was
          // deleted in the CLI while dashboard was open), fall back to
          // default so the UI doesn't hang on a 404.
          if (board && board !== "default" && !boards.find(function (b) { return b.slug === board; })) {
            setBoard("default");
            writeSelectedBoard("default");
          }
        })
        .catch(function () { /* non-fatal */ });
    }, [board]);

    useEffect(function () { loadBoardList(); }, [loadBoardList]);

    const scheduleReload = useCallback(function () {
      if (reloadTimerRef.current) return;
      reloadTimerRef.current = setTimeout(function () {
        reloadTimerRef.current = null;
        loadBoard();
      }, 250);
    }, [loadBoard]);

    useEffect(function () {
      loadBoard();
      return function () {
        if (reloadTimerRef.current) {
          clearTimeout(reloadTimerRef.current);
          reloadTimerRef.current = null;
        }
      };
    }, [loadBoard]);

    // --- WebSocket ---------------------------------------------------------
    useEffect(function () {
      if (!boardData) return undefined;
      wsClosedRef.current = false;
      function openWs() {
        if (wsClosedRef.current) return;
        // Build the WS URL via the host SDK so the correct auth param is used
        // in BOTH modes: single-use ?ticket= in gated OAuth mode, ?token= in
        // loopback. Reading window.__HERMES_SESSION_TOKEN__ directly (the old
        // path) sends an empty token and is rejected in gated mode. buildWsUrl
        // also applies the dashboard base-path prefix for reverse-proxied
        // deployments, which the old inline URL did not. It's async (gated
        // mode mints a fresh ticket per connect), so resolve then open.
        const wsParams = { since: String(cursorRef.current || 0) };
        // Pin the WS stream to the currently-selected board so events
        // from other boards don't bleed in. Includes "default" so the
        // dashboard's own board pin always wins over the server-side
        // ``current`` file — same rationale as ``withBoard()`` above.
        // Regression: #20879.
        if (board) wsParams.board = board;
        SDK.buildWsUrl(`${API}/events`, wsParams).then(function (url) {
          if (wsClosedRef.current) return;
          let ws;
          try { ws = new WebSocket(url); } catch (_e) { return; }
          wsRef.current = ws;
          ws.onopen = function () { wsBackoffRef.current = 1000; };
          ws.onmessage = function (ev) {
            try {
              const msg = JSON.parse(ev.data);
              if (msg && Array.isArray(msg.events) && msg.events.length > 0) {
                cursorRef.current = msg.cursor || cursorRef.current;
                // Stamp per-task signal so the TaskDrawer can reload itself.
                setTaskEventTick(function (prev) {
                  const next = Object.assign({}, prev);
                  for (const e of msg.events) {
                    if (e && e.task_id) next[e.task_id] = (next[e.task_id] || 0) + 1;
                  }
                  return next;
                });
                scheduleReload();
                // K7: feed notification-worthy events into the in-page notice
                // region (extended existing stream, never a second notifier).
                ingestNoticeEvents(board, msg.events);
              }
            } catch (_e) { /* ignore */ }
          };
          ws.onclose = function (ev) {
            if (wsClosedRef.current) return;
            if (ev && ev.code === 1008) {
              setError(tx(t, "wsAuthFailed",
                "WebSocket auth failed — reload the page to refresh the session token."));
              return;
            }
            const delay = Math.min(wsBackoffRef.current, 30000);
            wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
            setTimeout(openWs, delay);
          };
        }).catch(function () {
          // Ticket mint / URL build failed (e.g. session expired). Back off
          // and retry; a hard auth failure surfaces via the 1008 close path.
          if (wsClosedRef.current) return;
          const delay = Math.min(wsBackoffRef.current, 30000);
          wsBackoffRef.current = Math.min(wsBackoffRef.current * 2, 30000);
          setTimeout(openWs, delay);
        });
      }
      openWs();
      return function () {
        wsClosedRef.current = true;
        try { wsRef.current && wsRef.current.close(); } catch (_e) { /* noop */ }
      };
    }, [!!boardData, board, scheduleReload, ingestNoticeEvents]);

    // K7: (re)establish the notification baseline whenever the selected board
    // or alignment changes. When not aligned there is no EVO evidence stream,
    // so any prior notices are cleared and no baseline is attempted.
    useEffect(function () {
      if (!board) return undefined;
      if (!evidenceAligned) {
        resetNotices(board);
        return undefined;
      }
      establishNoticeBaseline(board);
    }, [board, evidenceAligned, establishNoticeBaseline, resetNotices]);

    // --- filtering ----------------------------------------------------------
    const filteredBoard = useMemo(function () {
      if (!boardData) return null;
      const q = search.trim().toLowerCase();
      const filterTask = function (t) {
        if (tenantFilter && t.tenant !== tenantFilter) return false;
        if (assigneeFilter && t.assignee !== assigneeFilter) return false;
        if (q) {
          const hay = `${t.id} ${t.title || ""} ${t.body || ""} ${t.result || ""} ${t.latest_summary || ""} ${t.assignee || ""} ${t.tenant || ""}`.toLowerCase();
          if (hay.indexOf(q) === -1) return false;
        }
        return true;
      };
      return Object.assign({}, boardData, {
        columns: boardData.columns.map(function (col) {
          return Object.assign({}, col, { tasks: col.tasks.filter(filterTask) });
        }),
      });
    }, [boardData, tenantFilter, assigneeFilter, search]);

    // --- actions ------------------------------------------------------------
    // Performs the actual move (optimistic UI + PATCH) once any required
    // confirmation and/or completion summary has been collected by the
    // caller. Extracted so moveTask / moveSelected / applyBulk can all
    // share the same dispatch path regardless of how confirmation was
    // collected (synchronous window.confirm in the original code, async
    // dialog via useKanbanDialogs now).
    //   taskId  — required when count <= 1 (single-task PATCH endpoint)
    //           — ignored when count >  1 (bulk endpoint uses selectedIds)
    //   summary     — completion summary string, or null/undefined to skip
    //   blockReason — required explanation when moving to blocked
    const performMoveTask = useCallback(function (taskId, newStatus, count, summary, blockReason) {
      const patch = { status: newStatus };
      const finalPatch = Object.assign(
        {},
        patch,
        summary ? { result: summary, summary: summary } : {},
        blockReason ? { block_reason: blockReason } : {},
      );
      if (count > 1) {
        // Bulk path: optimistic UI prepends all moved tasks to dest column.
        setBoardData(function (b) {
          if (!b) return b;
          const moved = [];
          const columns = b.columns.map(function (col) {
            const kept = [];
            for (const tk of col.tasks) {
              if (selectedIds.has(tk.id)) moved.push(Object.assign({}, tk, { status: newStatus }));
              else kept.push(tk);
            }
            return Object.assign({}, col, { tasks: kept });
          });
          const dest = columns.find(function (c) { return c.name === newStatus; });
          if (dest) dest.tasks = moved.concat(dest.tasks);
          return Object.assign({}, b, { columns });
        });
        const ids = Array.from(selectedIds);
        SDK.fetchJSON(withBoard(`${API}/tasks/bulk`, board), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({ ids: ids }, finalPatch)),
        }).then(function (res) {
          const failed = (res.results || []).filter(function (r) { return !r.ok; });
          if (failed.length > 0) {
            setError(`Bulk move: ${failed.length} of ${res.results.length} failed`);
            setFailedIds(new Set(failed.map(function (f) { return f.id; })));
          } else {
            setFailedIds(new Set());
          }
          setSelectedIds(new Set());
          setLastSelectedId(null);
          loadBoard();
        }).catch(function (err) {
          setError(`Move failed: ${err.message || err}`);
          setFailedIds(new Set(selectedIds));
          loadBoard();
        });
        return;
      }
      // Single-task path.
      setBoardData(function (b) {
        if (!b) return b;
        let moved = null;
        const columns = b.columns.map(function (col) {
          const next = col.tasks.filter(function (tk) {
            if (tk.id === taskId) { moved = Object.assign({}, tk, { status: newStatus }); return false; }
            return true;
          });
          return Object.assign({}, col, { tasks: next });
        });
        if (moved) {
          const dest = columns.find(function (c) { return c.name === newStatus; });
          if (dest) dest.tasks = [moved].concat(dest.tasks);
        }
        return Object.assign({}, b, { columns });
      });
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(taskId)}`, board), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(finalPatch),
      }).catch(function (err) {
        setError(tx(t, "moveFailed", "Move failed: ") + parseApiErrorMessage(err));
        loadBoard();
      });
    }, [loadBoard, board, t, selectedIds]);

    // Pre-dispatch dialog step for both moveTask and moveSelected. Drives
    // the new in-app ConfirmDialog instead of window.confirm. The flow:
    //   1. If newStatus is destructive (done/archived/blocked), open
    //      a confirm dialog.
    //   2. If newStatus is "done", additionally open a completion-summary
    //      dialog (chained via Promise).
    //   3. On confirm of all steps, call performMoveTask.
    //   4. On cancel anywhere, do nothing.
    const requestMoveConfirm = useCallback(function (newStatus, count) {
      const confirmMsg = getDestructiveConfirm(t, newStatus, count);
      if (!confirmMsg) return Promise.resolve({ confirmed: true });
      return kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "confirmStatusTitle." + newStatus, "Confirm status change"),
        description: confirmMsg,
        confirmLabel: tx(t, "confirmStatusLabel." + newStatus, "Confirm"),
        destructive: true,
      });
    }, [kanbanDialogs, t]);

    const requestCompletionSummary = useCallback(function (count) {
      const label = dialogLabelForCount(count, t);
      // Uses window.prompt as a documented carve-out — the host's
      // ConfirmDialog hardcodes onClick → unmount (confirmedRef + Radix
      // AlertDialogAction), making it impossible to keep a dialog open
      // across a validation failure. Once ConfirmDialog grows a
      // disabled prop upstream, this switches to a Dialog-based
      // completion body (see KanbanDialogs doc comment).
      var summary = window.prompt(
        tx(t, "completionSummary",
          "Completion summary for {label}. This is stored as the task result.",
          { label: label }),
        "",
      );
      if (summary === null) return Promise.resolve({ confirmed: false });
      summary = summary.trim();
      if (!summary) {
        window.alert(tx(t, "completionSummaryRequired",
          "Completion summary is required before marking a task done."));
        return Promise.resolve({ confirmed: false });
      }
      return Promise.resolve({ confirmed: true, summary: summary });
    }, [t]);

    const requestBlockReason = useCallback(function (count) {
      const label = dialogLabelForCount(count, t);
      var reason = window.prompt(
        tx(t, "blockReasonPrompt",
          "Why is {label} blocked? This reason is shown on the card until it is unblocked.",
          { label: label }),
        "",
      );
      if (reason === null) return Promise.resolve({ confirmed: false });
      reason = reason.trim();
      if (!reason) {
        window.alert(tx(t, "blockReasonRequired",
          "A block reason is required before marking a task blocked."));
        return Promise.resolve({ confirmed: false });
      }
      return Promise.resolve({ confirmed: true, blockReason: reason });
    }, [t]);

    // Single-task card move. Drives confirmation + required status details
    // via the hook, then dispatches via performMoveTask.
    const moveTask = useCallback(function (taskId, newStatus) {
      requestMoveConfirm(newStatus, 1)
        .then(function (r1) {
          if (!r1.confirmed) return null;
          if (newStatus === "blocked") {
            return requestBlockReason(1).then(function (r2) {
              if (!r2.confirmed) return null;
              performMoveTask(taskId, newStatus, 1, null, r2.blockReason);
            });
          }
          if (newStatus !== "done") {
            performMoveTask(taskId, newStatus, 1, null, null);
            return null;
          }
          return requestCompletionSummary(1).then(function (r2) {
            if (!r2.confirmed) return null;
            performMoveTask(taskId, newStatus, 1, r2.summary || null, null);
          });
        })
        .catch(function () { /* dialog cancelled */ });
    }, [requestMoveConfirm, requestCompletionSummary, requestBlockReason, performMoveTask]);

    const clearSelected = useCallback(function () {
      setSelectedIds(new Set());
      setLastSelectedId(null);
      setFailedIds(new Set());
    }, []);
    const moveSelected = useCallback(function (newStatus) {
      if (selectedIds.size === 0) return;
      const count = selectedIds.size;
      const taskId = Array.from(selectedIds)[0]; // representative id for performMoveTask's single-task branch
      requestMoveConfirm(newStatus, count)
        .then(function (r1) {
          if (!r1.confirmed) return null;
          if (newStatus === "blocked") {
            return requestBlockReason(count).then(function (r2) {
              if (!r2.confirmed) return null;
              performMoveTask(taskId, newStatus, count, null, r2.blockReason);
            });
          }
          if (newStatus !== "done") {
            performMoveTask(taskId, newStatus, count, null, null);
            return null;
          }
          return requestCompletionSummary(count).then(function (r2) {
            if (!r2.confirmed) return null;
            performMoveTask(taskId, newStatus, count, r2.summary || null, null);
          });
        })
        .catch(function () { /* dialog cancelled */ });
    }, [selectedIds, requestMoveConfirm, requestCompletionSummary, requestBlockReason, performMoveTask]);

    const createTask = useCallback(function (body) {
      return SDK.fetchJSON(withBoard(`${API}/tasks`, board), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(function (res) {
        // Surface dispatcher-presence warnings (e.g. "no gateway is
        // running") via the existing error banner channel. Not fatal —
        // the task was created successfully — but the user should know
        // their ready task will sit idle until the gateway is up.
        if (res && res.warning) {
          setError(tx(t, "taskCreatedWarning", "Task created, but: ") + res.warning);
        }
        loadBoard();
        loadBoardList();  // refresh counts in the switcher
        return res;
      });
    }, [loadBoard, loadBoardList, board, t]);

    const toggleSelected = useCallback(function (id, additive) {
      setSelectedIds(function (prev) {
        const next = new Set(additive ? prev : []);
        if (prev.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
      setLastSelectedId(id);
      setFailedIds(function (prev) {
        if (prev.has(id)) {
          const next = new Set(prev);
          next.delete(id);
          return next;
        }
        return prev;
      });
    }, []);

    const toggleRange = useCallback(function (toId) {
      // Build flat visible task order from filteredBoard columns.
      setSelectedIds(function (prev) {
        const next = new Set(prev);
        if (!filteredBoard || !filteredBoard.columns) return next;
        const order = [];
        for (const col of filteredBoard.columns) {
          for (const t of col.tasks || []) order.push(t.id);
        }
        const anchor = lastSelectedId;
        if (!anchor || anchor === toId) {
          next.add(toId);
          return next;
        }
        const aIdx = order.indexOf(anchor);
        const bIdx = order.indexOf(toId);
        if (aIdx === -1 || bIdx === -1) {
          next.add(toId);
          return next;
        }
        const lo = Math.min(aIdx, bIdx);
        const hi = Math.max(aIdx, bIdx);
        for (let i = lo; i <= hi; i++) next.add(order[i]);
        return next;
      });
      setLastSelectedId(toId);
    }, [filteredBoard, lastSelectedId]);

    const selectAllVisible = useCallback(function () {
      if (!filteredBoard || !filteredBoard.columns) return;
      const next = new Set();
      for (const col of filteredBoard.columns) {
        for (const t of col.tasks || []) next.add(t.id);
      }
      setSelectedIds(next);
      if (next.size > 0) {
        const first = Array.from(next)[0];
        setLastSelectedId(first);
      }
    }, [filteredBoard]);

    const selectAllInColumn = useCallback(function (columnName) {
      if (!filteredBoard || !filteredBoard.columns) return;
      const col = filteredBoard.columns.find(function (c) { return c.name === columnName; });
      if (!col) return;
      const allSelected = col.tasks && col.tasks.length > 0 && col.tasks.every(function (t) { return selectedIds.has(t.id); });
      const next = new Set(selectedIds);
      if (allSelected) {
        for (const t of col.tasks || []) next.delete(t.id);
      } else {
        for (const t of col.tasks || []) next.add(t.id);
      }
      setSelectedIds(next);
      if (col.tasks && col.tasks.length > 0) setLastSelectedId(col.tasks[0].id);
    }, [filteredBoard, selectedIds]);

    const applyBulk = useCallback(function (patch, confirmMsg) {
      if (selectedIds.size === 0) return;
      const count = selectedIds.size;
      const run = function (finalPatch) {
        const body = Object.assign({ ids: Array.from(selectedIds) }, finalPatch);
        // Optimistic UI for status moves (same pattern as moveSelected).
        if (finalPatch.status) {
          setBoardData(function (b) {
            if (!b) return b;
            const moved = [];
            const columns = b.columns.map(function (col) {
              const kept = [];
              for (const t of col.tasks) {
                if (selectedIds.has(t.id)) moved.push(Object.assign({}, t, { status: finalPatch.status }));
                else kept.push(t);
              }
              return Object.assign({}, col, { tasks: kept });
            });
            const dest = columns.find(function (c) { return c.name === finalPatch.status; });
            if (dest) dest.tasks = moved.concat(dest.tasks);
            return Object.assign({}, b, { columns });
          });
        }
        SDK.fetchJSON(withBoard(`${API}/tasks/bulk`, board), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
          .then(function (res) {
            const failed = (res.results || []).filter(function (r) { return !r.ok; });
            if (failed.length > 0) {
              setError(tx(t, "bulkFailed", "Bulk: ") +
                `${failed.length} of ${res.results.length} failed: ` +
                failed.slice(0, 3).map(function (f) { return `${f.id} (${f.error})`; }).join("; "));
              setFailedIds(new Set(failed.map(function (f) { return f.id; })));
            } else {
              setFailedIds(new Set());
            }
            setSelectedIds(new Set());
            setLastSelectedId(null);
            loadBoard();
          })
          .catch(function (e) {
            setError(String(e.message || e));
            setFailedIds(new Set(selectedIds));
            loadBoard();
          });
      };
      const runWithDetails = function () {
        if (patch.status !== "blocked") {
          run(patch);
          return;
        }
        requestBlockReason(count).then(function (r) {
          if (!r.confirmed) return;
          run(Object.assign({}, patch, { block_reason: r.blockReason }));
        });
      };
      if (!confirmMsg) {
        runWithDetails();
        return;
      }
      kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "bulkConfirmTitle", "Apply bulk change"),
        description: confirmMsg,
        confirmLabel: tx(t, "apply", "Apply"),
        destructive: false,
      }).then(function (r) {
        if (r.confirmed) runWithDetails();
      }).catch(function () { /* cancelled */ });
    }, [selectedIds, loadBoard, board, t, kanbanDialogs, requestBlockReason]);

    // --- board switching ----------------------------------------------------
    const switchBoard = useCallback(function (nextSlug) {
      if (!nextSlug || nextSlug === board) return;
      // Optimistic UI: clear the current grid + show loading, reset the
      // event cursor so the WS reopens aligned to the new board's
      // latest_event_id on the next loadBoard.
      setBoardData(null);
      cursorRef.current = 0;
      setLoading(true);
      setBoard(nextSlug);
      writeSelectedBoard(nextSlug);
      // Reset filters so stale search/tenant/assignee don't persist across boards.
      setSearch("");
      setTenantFilter("");
      setAssigneeFilter("");
      setIncludeArchived(false);
      clearSelected();
    }, [board, clearSelected]);

    const createNewBoard = useCallback(function (payload) {
      return SDK.fetchJSON(`${API}/boards`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) {
        loadBoardList();
        const slug = res && res.board && res.board.slug;
        if (slug && payload.switch) switchBoard(slug);
        return res;
      });
    }, [loadBoardList, switchBoard, board]);

    // PATCH board metadata (name / description / default project directory).
    // Refreshes the board list so InlineCreate's workspace defaults pick up
    // the new default_workdir immediately.
    const updateBoard = useCallback(function (slug, payload) {
      return SDK.fetchJSON(`${API}/boards/${encodeURIComponent(slug)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (res) {
        loadBoardList();
        return res;
      });
    }, [loadBoardList]);

    const deleteBoard = useCallback(function (slug) {
      if (!slug || slug === "default") return Promise.resolve();
      return SDK.fetchJSON(`${API}/boards/${encodeURIComponent(slug)}`, {
        method: "DELETE",
      }).then(function () {
        loadBoardList();
        if (board === slug) switchBoard("default");
      });
    }, [board, loadBoardList, switchBoard]);

   const deleteTask = useCallback(function (taskId) {
     return kanbanDialogs.request({
       kind: "confirm",
       title: tx(t, "trash.confirmTitle", "Delete task?"),
       description: tx(t, "trash.confirm", FALLBACK_TRASH.confirm),
       confirmLabel: tx(t, "common.delete", "Delete"),
       destructive: true,
     }).then(function (r) {
       if (!r.confirmed) return null;
       return SDK.fetchJSON(`${API}/tasks/${encodeURIComponent(taskId)}`, {
         method: "DELETE",
       }).then(function () {
         loadBoard();
         setSelectedIds(function (prev) {
           const next = new Set(prev);
           next.delete(taskId);
           return next;
         });
       }).catch(function (e) { setError(String(e.message || e)); });
     }).catch(function () { /* cancelled */ });
   }, [board, loadBoard, t, kanbanDialogs]);

    const deleteSelected = useCallback(function (count) {
      if (selectedIds.size === 0) return Promise.resolve();
      kanbanDialogs.request({
        kind: "confirm",
        title: tx(t, "trash.confirmManyTitle", "Delete {n} tasks?", { n: count }),
        description: tx(t, "trash.confirmMany", "Permanently delete {n} selected tasks? This cannot be undone.", { n: count }),
        confirmLabel: tx(t, "common.delete", "Delete"),
        destructive: true,
      }).then(function (r) {
        if (!r.confirmed) return null;
        const ids = Array.from(selectedIds);
        setSelectedIds(new Set());
        return Promise.all(ids.map(function (id) {
          return SDK.fetchJSON(`${API}/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
        })).then(function () {
          loadBoard();
        }).catch(function (e) { setError(String(e.message || e)); });
      }).catch(function () { /* cancelled */ });
    }, [selectedIds, board, loadBoard, t, kanbanDialogs]);

    // --- render -------------------------------------------------------------
    if (loading && !boardData) {
      return h("div", { className: "p-8 text-sm text-muted-foreground" },
        tx(t, "loading", "Loading Kanban board…"));
    }
    if (error && !boardData) {
      return h(Card, null,
        h(CardContent, { className: "p-6" },
          h("div", { className: "text-sm text-destructive" },
            tx(t, "loadFailed", "Failed to load Kanban board: "), error),
          h("div", { className: "text-xs text-muted-foreground mt-2" },
            tx(t, "loadFailedHint",
              "The backend auto-creates kanban.db on first read. If this persists, check the dashboard logs.")),
        ),
      );
    }
    if (!filteredBoard) return null;

    const renderMd = !config || config.render_markdown !== false;

    return h(ErrorBoundary, null,
      h("div", { className: "hermes-kanban flex flex-col gap-4" },
        h(BoardSwitcher, {
          board: board,
          boardList: boardList,
          onSwitch: switchBoard,
          onNewClick: function () { setShowNewBoard(true); },
          onSettingsClick: function () { setShowBoardSettings(true); },
          onDeleteBoard: deleteBoard,
          requestDialog: function (req) { return kanbanDialogs.request(req); },
        }),
        showNewBoard ? h(NewBoardDialog, {
          onCancel: function () { setShowNewBoard(false); },
          onCreate: function (payload) {
            return createNewBoard(payload).then(function () { setShowNewBoard(false); });
          },
        }) : null,
        showBoardSettings ? h(BoardSettingsDialog, {
          board: boardList.find(function (item) { return item.slug === board; })
            || { slug: board },
          onCancel: function () { setShowBoardSettings(false); },
          onSave: function (payload) {
            return updateBoard(board, payload).then(function () { setShowBoardSettings(false); });
          },
        }) : null,
        h(OrchestrationPanel, null),
        h(AttentionStrip, {
          boardData,
          onOpen: setSelectedTaskId,
        }),
        h(BoardToolbar, {
          board: boardData,
          tenantFilter, setTenantFilter,
          assigneeFilter, setAssigneeFilter,
          includeArchived, setIncludeArchived,
          laneByProfile, setLaneByProfile,
          search, setSearch,
          onNudgeDispatch: function () {
            SDK.fetchJSON(withBoard(`${API}/dispatch?max=8`, board), { method: "POST" })
              .then(loadBoard)
              .catch(function (e) { setError(String(e.message || e)); });
          },
          onRefresh: loadBoard,
        }),
       selectedIds.size > 0 ? h(BulkActionBar, {
         count: selectedIds.size,
         assignees: (boardData && boardData.assignees) || [],
         onApply: applyBulk,
         onClear: clearSelected,
         onSelectAllVisible: selectAllVisible,
         onDelete: deleteSelected,
       }) : null,
        error ? h("div", { className: "text-xs text-destructive px-2" }, error) : null,
        h(KanbanDialogs, {
          dialogProps: kanbanDialogs.dialogProps,
          dialogState: kanbanDialogs.dialogState,
        }),
        h(EvidenceBanner, { evidence: evidence }),
        h(SupportPanel, {
          boardSlug: board,
          snapshot: evidence.snapshot,
        }),
        evidenceAligned ? h(WorkflowNoticeRegion, {
          notices: workflowNotices,
          baselineState: noticeBaselineState,
          onOpen: setSelectedTaskId,
          onDismiss: dismissNotice,
          onClear: clearNotices,
          onRetry: function () { establishNoticeBaseline(board); },
        }) : null,
        evidenceAligned ? h(WorkflowAttentionSection, { boardSlug: board, onOpen: setSelectedTaskId }) : null,
        evidenceAligned ? h(WorkflowChangesSection, { boardSlug: board }) : null,
        evidenceAligned ? h(ReadinessBatchSection, { boardSlug: board, heldCards: heldCards }) : null,
        h(BoardColumns, {
          board: filteredBoard,
          boardMeta: boardList.find(function (item) { return item.slug === board; }) || null,
          laneByProfile,
          selectedIds,
          failedIds,
          draggingTaskId,
          evidenceWorkerMap: evidence.workerMap,
          evidenceAligned: evidence.aligned,
          evidenceObservedAt: evidence.observedAt,
          onDragStart: handleDragStart,
          onDragEnd: handleDragEnd,
          toggleSelected,
          toggleRange,
          selectAllInColumn,
          onMove: moveTask,
          onMoveSelected: moveSelected,
          onDelete: deleteTask,
          onDeleteSelected: deleteSelected,
          onOpen: setSelectedTaskId,
          onCreate: createTask,
          allTasks: boardData.columns.reduce(function (acc, c) { return acc.concat(c.tasks); }, []),
        }),
        selectedTaskId ? h(TaskDrawer, {
          taskId: selectedTaskId,
          boardSlug: board,
          evidenceAligned: evidence.aligned,
          onClose: function () { setSelectedTaskId(null); },
          onOpenTask: setSelectedTaskId,
          onRefresh: loadBoard,
          renderMarkdown: renderMd,
          allTasks: boardData.columns.reduce(function (acc, c) { return acc.concat(c.tasks); }, []),
          assignees: (boardData && boardData.assignees) || [],
          eventTick: taskEventTick[selectedTaskId] || 0,
          // Hook for the side-drawer's doPatch to use the same in-app
          // dialog machinery as the column-card flow. TaskDetail also
          // owns its own kanbanDialogs so the dialog portal mounts in
          // its tree; we expose requestDialog as the imperative API.
          requestDialog: function (req) { return kanbanDialogs.request(req); },
        }) : null,
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Attention strip — surfaces every task with active diagnostics,
  // severity-marked (warning/error/critical). Collapsed by default; click
  // Show to expand into per-task rows with Open buttons. Dismissible
  // per session via state flag.
  // -------------------------------------------------------------------------

  function collectDiagTasks(boardData) {
    if (!boardData || !boardData.columns) return [];
    const out = [];
    for (const col of boardData.columns) {
      for (const t of col.tasks || []) {
        if (t.diagnostics && t.diagnostics.length > 0) out.push(t);
        else if (t.warnings && t.warnings.count > 0) out.push(t);
      }
    }
    // Sort: highest severity first (critical > error > warning), then by
    // most recent latest_at.
    const sevIdx = function (s) {
      if (s === "critical") return 3;
      if (s === "error") return 2;
      if (s === "warning") return 1;
      return 0;
    };
    out.sort(function (a, b) {
      const aSev = sevIdx((a.warnings && a.warnings.highest_severity) || "warning");
      const bSev = sevIdx((b.warnings && b.warnings.highest_severity) || "warning");
      if (aSev !== bSev) return bSev - aSev;
      const aLa = (a.warnings && a.warnings.latest_at) || 0;
      const bLa = (b.warnings && b.warnings.latest_at) || 0;
      return bLa - aLa;
    });
    return out;
  }

  function AttentionStrip(props) {
    const { t } = useI18n();
    const [expanded, setExpanded] = useState(false);
    const [dismissed, setDismissed] = useState(false);
    const diagTasks = useMemo(
      function () { return collectDiagTasks(props.boardData); },
      [props.boardData]
    );
    if (dismissed || diagTasks.length === 0) return null;
    // Pick the highest severity present so we can colour the strip.
    let topSev = "warning";
    for (const td of diagTasks) {
      const s = (td.warnings && td.warnings.highest_severity) || "warning";
      if (s === "critical") { topSev = "critical"; break; }
      if (s === "error" && topSev !== "critical") topSev = "error";
    }
    return h("div", {
      className: cn(
        "hermes-kanban-attention",
        "hermes-kanban-attention--" + topSev,
      ),
    },
      h("div", { className: "hermes-kanban-attention-bar" },
        h("span", { className: "hermes-kanban-attention-icon" },
          topSev === "critical" ? "!!!" : topSev === "error" ? "!!" : "⚠"),
        h("span", { className: "hermes-kanban-attention-text" },
          diagTasks.length === 1
            ? tx(t, "taskNeedsAttention", "1 task needs attention")
            : tx(t, "tasksNeedAttention", "{n} tasks need attention",
                { n: diagTasks.length }),
        ),
        h("button", {
          className: "hermes-kanban-attention-toggle",
          onClick: function () { setExpanded(function (x) { return !x; }); },
          type: "button",
        }, expanded ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
        h("button", {
          className: "hermes-kanban-attention-dismiss",
          onClick: function () { setDismissed(true); },
          title: "Hide until next page reload",
          type: "button",
        }, "\u2715"),
      ),
      expanded
        ? h("div", { className: "hermes-kanban-attention-list" },
            diagTasks.map(function (task) {
              const sev = (task.warnings && task.warnings.highest_severity) || "warning";
              const kinds = task.warnings && task.warnings.kinds ? Object.keys(task.warnings.kinds) : [];
              return h("div", {
                key: task.id,
                className: cn(
                  "hermes-kanban-attention-row",
                  "hermes-kanban-attention-row--" + sev,
                ),
              },
                h("span", { className: "hermes-kanban-attention-row-sev" },
                  sev === "critical" ? "!!!" : sev === "error" ? "!!" : "⚠"),
                h("span", { className: "hermes-kanban-attention-row-id" }, task.id),
                h("span", { className: "hermes-kanban-attention-row-title" },
                  task.title || tx(t, "untitled", "(untitled)")),
                h("span", { className: "hermes-kanban-attention-row-meta" },
                  task.assignee ? "@" + task.assignee : tx(t, "unassigned", "unassigned"),
                  " \u00b7 ",
                  kinds.length > 0 ? kinds.join(", ") : tx(t, "diagnostic", "diagnostic"),
                ),
                h("button", {
                  className: "hermes-kanban-attention-row-btn",
                  onClick: function () { props.onOpen(task.id); },
                  type: "button",
                }, tx(t, "open", "Open")),
              );
            }),
          )
        : null,
    );
  }

  // -------------------------------------------------------------------------
  // Diagnostics section — generic renderer for a task's active distress
  // signals. Each diagnostic carries its own title, detail, data payload,
  // and a list of structured actions; the section renders them uniformly
  // regardless of kind. Replaces the hallucination-specific
  // ``RecoveryPopover`` from the previous iteration.
  //
  // Action kinds supported today:
  //   reclaim   → POST /tasks/:id/reclaim
  //   reassign  → POST /tasks/:id/reassign (with profile picker)
  //   unblock   → PATCH /tasks/:id  body: {status: "ready"}
  //   comment   → scroll to the comment input at the bottom of the drawer
  //   cli_hint  → copy payload.command to clipboard
  //   open_docs → open payload.url in a new tab
  // Unknown kinds are rendered as a disabled informational row so the
  // server can add new action kinds without breaking the UI.
  // -------------------------------------------------------------------------

  function DiagnosticActionButton(props) {
    const { t } = useI18n();
    const { action, onExec, busy, extra } = props;
    const label = (action.suggested ? "\u2606 " : "") + action.label;
    const cls = cn(
      "hermes-kanban-diag-action-btn",
      action.suggested ? "hermes-kanban-diag-action-btn--suggested" : "",
    );
    if (action.kind === "reclaim" || action.kind === "reassign" ||
        action.kind === "unblock") {
      return h("button", {
        className: cls,
        disabled: busy || (extra && extra.disabled),
        onClick: function () { onExec(action); },
        type: "button",
      }, label);
    }
    if (action.kind === "cli_hint") {
      return h("button", {
        className: cls,
        disabled: busy,
        onClick: function () { onExec(action); },
        type: "button",
        title: tx(t, "copyCommand", "Copy command to clipboard"),
      }, (extra && extra.copied) ? tx(t, "copied", "Copied") : label);
    }
    if (action.kind === "comment") {
      return h("button", {
        className: cls,
        onClick: function () { onExec(action); },
        type: "button",
      }, label);
    }
    if (action.kind === "open_docs") {
      return h("a", {
        className: cls,
        href: (action.payload && action.payload.url) || "#",
        target: "_blank",
        rel: "noreferrer",
      }, label);
    }
    // Unknown kind — render informational, non-interactive.
    return h("span", { className: cls + " hermes-kanban-diag-action-btn--unknown" },
      label);
  }

  function DiagnosticCard(props) {
    const { t } = useI18n();
    const { diag, task, boardSlug, assignees, onRefresh } = props;
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);
    const [copiedKey, setCopiedKey] = useState(null);
    const [reassignProfile, setReassignProfile] = useState(task.assignee || "");

    const execAction = function (action) {
      if (busy) return;
      if (action.kind === "cli_hint") {
        const cmd = (action.payload && action.payload.command) || action.label;
        const fallback = function () {
          // The clipboard API is unavailable in this context. The native
          // window.prompt is acceptable here because:
          //   (a) The success path doesn't open a dialog at all (just
          //       sets `copiedKey` for 2 seconds), and
          //   (b) the fallback only fires when the browser blocks
          //       navigator.clipboard, which is rare.
          // Documented carve-out — see issue #50547 followups for the
          // dedicated copyFallback dialog body that will replace this
          // once ConfirmDialog grows a `disabled` prop upstream.
          window.prompt("Copy this command:", cmd);
        };
        try {
          const p = navigator.clipboard && navigator.clipboard.writeText(cmd);
          if (p && p.then) {
            p.then(function () {
              setCopiedKey(action.label);
              setTimeout(function () { setCopiedKey(null); }, 2000);
            }).catch(fallback);
          } else {
            fallback();
          }
        } catch (_) {
          fallback();
        }
        return;
      }
      if (action.kind === "comment") {
        // Scroll the comment input into view; the drawer already has one
        // at the bottom. Focus it so the operator can start typing.
        const ta = document.querySelector(".hermes-kanban-drawer-comment-row input, .hermes-kanban-drawer-comment-row textarea");
        if (ta) {
          ta.scrollIntoView({ behavior: "smooth", block: "nearest" });
          ta.focus();
        }
        return;
      }
      if (action.kind === "unblock") {
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}`, boardSlug);
        SDK.fetchJSON(url, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "ready" }),
        }).then(function () {
          setMsg({ ok: true, text: tx(t, "unblockedMessage",
            "Unblocked {id}. Task is ready for the next tick.", { id: task.id }) });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "unblockFailed", "Unblock failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
      if (action.kind === "reclaim") {
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reclaim`, boardSlug);
        SDK.fetchJSON(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: `recovery action for ${diag.kind}` }),
        }).then(function () {
          setMsg({ ok: true, text: tx(t, "reclaimedMessage",
            "Reclaimed {id}. Task is back to ready.", { id: task.id }) });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "reclaimFailed", "Reclaim failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
      if (action.kind === "reassign") {
        if (!reassignProfile) {
          setMsg({ ok: false, text: tx(t, "pickProfileFirst", "Pick a profile first.") });
          return;
        }
        setBusy(true); setMsg(null);
        const url = withBoard(`${API}/tasks/${encodeURIComponent(task.id)}/reassign`, boardSlug);
        const body = {
          profile: reassignProfile || null,
          reclaim_first: !!(action.payload && action.payload.reclaim_first),
          reason: `recovery action for ${diag.kind}`,
        };
        SDK.fetchJSON(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then(function () {
          setMsg({
            ok: true,
            text: tx(t, "reassignedMessage", "Reassigned {id} to {profile}.",
              { id: task.id, profile: reassignProfile }),
          });
          if (onRefresh) onRefresh();
        }).catch(function (err) {
          setMsg({ ok: false, text: tx(t, "reassignFailed", "Reassign failed: ") + (err.message || err) });
        }).then(function () { setBusy(false); });
        return;
      }
    };

    // Pull out the reassign action so we can render its picker inline.
    const reassignAction = (diag.actions || []).find(function (a) {
      return a.kind === "reassign";
    });

    const sevClass = "hermes-kanban-diag--" + (diag.severity || "warning");
    return h("div", { className: cn("hermes-kanban-diag", sevClass) },
      h("div", { className: "hermes-kanban-diag-header" },
        h("span", { className: "hermes-kanban-diag-sev" },
          diag.severity === "critical" ? "!!!" :
          diag.severity === "error" ? "!!" : "\u26a0"),
        h("span", { className: "hermes-kanban-diag-title" },
          diag.title),
      ),
      h("div", { className: "hermes-kanban-diag-detail" },
        diag.detail),
      diag.data && Object.keys(diag.data).length > 0
        ? h("div", { className: "hermes-kanban-diag-data" },
            Object.keys(diag.data).map(function (k) {
              const v = diag.data[k];
              if (Array.isArray(v) && v.length > 0 && typeof v[0] === "string" &&
                  v[0].indexOf("t_") === 0) {
                // Task-id list — render as chips.
                return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                  h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                  v.map(function (x) {
                    return h("code", {
                      key: x, className: "hermes-kanban-event-phantom-chip",
                    }, x);
                  }),
                );
              }
              return h("div", { key: k, className: "hermes-kanban-diag-data-row" },
                h("span", { className: "hermes-kanban-diag-data-key" }, k + ":"),
                h("span", { className: "hermes-kanban-diag-data-val" },
                  Array.isArray(v) ? v.join(", ") : String(v)),
              );
            }),
          )
        : null,
      // Inline reassign picker — only shown when the diagnostic offers
      // a reassign action. Profile list comes from the board payload.
      reassignAction
        ? h("div", { className: "hermes-kanban-diag-reassign-row" },
            h("span", { className: "hermes-kanban-diag-reassign-label" },
              tx(t, "reassignTo", "Reassign to:")),
            h("select", {
              className: "hermes-kanban-recovery-select",
              value: reassignProfile,
              onChange: function (e) { setReassignProfile(e.target.value); },
            },
              h("option", { value: "" }, "(unassigned)"),
              (assignees || []).map(function (a) {
                return h("option", { key: a, value: a }, a);
              }),
            ),
          )
        : null,
      h("div", { className: "hermes-kanban-diag-actions" },
        (diag.actions || []).map(function (a, i) {
          return h(DiagnosticActionButton, {
            key: a.kind + i,
            action: a,
            onExec: execAction,
            busy: busy,
            extra: {
              copied: copiedKey === a.label,
              disabled: (a.kind === "reassign" && !reassignProfile),
            },
          });
        }),
      ),
      msg
        ? h("div", {
            className: cn(
              "hermes-kanban-diag-msg",
              msg.ok ? "hermes-kanban-diag-msg--ok" : "hermes-kanban-diag-msg--err",
            ),
          }, msg.text)
        : null,
    );
  }

  function DiagnosticsSection(props) {
    const { t } = useI18n();
    const diags = props.diagnostics || [];
    const hasOpenDiags = diags.length > 0;
    const [open, setOpen] = useState(hasOpenDiags);
    useEffect(function () {
      if (hasOpenDiags) setOpen(true);
    }, [hasOpenDiags]);
    if (!hasOpenDiags && !props.alwaysVisible && !props.diagnosticsUnknown) {
      // Nothing active. Collapse the section entirely rather than showing
      // an empty "Recovery" header — keeps clean tasks visually clean.
      return null;
    }
    if (!hasOpenDiags && props.diagnosticsUnknown) {
      // K10: the legacy detail read was told NOT to materialise diagnostics
      // (include_history=false on an EVO-aligned board). Surface that honestly
      // instead of collapsing to an implicit "no diagnostics".
      return h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          tx(t, "diagnostics", "Diagnostics")),
        h("div", { className: "text-xs text-muted-foreground" },
          "Not loaded: diagnostics are unavailable on an evidence read."));
    }
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          hasOpenDiags
            ? h("span", { className: "hermes-kanban-section-head-warning" },
                `\u26a0 ${tx(t, "diagnostics", "Diagnostics")} (${diags.length})`)
            : tx(t, "diagnostics", "Diagnostics"),
        ),
        h("button", {
          className: "hermes-kanban-section-toggle",
          onClick: function () { setOpen(function (x) { return !x; }); },
          type: "button",
        }, open ? tx(t, "hide", "Hide") : tx(t, "show", "Show")),
      ),
      open
        ? h("div", { className: "hermes-kanban-diag-list" },
            diags.map(function (d, i) {
              return h(DiagnosticCard, {
                key: props.task.id + ":" + d.kind + i,
                diag: d,
                task: props.task,
                boardSlug: props.boardSlug,
                assignees: props.assignees,
                onRefresh: props.onRefresh,
              });
            }),
          )
        : null,
    );
  }

    // -------------------------------------------------------------------------
  // Board switcher (multi-project)
  // -------------------------------------------------------------------------

  // Small `?` affordance next to the board controls. Opens the kanban docs
  // page in a new tab so users can look up what any of the widgets mean
  // without losing the current board view.
  function DocsLink() {
    return h("a", {
      href: DOCS_URL,
      target: "_blank",
      rel: "noopener noreferrer",
      className: "hermes-kanban-docs-link",
      title: "Open Hermes Kanban docs in a new tab",
      "aria-label": "Hermes Kanban documentation",
    }, "?");
  }

  // ---------------------------------------------------------------------
  // OrchestrationPanel — collapsible settings panel for the kanban
  // orchestrator (orchestrator profile picker, default assignee picker,
  // auto-decompose toggle, plus per-profile description editing with
  // auto-generate). Backed by /orchestration + /profiles endpoints.
  // ---------------------------------------------------------------------

  function OrchestrationPanel() {
    const [expanded, setExpanded] = useState(false);
    const [settings, setSettings] = useState(null);
    const [profiles, setProfiles] = useState([]);
    const [busy, setBusy] = useState({});
    const [msg, setMsg] = useState(null);

    const loadAll = useCallback(function () {
      Promise.all([
        SDK.fetchJSON(`${API}/orchestration`),
        SDK.fetchJSON(`${API}/profiles`),
      ]).then(function (results) {
        setSettings(results[0] || null);
        setProfiles((results[1] && results[1].profiles) || []);
        setMsg(null);
      }).catch(function (err) {
        setMsg({ ok: false, text: "Failed to load: " + (err.message || String(err)) });
      });
    }, []);

    useEffect(function () {
      // Load on mount so the collapsed pill shows the real mode without
      // requiring the user to expand the panel first.
      if (settings === null) loadAll();
    }, [settings, loadAll]);

    const saveSettings = function (patch) {
      setMsg(null);
      return SDK.fetchJSON(`${API}/orchestration`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }).then(function (res) {
        setSettings(res);
        setMsg({ ok: true, text: "Settings saved." });
        return res;
      }).catch(function (err) {
        setMsg({ ok: false, text: "Save failed: " + (err.message || String(err)) });
      });
    };

    const saveProfileDescription = function (name, description) {
      setBusy(function (b) { return Object.assign({}, b, { [name]: "save" }); });
      return SDK.fetchJSON(`${API}/profiles/${encodeURIComponent(name)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: description }),
      }).then(function () {
        loadAll();
        setMsg({ ok: true, text: `Description saved for ${name}.` });
      }).catch(function (err) {
        setMsg({ ok: false, text: "Save failed: " + (err.message || String(err)) });
      }).then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b); delete next[name]; return next;
        });
      });
    };

    const autoGenerateDescription = function (name, overwrite) {
      setBusy(function (b) { return Object.assign({}, b, { [name]: "auto" }); });
      return SDK.fetchJSON(`${API}/profiles/${encodeURIComponent(name)}/describe-auto`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overwrite: !!overwrite }),
      }).then(function (res) {
        if (res && res.ok) {
          loadAll();
          setMsg({ ok: true, text: `Auto-generated description for ${name}.` });
        } else {
          setMsg({
            ok: false,
            text: "Auto-generate failed: " + ((res && res.reason) || "unknown error"),
          });
        }
      }).catch(function (err) {
        setMsg({ ok: false, text: "Auto-generate failed: " + (err.message || String(err)) });
      }).then(function () {
        setBusy(function (b) {
          const next = Object.assign({}, b); delete next[name]; return next;
        });
      });
    };

    const headerLabel = expanded
      ? "▾ Orchestration settings"
      : "▸ Orchestration settings";

    // Mode pill — always visible (collapsed or expanded). One click flips
    // between Auto and Manual. Auto = dispatcher decomposes new triage tasks
    // every tick. Manual = pre-PR behavior, the user clicks ⚗ Decompose on
    // each triage card (or runs `hermes kanban decompose <id>`) and tasks
    // stay in triage until then.
    const autoOn = !!(settings && settings.auto_decompose);
    const modePillTitle = settings === null
      ? "Loading mode…"
      : (autoOn
          ? "Orchestration: Auto — the dispatcher decomposes new triage tasks automatically every tick. Click to switch to Manual (pre-PR behavior)."
          : "Orchestration: Manual — triage tasks stay in triage until you click ⚗ Decompose on each card. Click to switch to Auto.");
    const modePill = h("button", {
      type: "button",
      onClick: function () {
        if (settings === null) return;  // not loaded yet
        saveSettings({ auto_decompose: !autoOn });
      },
      disabled: settings === null,
      title: modePillTitle,
      className: "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 "
                 + "text-xs font-medium "
                 + (autoOn
                    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                    : "border-muted-foreground/30 bg-muted/30 text-muted-foreground"),
    },
      "Orchestration: ",
      h("span", { className: "ml-1 font-semibold" },
        settings === null ? "…" : (autoOn ? "Auto" : "Manual"))
    );

    if (!expanded) {
      return h("div", { className: "flex items-center gap-3 text-xs" },
        modePill,
        h("button", {
          type: "button",
          onClick: function () { setExpanded(true); },
          className: "underline text-muted-foreground hover:text-foreground",
          title: "Configure the kanban orchestrator (profile picker, default assignee, auto-decompose, profile descriptions)",
        }, headerLabel),
      );
    }

    const profileOptions = profiles.map(function (p) {
      const tag = p.is_default ? " (default)" : "";
      return h(SelectOption, { key: p.name, value: p.name }, p.name + tag);
    });

    return h(Card, { className: "p-3" },
      h(CardContent, { className: "p-2 flex flex-col gap-3" },
        h("div", { className: "flex items-center justify-between" },
          h("button", {
            type: "button",
            onClick: function () { setExpanded(false); },
            className: "text-sm font-medium underline-offset-2 hover:underline",
          }, headerLabel),
          modePill,
          h(Button, { onClick: loadAll, size: "sm" }, "Reload"),
        ),
        msg ? h("div", {
          className: msg.ok ? "hermes-kanban-msg-ok" : "hermes-kanban-msg-err",
        }, msg.text) : null,

        settings ? h("div", { className: "grid gap-3 sm:grid-cols-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              "Orchestrator profile"),
            h(Select, Object.assign({
              value: settings.orchestrator_profile || "",
              className: "h-8",
            }, selectChangeHandler(function (v) {
              saveSettings({ orchestrator_profile: v });
            })),
              h(SelectOption, { value: "" },
                "(default: " + (settings.active_profile || "default") + ")"),
              profileOptions,
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              "Resolved: " + (settings.resolved_orchestrator_profile || "default")),
            h("div", { className: "text-[10px] text-muted-foreground" },
              "Owns the root task after fan-out (wakes back up to judge completion). Does not drive how tasks split — configure the decomposer model under auxiliary.kanban_decomposer."),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              "Default assignee"),
            h(Select, Object.assign({
              value: settings.default_assignee || "",
              className: "h-8",
            }, selectChangeHandler(function (v) {
              saveSettings({ default_assignee: v });
            })),
              h(SelectOption, { value: "" },
                "(default: " + (settings.active_profile || "default") + ")"),
              profileOptions,
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              "Resolved: " + (settings.resolved_default_assignee || "default")),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs text-muted-foreground" },
              "Orchestration mode"),
            h("label", { className: "flex items-center gap-2 text-xs h-8" },
              h(Checkbox, {
                checked: !!settings.auto_decompose,
                onCheckedChange: function (checked) {
                  saveSettings({ auto_decompose: checked === true });
                },
              }),
              "Auto-decompose triage tasks",
            ),
            h("div", { className: "text-[10px] text-muted-foreground" },
              settings.auto_decompose
                ? "The dispatcher decomposes new triage tasks automatically."
                : "Triage tasks stay in triage until you click ⚗ Decompose."),
          ),
        ) : h("div", { className: "text-xs text-muted-foreground" },
          "Loading…"),

        h("div", { className: "border-t pt-3" },
          h(Label, { className: "text-xs text-muted-foreground" },
            "Profile descriptions"),
          h("div", { className: "text-[10px] text-muted-foreground pb-2" },
            "Descriptions guide the decomposer's routing. Click ⚗ to auto-generate, or edit and save."),
          profiles.length === 0
            ? h("div", { className: "text-xs text-muted-foreground" }, "No profiles installed.")
            : h("div", { className: "flex flex-col gap-2" },
                profiles.map(function (p) {
                  return h(ProfileDescriptionRow, {
                    key: p.name,
                    profile: p,
                    busy: busy[p.name] || null,
                    onSave: saveProfileDescription,
                    onAuto: autoGenerateDescription,
                  });
                }),
              ),
        ),
      ),
    );
  }

  function ProfileDescriptionRow(props) {
    const p = props.profile;
    const [draft, setDraft] = useState(p.description || "");
    const busy = props.busy;
    // Re-sync the local draft if the server-side description changes (e.g.
    // after auto-generate). Cheap because re-runs only happen on prop change.
    useEffect(function () {
      setDraft(p.description || "");
    }, [p.description]);

    const tag = p.description_auto && p.description ? " [auto, review]" : "";
    return h("div", { className: "flex flex-col gap-1 border-l-2 pl-2",
      style: { borderColor: p.description ? "#888" : "#cc6" } },
      h("div", { className: "flex items-center gap-2 text-xs" },
        h("span", { className: "font-medium" }, p.name),
        p.is_default ? h("span", { className: "text-[10px] text-muted-foreground" }, "(default)") : null,
        p.description_auto && p.description
          ? h("span", { className: "text-[10px] text-yellow-600" }, "auto — review")
          : null,
        !p.description
          ? h("span", { className: "text-[10px] text-yellow-600" }, "⚠ no description")
          : null,
      ),
      h("div", { className: "flex items-center gap-2" },
        h(Input, {
          value: draft,
          onChange: function (e) { setDraft(e.target.value); },
          placeholder: "What is this profile good at?",
          className: "h-7 text-xs flex-1",
        }),
        h(Button, {
          onClick: function () { props.onSave(p.name, draft); },
          size: "sm",
          disabled: !!busy || draft === (p.description || ""),
          title: "Save the description above as user-authored",
        }, busy === "save" ? "Saving…" : "Save"),
        h(Button, {
          onClick: function () { props.onAuto(p.name, true); },
          size: "sm",
          disabled: !!busy,
          title: "Auto-generate a description from this profile's skills and model",
        }, busy === "auto" ? "Generating…" : "⚗ Auto"),
      ),
    );
  }

  function BoardSwitcher(props) {
    const { t } = useI18n();
    const list = props.boardList || [];
    const current = list.find(function (b) { return b.slug === props.board; });
    const currentName = current && current.name ? current.name : props.board;
    const currentTotal = current ? current.total : 0;
    const hasMultipleBoards = list.length > 1;

    // Hide entirely when only the default board exists AND it's empty —
    // single-project users never see boards UI unless they ask for it.
    // We show the [+ New board] affordance as soon as any board has a
    // task (so the user can discover multi-project before they need it)
    // OR when any non-default board exists.
    const totalAcrossAllBoards = list.reduce(function (n, b) { return n + (b.total || 0); }, 0);
    const shouldShow = hasMultipleBoards || totalAcrossAllBoards > 0;
    if (!shouldShow) {
      return h("div", {
        className: "hermes-kanban-boardswitcher-compact",
        title: tx(t, "boardSwitcherHint", "Boards let you separate unrelated streams of work"),
      },
        h(Button, {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-7 text-xs",
        }, tx(t, "newBoard", "+ New board")),
        h(Button, {
          onClick: props.onSettingsClick,
          size: "sm",
          className: "h-7 text-xs",
          title: tx(t, "boardSettingsTitle",
            "Board settings — name, description, and the default project directory new tasks inherit"),
        }, tx(t, "boardSettings", "Settings")),
        h(DocsLink, null),
      );
    }

    return h("div", { className: "hermes-kanban-boardswitcher" },
      h("div", { className: "hermes-kanban-boardswitcher-inner" },
        h("div", { className: "flex flex-col gap-0.5" },
          h("div", { className: "text-[11px] tracking-wider text-muted-foreground" },
            tx(t, "board", "Board")),
          h("div", { className: "flex items-center gap-2" },
            h(Select, Object.assign({
              value: props.board,
              className: "h-8 min-w-[220px]",
              "aria-label": "Switch kanban board",
              title: "Boards are independent work streams. Each board has its own tasks, tenants, and assignees.",
            }, selectChangeHandler(function (v) { if (v) props.onSwitch(v); })),
              list.map(function (b) {
                const label = b.total > 0
                  ? `${b.name || b.slug} · ${b.total}`
                  : (b.name || b.slug);
                return h(SelectOption, { key: b.slug, value: b.slug }, label);
              }),
            ),
            h("span", { className: "text-xs text-muted-foreground" },
              `${currentTotal || 0} task${currentTotal === 1 ? "" : "s"}`),
          ),
        ),
        h("div", { className: "flex-1" }),
        h(DocsLink, null),
        h(Button, {
          onClick: props.onSettingsClick,
          size: "sm",
          className: "h-8",
          title: tx(t, "boardSettingsTitle",
            "Board settings — name, description, and the default project directory new tasks inherit"),
        }, tx(t, "boardSettings", "Settings")),
        h(Button, {
          onClick: props.onNewClick,
          size: "sm",
          className: "h-8",
          title: "Create a new board. Useful when you want an unrelated work stream (different project, different team, isolated scratch area).",
        }, tx(t, "newBoard", "+ New board")),
        props.board !== "default"
          ? h(Button, {
            onClick: function () {
              const msg = tx(t, "archiveBoardConfirm",
                "Archive board '{name}'? It will be moved to boards/_archived/ so you can recover it later. Tasks on this board will no longer appear anywhere in the UI.",
                { name: currentName });
              // Prefer the in-app dialog flow if the host wired one.
              if (props.requestDialog) {
                props.requestDialog({
                  kind: "confirm",
                  title: tx(t, "archiveBoardTitle", "Archive this board"),
                  description: msg,
                  confirmLabel: tx(t, "archive", "Archive"),
                  destructive: true,
                }).then(function (r) {
                  if (r.confirmed) props.onDeleteBoard(props.board);
                }).catch(function () { /* cancelled */ });
              } else if (window.confirm(msg)) {
                props.onDeleteBoard(props.board);
              }
            },
            size: "sm",
            className: "h-8",
            title: tx(t, "archiveBoardTitle", "Archive this board"),
          }, tx(t, "archive", "Archive"))
          : null,
      ),
    );
  }

  function NewBoardDialog(props) {
    const { t } = useI18n();
    const [slug, setSlug] = useState("");
    const [name, setName] = useState("");
    const [description, setDescription] = useState("");
    const [icon, setIcon] = useState("");
    const [projectDirectory, setProjectDirectory] = useState("");
    const [switchTo, setSwitchTo] = useState(true);
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState(null);

    // Auto-derive a name from the slug if the user hasn't typed one.
    const autoName = useMemo(function () {
      if (!slug) return "";
      return slug.replace(/[-_]+/g, " ")
        .split(" ")
        .filter(Boolean)
        .map(function (w) { return w[0].toUpperCase() + w.slice(1); })
        .join(" ");
    }, [slug]);

    function onSubmit(ev) {
      if (ev) ev.preventDefault();
      if (!slug.trim()) { setErr("slug is required"); return; }
      setSubmitting(true);
      setErr(null);
      props.onCreate({
        slug: slug.trim(),
        name: name.trim() || autoName || undefined,
        description: description.trim() || undefined,
        icon: icon.trim() || undefined,
        default_workdir: projectDirectory.trim() || undefined,
        switch: switchTo,
      }).catch(function (e) {
        setErr(String(e && e.message ? e.message : e));
        setSubmitting(false);
      });
    }

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog",
        onSubmit: onSubmit,
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "newBoardTitle", "New board")),
        h("div", { className: "text-xs text-muted-foreground mb-2" },
          tx(t, "newBoardDescription",
            "Boards let you separate unrelated streams of work — one per project, repo, or domain. Workers on one board never see another board's tasks.")),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "slug", "Slug"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "slugHint", "— lowercase, hyphens, e.g. atm10-server"))),
            h(Input, {
              value: slug,
              onChange: function (e) { setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9\-_]/g, "-")); },
              placeholder: "atm10-server",
              autoFocus: true,
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "displayNameHint", "(optional)"))),
            h(Input, {
              value: name,
              onChange: function (e) { setName(e.target.value); },
              placeholder: autoName || tx(t, "displayName", "Display name"),
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "description", "Description"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "descriptionHint", "(optional)"))),
            h(Input, {
              value: description,
              onChange: function (e) { setDescription(e.target.value); },
              placeholder: "What goes on this board?",
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" },
              tx(t, "projectDirectory", "Project directory"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "projectDirectoryHint", "(recommended)"))),
            h(Input, {
              value: projectDirectory,
              onChange: function (e) { setProjectDirectory(e.target.value); },
              placeholder: tx(t, "projectDirectoryPlaceholder",
                "Absolute path to the project folder"),
              title: tx(t, "projectDirectoryHelp",
                "Git projects use preserved worktrees. Other folders use the directory directly. Leave blank only for temporary work."),
              className: "h-8",
              autoCapitalize: "none",
              autoCorrect: "off",
              spellCheck: false,
            }),
            h("div", { className: "text-xs text-muted-foreground" },
              tx(t, "projectDirectoryExplanation",
                "Sets the default location for task files so project output is preserved.")),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "icon", "Icon"), " ",
              h("span", { className: "text-muted-foreground" },
                tx(t, "iconHint", "(single character or emoji)"))),
            h(Input, {
              value: icon,
              onChange: function (e) { setIcon(e.target.value.slice(0, 4)); },
              placeholder: "📦",
              className: "h-8 w-24",
            }),
          ),
          h("label", { className: "flex items-center gap-2 text-xs" },
            h(Checkbox, {
              checked: switchTo,
              onCheckedChange: function (checked) { setSwitchTo(checked === true); },
            }),
            tx(t, "switchAfterCreate", "Switch to this board after creating it"),
          ),
        ),
        err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
            disabled: submitting,
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: submitting || !slug.trim(),
          }, submitting ? tx(t, "creating", "Creating…") : tx(t, "createBoard", "Create board")),
        ),
      ),
    );
  }

  // Board settings dialog — edit display name, description, and the
  // board-level default project directory (default_workdir). The workdir
  // is the board-level setting every new task's workspace kind/path is
  // seeded from; task-level values in the create dialog override it.
  function BoardSettingsDialog(props) {
    const { t } = useI18n();
    const b = props.board || {};
    const [name, setName] = useState(b.name || "");
    const [description, setDescription] = useState(b.description || "");
    const [projectDirectory, setProjectDirectory] = useState(b.default_workdir || "");
    const [submitting, setSubmitting] = useState(false);
    const [err, setErr] = useState(null);

    function onSubmit(ev) {
      if (ev) ev.preventDefault();
      setSubmitting(true);
      setErr(null);
      // Send default_workdir unconditionally: "" clears it on the server,
      // a path sets it (validated server-side: absolute + existing dir).
      props.onSave({
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        default_workdir: projectDirectory.trim(),
      }).catch(function (e) {
        setErr(parseApiErrorMessage(e));
        setSubmitting(false);
      });
    }

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
      onKeyDown: function (e) { if (e.key === "Escape") props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog",
        onSubmit: onSubmit,
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "boardSettingsTitleFor", "Board settings — {name}",
            { name: b.name || b.slug || "default" })),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "displayName", "Display name")),
            h(Input, {
              value: name,
              onChange: function (e) { setName(e.target.value); },
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" }, tx(t, "description", "Description")),
            h(Input, {
              value: description,
              onChange: function (e) { setDescription(e.target.value); },
              className: "h-8",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            h(Label, { className: "text-xs" },
              tx(t, "projectDirectory", "Project directory")),
            h(Input, {
              value: projectDirectory,
              onChange: function (e) { setProjectDirectory(e.target.value); },
              placeholder: tx(t, "projectDirectoryPlaceholder",
                "Absolute path to the project folder"),
              title: tx(t, "projectDirectoryHelp",
                "Git projects use preserved worktrees. Other folders use the directory directly. Leave blank only for temporary work."),
              className: "h-8",
              autoCapitalize: "none",
              autoCorrect: "off",
              spellCheck: false,
            }),
            h("div", { className: "text-xs text-muted-foreground" },
              tx(t, "projectDirectoryOverrideHint",
                "New tasks inherit this as their workspace default; each task can still override it in the create dialog.")),
          ),
        ),
        err ? h("div", { className: "text-xs text-destructive mt-2" }, err) : null,
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
            disabled: submitting,
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: submitting,
          }, submitting ? tx(t, "saving", "Saving…") : tx(t, "save", "Save")),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Toolbar
  // -------------------------------------------------------------------------

  function BoardToolbar(props) {
    const { t } = useI18n();
    const tenants = (props.board && props.board.tenants) || [];
    const assignees = (props.board && props.board.assignees) || [];
    return h("div", { className: "flex flex-wrap items-end gap-3" },
      h("div", { className: "flex flex-col gap-1",
                 title: "Fuzzy-match tasks by id, title, or description. Matches across all columns." },
        h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "search", "Search")),
        h(Input, {
          placeholder: tx(t, "filterCards", "Filter cards…"),
          value: props.search,
          onChange: function (e) { props.setSearch(e.target.value); },
          className: "w-56 h-8",
        }),
      ),
      h("div", { className: "flex flex-col gap-1",
                 title: "Tenants are free-form tags on a task (e.g. customer, project, team). Set them via the task drawer or kanban_create." },
        h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "tenant", "Tenant")),
        h(Select, Object.assign({
          value: props.tenantFilter,
          className: "h-8",
        }, selectChangeHandler(props.setTenantFilter)),
          h(SelectOption, { value: "" }, tx(t, "allTenants", "All tenants")),
          tenants.map(function (tn) {
            return h(SelectOption, { key: tn, value: tn }, tn);
          }),
        ),
      ),
      h("div", { className: "flex flex-col gap-1",
                 title: "Filter by assigned Hermes profile. Profiles are the named agent identities that claim and work on tasks." },
        h(Label, { className: "text-xs text-muted-foreground" }, tx(t, "assignee", "Assignee")),
        h(Select, Object.assign({
          value: props.assigneeFilter,
          className: "h-8",
        }, selectChangeHandler(props.setAssigneeFilter)),
          h(SelectOption, { value: "" }, tx(t, "allProfiles", "All profiles")),
          assignees.map(function (a) {
            return h(SelectOption, { key: a, value: a }, a);
          }),
        ),
      ),
      h("label", { className: "flex items-center gap-2 text-xs",
                   title: "Include archived tasks in the board view. Archived tasks are hidden by default." },
        h(Checkbox, {
          checked: props.includeArchived,
          onCheckedChange: function (checked) { props.setIncludeArchived(checked === true); },
        }),
        tx(t, "showArchived", "Show archived"),
      ),
      h("label", { className: "flex items-center gap-2 text-xs",
                   title: "Group the Running column by assigned profile" },
        h(Checkbox, {
          checked: props.laneByProfile,
          onCheckedChange: function (checked) { props.setLaneByProfile(checked === true); },
        }),
        tx(t, "lanesByProfile", "Lanes by profile"),
      ),
      h("div", { className: "flex-1" }),
      h(Button, {
        onClick: props.onNudgeDispatch,
        size: "sm",
        title: "Wake the dispatcher to claim ready tasks now instead of waiting for the next tick. Use this after adding tasks if you want them picked up immediately.",
      }, tx(t, "nudgeDispatcher", "Nudge dispatcher")),
      h(Button, {
        onClick: props.onRefresh,
        size: "sm",
        title: "Reload the board from the database. The board auto-refreshes on task events; this is for forcing a re-read.",
      }, tx(t, "refresh", "Refresh")),
      h(Button, {
        onClick: function () {
          props.setSearch("");
          props.setTenantFilter("");
          props.setAssigneeFilter("");
          props.setIncludeArchived(false);
        },
        size: "sm",
        title: "Clear all active filters (search, tenant, assignee, archived).",
      }, tx(t, "clearFilters", "Clear filters")),
    );
  }

  // -------------------------------------------------------------------------
  // Bulk action bar (appears when >= 1 card is selected)
  // -------------------------------------------------------------------------

  function BulkActionBar(props) {
    const { t } = useI18n();
    const [assignee, setAssignee] = useState("");
    const [reclaimFirst, setReclaimFirst] = useState(false);
    const [priority, setPriority] = useState("");
    return h("div", { className: "hermes-kanban-bulk" },
      h("span", { className: "hermes-kanban-bulk-count" },
        `${props.count} ${tx(t, "selected", "selected")}`),
      h(Button, {
        onClick: function () { props.onApply({ status: "todo" }); },
        size: "sm",
        title: "Move selected tasks to Todo.",
      }, "→ todo"),
      h(Button, {
        onClick: function () { props.onApply({ status: "ready" }); },
        size: "sm",
        title: "Move selected tasks to Ready. Ready tasks are picked up by the dispatcher on the next tick.",
      }, "→ ready"),
      h(Button, {
        onClick: function () { props.onApply({ status: "blocked" },
          `Block ${props.count} task(s)?`); },
        size: "sm",
        title: "Block selected tasks. Releases any active claims.",
      }, "Block"),
      h(Button, {
        onClick: function () { props.onApply({ status: "ready" },
          `Unblock ${props.count} task(s)?`); },
        size: "sm",
        title: "Unblock selected tasks (promote to Ready).",
      }, "Unblock"),
      h(Button, {
        onClick: function () {
          props.onApply({ status: "done" },
            tx(t, "markDone", "Mark {n} task(s) as done?", { n: props.count }));
        },
        size: "sm",
        title: "Mark selected tasks as done. Releases any claims and unblocks dependent children. You'll be asked for a completion summary.",
      }, tx(t, "complete", "Complete")),
      h(Button, {
        onClick: function () {
          props.onApply({ archive: true },
            tx(t, "markArchived", "Archive {n} task(s)?", { n: props.count }));
        },
        size: "sm",
        title: "Archive selected tasks. They disappear from the default board view but remain in the database.",
      }, tx(t, "archive", "Archive")),
      h(Button, {
        onClick: function () {
          props.onDelete(props.count);
        },
        size: "sm",
        variant: "destructive",
        title: "Permanently delete selected tasks. This cannot be undone.",
      }, tx(t, "delete", "Delete")),
      h("div", { className: "hermes-kanban-bulk-priority",
                 title: "Set priority on selected tasks. Higher = claimed first." },
        h(Input, {
          type: "number",
          value: priority,
          onChange: function (e) { setPriority(e.target.value); },
          placeholder: tx(t, "priority", "pri"),
          className: "h-7 text-xs w-16",
        }),
        h(Button, {
          onClick: function () {
            if (priority === "") return;
            props.onApply({ priority: Number(priority) });
            setPriority("");
          },
          disabled: priority === "",
          size: "sm",
        }, tx(t, "setPriority", "Set priority")),
      ),
      h("div", { className: "hermes-kanban-bulk-reassign",
                 title: "Reassign selected tasks to a different Hermes profile. Pick a profile (or unassign) and click Apply." },
        h(Select, Object.assign({
          value: assignee,
          className: "h-7 text-xs",
        }, selectChangeHandler(setAssignee)),
          h(SelectOption, { value: "" }, "— reassign —"),
          h(SelectOption, { value: "__none__" }, "(unassign)"),
          props.assignees.map(function (a) {
            return h(SelectOption, { key: a, value: a }, a);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!assignee) return;
            props.onApply({ assignee: assignee === "__none__" ? "" : assignee, reclaim_first: reclaimFirst });
            setAssignee("");
          },
          disabled: !assignee,
          size: "sm",
          title: "Apply the selected assignee to all selected tasks.",
        }, tx(t, "apply", "Apply")),
      ),
      h("label", { className: "hermes-kanban-bulk-reclaim-first", title: "Reclaim any active claims before reassigning" },
        h(Checkbox, {
          checked: reclaimFirst,
          onCheckedChange: function (checked) { setReclaimFirst(checked === true); },
        }),
        "Reclaim first",
      ),
      h("div", { className: "flex-1" }),
      h(Button, {
        onClick: props.onSelectAllVisible,
        size: "sm",
        title: "Select all visible cards across columns.",
      }, "Select all visible"),
      h(Button, {
        onClick: props.onClear,
        size: "sm",
        title: "Deselect all tasks and hide this bar.",
      }, tx(t, "clear", "Clear")),
    );
  }

  // -------------------------------------------------------------------------
  // Trash Drop Zone
  // -------------------------------------------------------------------------

  function TrashDropZone(props) {
    const { t } = useI18n();
    const [dragOver, setDragOver] = useState(false);
    const zoneRef = useRef(null);

    useEffect(function () {
      if (!zoneRef.current) return undefined;
      const el = zoneRef.current;
      function onTouchDelete(e) {
        const taskId = e.detail && e.detail.taskId;
        if (taskId && props.onDelete) props.onDelete(taskId);
      }
      el.addEventListener("hermes-kanban:delete", onTouchDelete);
      return function () { el.removeEventListener("hermes-kanban:delete", onTouchDelete); };
    }, [props.onDelete]);

    const handleDragOver = function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragOver) setDragOver(true);
    };
    const handleDragLeave = function () { setDragOver(false); };
    const handleDrop = function (e) {
      e.preventDefault();
      setDragOver(false);
      const taskId = e.dataTransfer.getData(MIME_TASK);
      if (!taskId) return;
      if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
        // Delegate to the bulk-delete path on the parent so we use a
        // single in-app confirmation modal. Falling back to the per-id
        // onDelete path (which would prompt N times) is preserved for
        // hosts that haven't wired onDeleteSelected.
        if (props.onDeleteSelected) {
          props.onDeleteSelected(props.selectedIds.size);
        } else {
          Promise.all(
            Array.from(props.selectedIds).map(function (id) { return props.onDelete(id); })
          ).catch(function () {});
        }
      } else {
        props.onDelete(taskId);
      }
    };

    return h("div", {
      ref: zoneRef,
      "data-kanban-trash": "true",
      className: cn(
        "hermes-kanban-trash",
        dragOver ? "hermes-kanban-trash--drop" : "",
        props.draggingTaskId ? "hermes-kanban-trash--active" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
      h("span", { className: "hermes-kanban-trash-icon" }, "🗑️"),
      h("span", { className: "hermes-kanban-trash-label" },
        tx(t, "trash.dropHint", FALLBACK_TRASH.dropHint)),
    );
  }

  // -------------------------------------------------------------------------
  // Columns
  // -------------------------------------------------------------------------

  function BoardColumns(props) {
    const columnsRef = useRef(null);
    const panRef = useRef({ isPanning: false, startX: 0, scrollLeft: 0 });
    const [isPanning, setIsPanning] = useState(false);
    const [isScrollable, setIsScrollable] = useState(false);

    const checkScrollable = useCallback(function () {
      const el = columnsRef.current;
      setIsScrollable(!!el && el.scrollWidth > el.clientWidth + 1);
    }, []);

    useEffect(function () {
      checkScrollable();
      const el = columnsRef.current;
      if (!el) return undefined;
      if (typeof ResizeObserver !== "undefined") {
        const observer = new ResizeObserver(checkScrollable);
        observer.observe(el);
        return function () { observer.disconnect(); };
      }
      window.addEventListener("resize", checkScrollable);
      return function () { window.removeEventListener("resize", checkScrollable); };
    }, [checkScrollable, props.board]);

    const isPanBlockedTarget = useCallback(function (target) {
      if (!target) return true;
      if (target.closest && target.closest(".hermes-kanban-card")) return true;
      if (target.closest && target.closest(".hermes-kanban-column-add")) return true;
      if (target.closest && target.closest(".hermes-kanban-col-check")) return true;
      if (target.closest && target.closest("button,input,textarea,select,a,[role='button']")) return true;
      return false;
    }, []);

    const stopPan = useCallback(function () {
      const el = columnsRef.current;
      if (!panRef.current.isPanning) return;
      panRef.current.isPanning = false;
      setIsPanning(false);
      if (el) {
        // Keep cursor feedback instant even before React flushes the state update.
        el.classList.remove("hermes-kanban-columns--panning");
        el.style.userSelect = "";
      }
      if (panRef.current.cleanup) panRef.current.cleanup();
      panRef.current.cleanup = null;
    }, []);

    useEffect(function () {
      return function () { stopPan(); };
    }, [stopPan]);

    const handleMouseDown = useCallback(function (e) {
      if (e.button !== 0) return;
      if (isPanBlockedTarget(e.target)) return;
      const el = columnsRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      // Preserve the native horizontal scrollbar as a fallback; grab-pan starts above it.
      if (e.clientY >= rect.bottom - 20) return;
      if (el.scrollWidth <= el.clientWidth) return;

      panRef.current.isPanning = true;
      panRef.current.startX = e.clientX;
      panRef.current.scrollLeft = el.scrollLeft;
      setIsPanning(true);
      el.classList.add("hermes-kanban-columns--panning");
      el.style.userSelect = "none";

      function onMouseMove(ev) {
        if (!panRef.current.isPanning) return;
        const dx = ev.clientX - panRef.current.startX;
        el.scrollLeft = panRef.current.scrollLeft - dx;
        ev.preventDefault();
      }
      function onMouseUp() { stopPan(); }

      if (panRef.current.cleanup) panRef.current.cleanup();
      window.addEventListener("mousemove", onMouseMove);
      window.addEventListener("mouseup", onMouseUp, { once: true });
      window.addEventListener("blur", onMouseUp, { once: true });
      panRef.current.cleanup = function () {
        window.removeEventListener("mousemove", onMouseMove);
        window.removeEventListener("mouseup", onMouseUp);
        window.removeEventListener("blur", onMouseUp);
      };
      e.preventDefault();
    }, [isPanBlockedTarget, stopPan]);

    const handleDragStart = useCallback(function (e) {
      const card = e.target.closest && e.target.closest(".hermes-kanban-card");
      if (!card) return;
      const taskId = card.getAttribute("data-task-id");
      if (taskId && props.onDragStart) props.onDragStart(taskId);
    }, [props.onDragStart]);
    const handleDragEnd = useCallback(function () {
      if (props.onDragEnd) props.onDragEnd();
    }, [props.onDragEnd]);
    return h("div", {
      ref: columnsRef,
      className: cn(
        "hermes-kanban-columns",
        isScrollable ? "hermes-kanban-columns--scrollable" : "",
        isPanning ? "hermes-kanban-columns--panning" : "",
      ),
      onDragStart: handleDragStart,
      onDragEnd: handleDragEnd,
      onMouseDown: handleMouseDown,
    },
      props.board.columns.map(function (col) {
        return h(Column, {
          key: col.name,
          column: col,
          boardMeta: props.boardMeta,
          laneByProfile: props.laneByProfile,
          selectedIds: props.selectedIds,
          failedIds: props.failedIds,
          draggingTaskId: props.draggingTaskId,
          evidenceWorkerMap: props.evidenceWorkerMap,
          evidenceAligned: props.evidenceAligned,
          evidenceObservedAt: props.evidenceObservedAt,
          toggleSelected: props.toggleSelected,
          toggleRange: props.toggleRange,
          selectAllInColumn: props.selectAllInColumn,
          onMove: props.onMove,
          onMoveSelected: props.onMoveSelected,
          onOpen: props.onOpen,
          onCreate: props.onCreate,
          allTasks: props.allTasks,
        });
      }),
      h(TrashDropZone, {
        draggingTaskId: props.draggingTaskId,
        selectedIds: props.selectedIds,
        onDelete: props.onDelete,
        onDeleteSelected: props.onDeleteSelected,
      }),
    );
  }

  function Column(props) {
    const { t } = useI18n();
    const [dragOver, setDragOver] = useState(false);
    const [showCreate, setShowCreate] = useState(false);
    const colRef = useRef(null);

    // Listen for our synthetic touch-drop events from attachTouchDrag().
    useEffect(function () {
      if (!colRef.current) return undefined;
      const el = colRef.current;
      function onTouchDrop(e) {
        if (e.detail && e.detail.status === props.column.name) {
          const taskId = e.detail.taskId;
          if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1 && props.onMoveSelected) {
            props.onMoveSelected(props.column.name);
          } else {
            props.onMove(taskId, props.column.name);
          }
        }
      }
      el.addEventListener("hermes-kanban:drop", onTouchDrop);
      return function () { el.removeEventListener("hermes-kanban:drop", onTouchDrop); };
    }, [props.column.name, props.onMove, props.selectedIds, props.onMoveSelected]);

    const handleDragOver = function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (!dragOver) setDragOver(true);
    };
    const handleDragLeave = function () { setDragOver(false); };
    const handleDrop = function (e) {
      e.preventDefault();
      setDragOver(false);
      const taskId = e.dataTransfer.getData(MIME_TASK);
      if (!taskId) return;
      if (props.selectedIds && props.selectedIds.has(taskId) && props.selectedIds.size > 1) {
        if (props.onMoveSelected) props.onMoveSelected(props.column.name);
      } else {
        props.onMove(taskId, props.column.name);
      }
    };

    const lanes = useMemo(function () {
      if (!props.laneByProfile || props.column.name !== "running") return null;
      const byProfile = {};
      for (const tk of props.column.tasks) {
        const key = tk.assignee || "(unassigned)";
        (byProfile[key] = byProfile[key] || []).push(tk);
      }
      return Object.keys(byProfile).sort().map(function (k) {
        return { assignee: k, tasks: byProfile[k] };
      });
    }, [props.column, props.laneByProfile]);

    const colHelp = getColumnHelp(t, props.column.name);
    const colLabel = getColumnLabel(t, props.column.name);

    return h("div", {
      ref: colRef,
      "data-kanban-column": props.column.name,
      className: cn(
        "hermes-kanban-column",
        dragOver ? "hermes-kanban-column--drop" : "",
      ),
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
      h("div", { className: "hermes-kanban-column-header",
                 title: colHelp || "" },
        h(Checkbox, {
          className: "hermes-kanban-col-check",
          title: "Select all tasks in this column",
          "aria-label": `Select all tasks in ${colLabel || props.column.name}`,
          checked: props.column.tasks.length > 0 && props.column.tasks.every(function (t) { return props.selectedIds.has(t.id); }),
          onCheckedChange: function () {
            if (props.selectAllInColumn) props.selectAllInColumn(props.column.name);
          },
          onClick: function (e) { e.stopPropagation(); },
        }),
        h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[props.column.name]) }),
        h("span", { className: "hermes-kanban-column-label" },
          colLabel || props.column.name),
        h("span", { className: "hermes-kanban-column-count",
                    title: `${props.column.tasks.length} task${props.column.tasks.length === 1 ? "" : "s"} in this column` },
          props.column.tasks.length),
        h("button", {
          type: "button",
          className: "hermes-kanban-column-add",
          title: tx(t, "createTask", "Create task in this column"),
          onClick: function () { setShowCreate(function (v) { return !v; }); },
        }, showCreate ? "×" : "+"),
      ),
      h("div", { className: "hermes-kanban-column-sub" },
        colHelp || ""),
      showCreate ? h(InlineCreate, {
        columnName: props.column.name,
        allTasks: props.allTasks,
        defaultWorkspaceKind: (props.boardMeta && props.boardMeta.default_workspace_kind) || "scratch",
        defaultWorkspacePath: (props.boardMeta && props.boardMeta.default_workdir) || "",
        onSubmit: function (body) {
          props.onCreate(body).then(function () { setShowCreate(false); });
        },
        onCancel: function () { setShowCreate(false); },
      }) : null,
      h("div", { className: "hermes-kanban-column-body" },
        props.column.tasks.length === 0
          ? h("div", { className: "hermes-kanban-empty" }, tx(t, "noTasks", "— no tasks —"))
          : lanes
            ? lanes.map(function (lane) {
                return h("div", { key: lane.assignee, className: "hermes-kanban-lane" },
                  h("div", { className: "hermes-kanban-lane-head" },
                    h("span", { className: "hermes-kanban-lane-name" }, lane.assignee),
                    h("span", { className: "hermes-kanban-lane-count" }, lane.tasks.length),
                  ),
                  lane.tasks.map(function (tk) {
                    return h(TaskCard, {
                      key: tk.id, task: tk,
                      selected: props.selectedIds.has(tk.id),
                      failed: props.failedIds && props.failedIds.has(tk.id),
                      draggingTaskId: props.draggingTaskId,
                      draggingSource: props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
                      evidenceWorkerMap: props.evidenceWorkerMap,
                      evidenceAligned: props.evidenceAligned,
                      evidenceObservedAt: props.evidenceObservedAt,
                      toggleSelected: props.toggleSelected,
                      toggleRange: props.toggleRange,
                      onOpen: props.onOpen,
                    });
                  }),
                );
              })
            : props.column.tasks.map(function (tk) {
                return h(TaskCard, {
                  key: tk.id, task: tk,
                  selected: props.selectedIds.has(tk.id),
                  failed: props.failedIds && props.failedIds.has(tk.id),
                  draggingTaskId: props.draggingTaskId,
                  draggingSource: props.draggingTaskId && props.selectedIds.has(props.draggingTaskId) && props.selectedIds.size > 1 && props.selectedIds.has(tk.id),
                  evidenceWorkerMap: props.evidenceWorkerMap,
                  evidenceAligned: props.evidenceAligned,
                  evidenceObservedAt: props.evidenceObservedAt,
                  toggleSelected: props.toggleSelected,
                  toggleRange: props.toggleRange,
                  onOpen: props.onOpen,
                });
              }),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Card
  // -------------------------------------------------------------------------

  // Staleness tiers — amber after a grace window, red when clearly stuck.
  // Values below are seconds.
  const STALENESS = {
    ready:   { amber: 1 * 60 * 60,   red: 24 * 60 * 60 },
    running: { amber: 10 * 60,       red: 60 * 60 },
    blocked: { amber: 1 * 60 * 60,   red: 24 * 60 * 60 },
    todo:    { amber: 7 * 24 * 60 * 60, red: 30 * 24 * 60 * 60 },
  };

  function stalenessClass(task) {
    if (!task || !task.age) return "";
    const age = task.status === "running"
      ? task.age.started_age_seconds
      : task.age.created_age_seconds;
    const tier = STALENESS[task.status];
    if (!tier || age == null) return "";
    if (age >= tier.red)   return "hermes-kanban-card--stale-red";
    if (age >= tier.amber) return "hermes-kanban-card--stale-amber";
    return "";
  }

  function TaskCard(props) {
    const { t: i18n } = useI18n();
    const t = props.task;
    const cardRef = useRef(null);

    useEffect(function () {
      return attachTouchDrag(cardRef.current, t.id);
    }, [t.id]);

    const handleDragStart = function (e) {
      e.dataTransfer.setData(MIME_TASK, t.id);
      e.dataTransfer.effectAllowed = "move";
      const selectedCards = document.querySelectorAll(".hermes-kanban-card--selected");
      if (selectedCards.length > 1 && props.selected) {
        const ghost = document.createElement("div");
        ghost.className = "hermes-kanban-drag-ghost";
        ghost.textContent = selectedCards.length + " cards";
        document.body.appendChild(ghost);
        e.dataTransfer.setDragImage(ghost, 0, 0);
        requestAnimationFrame(function () {
          if (ghost.parentNode) document.body.removeChild(ghost);
        });
      }
    };
    const handleClick = function (e) {
      if (e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        if (props.toggleRange) props.toggleRange(t.id);
        return;
      }
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        e.stopPropagation();
        props.toggleSelected(t.id, true);
        return;
      }
      props.onOpen(t.id);
    };
    const handleKeyDown = function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        props.onOpen(t.id);
      }
      if (e.key === "Escape") {
        if (props.toggleSelected) props.toggleSelected(t.id, false);
      }
    };
    const handleCheckedChange = function () {
      props.toggleSelected(t.id, true);
    };

    const progress = t.progress;
    const needsAssignee = t.status === "ready" && !t.assignee;

    // Worker-evidence badge (K3). Shown only when the board is identity-aligned
    // with the EVO database. A card with a snapshot observation gets that
    // state; a running card with no observation is UNKNOWN (never stopped).
    let evidenceState = null;
    let evidenceTitle = null;
    if (props.evidenceAligned) {
      const wm = props.evidenceWorkerMap || {};
      if (wm[t.id]) {
        evidenceState = wm[t.id].state;
        evidenceTitle = "Worker evidence: " + (EVIDENCE_STATE_LABEL[evidenceState] || evidenceState);
      } else {
        evidenceState = "unknown";
        evidenceTitle = "Worker evidence: no live observation (unknown, not stopped)";
      }
      if (evidenceState && props.evidenceObservedAt != null) {
        evidenceTitle += " · evidence observed " + timeAgo(props.evidenceObservedAt);
      }
    }

    return h("div", {
      ref: cardRef,
      "data-task-id": t.id,
      className: cn(
        "hermes-kanban-card",
        props.selected ? "hermes-kanban-card--selected" : "",
        props.failed ? "hermes-kanban-card--failed" : "",
        props.draggingSource ? "hermes-kanban-card--dragging-source" : "",
        stalenessClass(t),
      ),
      draggable: true,
      tabIndex: 0,
      role: "button",
      "aria-label": `${t.title || "untitled"} — ${t.id} — ${t.status}`,
      onDragStart: handleDragStart,
      onClick: handleClick,
      onKeyDown: handleKeyDown,
    },
      h(Card, null,
        h(CardContent, { className: "hermes-kanban-card-content" },
          h("div", { className: "hermes-kanban-card-row" },
            h("label", {
              className: "hermes-kanban-card-check-wrap",
              title: tx(i18n, "selectForBulk", "Select for bulk actions"),
              onClick: function (e) { e.stopPropagation(); },
            },
              h(Checkbox, {
                className: "hermes-kanban-card-check",
                checked: props.selected,
                onCheckedChange: handleCheckedChange,
                onClick: function (e) { e.stopPropagation(); },
                "aria-label": `Select task ${t.id}`,
              }),
            ),
            h("span", { className: "hermes-kanban-card-id",
                        title: `Task id: ${t.id}. Use this id with kanban_show, /kanban show, or hermes kanban show.` }, t.id),
            t.warnings && t.warnings.count > 0
              ? h("span", {
                  className: cn(
                    "hermes-kanban-warning-badge",
                    "hermes-kanban-warning-badge--" + (t.warnings.highest_severity || "warning"),
                  ),
                  title: (
                    `${t.warnings.count} active diagnostic` +
                    (t.warnings.count === 1 ? "" : "s") +
                    ` (severity: ${t.warnings.highest_severity || "warning"}). ` +
                    `Click to open for details.`
                  ),
                }, t.warnings.highest_severity === "critical" ? "!!!" :
                   t.warnings.highest_severity === "error" ? "!!" : "⚠")
              : null,
            t.priority > 0
              ? h(Badge, { className: "hermes-kanban-priority",
                           title: `Priority ${t.priority}. Higher-priority tasks are claimed first by the dispatcher.` }, `P${t.priority}`)
              : null,
            t.tenant
              ? h(Badge, { variant: "outline", className: "hermes-kanban-tag",
                           title: `Tenant: ${t.tenant}. Free-form tag for grouping tasks (customer, project, team).` }, t.tenant)
              : null,
            progress
              ? h("span", {
                  className: cn(
                    "hermes-kanban-progress",
                    progress.done === progress.total ? "hermes-kanban-progress--full" : "",
                  ),
                  title: `${progress.done} of ${progress.total} child tasks done`,
                }, `${progress.done}/${progress.total}`)
              : null,
            needsAssignee
              ? h(Badge, {
                  variant: "outline",
                  className: "hermes-kanban-needs-assignee",
                  title: tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile."),
                }, tx(i18n, "needsAssignee", "Needs assignee"))
              : null,
          ),
          h("div", { className: "hermes-kanban-card-title" },
            t.title || tx(i18n, "untitled", "(untitled)")),
          h("div", { className: "hermes-kanban-card-row hermes-kanban-card-meta" },
            t.assignee
              ? h("span", { className: "hermes-kanban-assignee",
                            title: `Assigned to Hermes profile @${t.assignee}` }, "@", t.assignee)
              : h("span", { className: "hermes-kanban-unassigned",
                            title: needsAssignee
                              ? tx(i18n, "needsAssigneeHint", "Dependencies are satisfied, but the dispatcher skips this task until you assign a profile.")
                              : "No profile assigned." },
                  tx(i18n, "unassigned", "unassigned")),
            evidenceState
              ? h(EvidenceBadge, { state: evidenceState, title: evidenceTitle })
              : null,
            t.comment_count > 0
              ? h("span", { className: "hermes-kanban-count",
                            title: `${t.comment_count} comment${t.comment_count === 1 ? "" : "s"} on this task` }, "💬 ", t.comment_count)
              : null,
            t.link_counts && (t.link_counts.parents + t.link_counts.children) > 0
              ? h("span", { className: "hermes-kanban-count",
                            title: `${t.link_counts.parents} parent${t.link_counts.parents === 1 ? "" : "s"}, ${t.link_counts.children} child${t.link_counts.children === 1 ? "" : "ren"}. Children stay blocked until their parent is done.` },
                  "↔ ", t.link_counts.parents + t.link_counts.children)
              : null,
            h("span", { className: "hermes-kanban-ago",
                        title: t.created_at ? `Created ${t.created_at}` : "" },
              timeAgo ? timeAgo(t.created_at) : ""),
          ),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Create-task dialog (modal, with parent selector)
  //
  // Launched from a column's [+] button. Was an inline form squeezed into
  // the ~280px column (8 fields, unlabeled, no room to breathe); now a
  // centered modal reusing the hermes-kanban-dialog chrome so the form is
  // resizable-window friendly and every field has a visible label.
  // -------------------------------------------------------------------------

  function InlineCreate(props) {
    const { t } = useI18n();
    const [title, setTitle] = useState("");
    const [assignee, setAssignee] = useState("");
    const [priority, setPriority] = useState(0);
    const [parent, setParent] = useState("");
    const [skills, setSkills] = useState("");
    // A board with a configured workdir defaults to a persistent workspace:
    // worktree for git repositories, dir for ordinary directories. Boards
    // without one keep scratch for disposable research and ops tasks.
    const defaultWorkspaceKind = props.defaultWorkspaceKind || "scratch";
    const defaultWorkspacePath = props.defaultWorkspacePath || "";
    const [workspaceKind, setWorkspaceKind] = useState(defaultWorkspaceKind);
    const [workspacePath, setWorkspacePath] = useState(defaultWorkspacePath);
    // Goal-mode: when on, the dispatched worker runs the Ralph-style /goal
    // loop — a judge re-checks the card after each turn and the worker keeps
    // going in the same session until done, or the turn budget runs out
    // (which blocks the card for review). goalMaxTurns is optional; blank
    // = backend default.
    const [goalMode, setGoalMode] = useState(false);
    const [goalMaxTurns, setGoalMaxTurns] = useState("");

    const submit = function () {
      const trimmed = title.trim();
      if (!trimmed) return;
      const body = {
        title: trimmed,
        assignee: assignee.trim() || null,
        priority: Number(priority) || 0,
        triage: props.columnName === "triage",
      };
      if (parent) body.parents = [parent];
      // Parse comma-separated skills into a clean list. Blank = no
      // extras (omit key so backend leaves it null). The dispatcher
      // always auto-loads kanban-worker; these are extras on top.
      const skillList = skills
        .split(",")
        .map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length > 0; });
      if (skillList.length > 0) body.skills = skillList;
      // Only send workspace_kind when it's non-default. Keeps the request
      // shape small and interoperable with older dispatcher versions.
      if (workspaceKind && workspaceKind !== "scratch") {
        body.workspace_kind = workspaceKind;
      }
      const wpTrim = workspacePath.trim();
      if (wpTrim) body.workspace_path = wpTrim;
      // Goal-mode toggle. Only send the keys when enabled so the request
      // shape stays small and old dispatchers ignore it cleanly.
      if (goalMode) {
        body.goal_mode = true;
        const gmt = parseInt(goalMaxTurns, 10);
        if (Number.isFinite(gmt) && gmt > 0) body.goal_max_turns = gmt;
      }
      props.onSubmit(body);
      setTitle(""); setAssignee(""); setPriority(0); setParent(""); setSkills("");
      setWorkspaceKind(defaultWorkspaceKind); setWorkspacePath(defaultWorkspacePath);
      setGoalMode(false); setGoalMaxTurns("");
    };

    const showPathInput = workspaceKind !== "scratch";
    const pathPlaceholder = workspaceKind === "dir"
      ? tx(t, "workspacePathDir", "workspace path (required without a board workdir)")
      : tx(t, "workspacePathOptional",
          "repository path (optional when the board has a workdir)");

    const fieldLabel = function (text, hint) {
      return h(Label, { className: "text-xs" }, text,
        hint ? h("span", { className: "text-muted-foreground" }, " ", hint) : null);
    };

    return h("div", {
      className: "hermes-kanban-dialog-backdrop",
      onClick: function (e) { if (e.target === e.currentTarget) props.onCancel(); },
      onKeyDown: function (e) { if (e.key === "Escape") props.onCancel(); },
    },
      h("form", {
        className: "hermes-kanban-dialog hermes-kanban-create-dialog",
        onSubmit: function (e) { e.preventDefault(); submit(); },
      },
        h("div", { className: "hermes-kanban-dialog-title" },
          tx(t, "newTaskTitle", "New task — {column}",
            { column: getColumnLabel(t, props.columnName) || props.columnName })),
        h("div", { className: "flex flex-col gap-3" },
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "taskTitleLabel", "Title")),
            h("textarea", {
              value: title,
              onChange: function (e) { setTitle(e.target.value); },
              onKeyDown: function (e) {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
              },
              placeholder: props.columnName === "triage"
                ? tx(t, "triagePlaceholder", "Rough idea — AI will spec it…")
                : tx(t, "taskTitlePlaceholder", "New task title…"),
              autoFocus: true,
              className: "text-sm min-h-[3rem] max-h-48 resize-y w-full border border-input bg-transparent px-2 py-1 rounded-md focus:outline-none focus:ring-2 focus:ring-ring",
              rows: 3,
            }),
          ),
          h("div", { className: "flex gap-2" },
            h("div", { className: "flex flex-col gap-1 flex-1" },
              fieldLabel(props.columnName === "triage"
                ? tx(t, "specifier", "specifier")
                : tx(t, "assigneeLabel", "Assignee"),
                tx(t, "assigneeLabelHint", "(blank = dispatcher picks)")),
              h(Input, {
                value: assignee,
                onChange: function (e) { setAssignee(e.target.value); },
                placeholder: props.columnName === "triage"
                  ? tx(t, "specifier", "specifier")
                  : tx(t, "assigneePlaceholder", "assignee"),
                className: "h-8 text-sm",
                title: props.columnName === "triage"
                  ? "Hermes profile that will spec this task (default: the dispatcher's configured specifier). Leave blank to let the dispatcher pick."
                  : "Hermes profile to assign. Leave blank and the dispatcher will pick from available profiles when the task is Ready.",
                style: { textTransform: "none" },
                autoCapitalize: "none",
                autoCorrect: "off",
                spellCheck: false,
              }),
            ),
            h("div", { className: "flex flex-col gap-1 w-20" },
              fieldLabel(tx(t, "priority", "Priority")),
              h(Input, {
                type: "number",
                value: priority,
                onChange: function (e) { setPriority(e.target.value); },
                placeholder: "pri",
                className: "h-8 text-sm",
                title: "Priority. Higher-priority tasks are claimed first by the dispatcher. 0 = default.",
              }),
            ),
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "skillsLabel", "Skills"),
              tx(t, "skillsLabelHint", "(optional, comma-separated)")),
            h(Input, {
              value: skills,
              onChange: function (e) { setSkills(e.target.value); },
              placeholder: tx(t, "skillsPlaceholder",
                "skills (optional, comma-separated): translation, github-code-review"),
              title: "Force-load these skills into the worker (in addition to the built-in kanban-worker).",
              className: "h-8 text-sm",
            }),
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "workspace", "Workspace")),
            h("div", { className: "flex gap-2" },
              h(Select, Object.assign({
                value: workspaceKind,
                title: "Choose whether task files are temporary or preserved after completion.",
                className: "h-8 text-sm flex-1",
              }, selectChangeHandler(setWorkspaceKind)),
                h(SelectOption, { value: "scratch" },
                  tx(t, "workspaceScratch", "Temporary — deleted on completion")),
                h(SelectOption, { value: "worktree" },
                  tx(t, "workspaceWorktree", "Git worktree — preserved")),
                h(SelectOption, { value: "dir" },
                  tx(t, "workspaceDir", "Directory — preserved")),
              ),
              showPathInput ? h(Input, {
                value: workspacePath,
                onChange: function (e) { setWorkspacePath(e.target.value); },
                placeholder: pathPlaceholder,
                className: "h-8 text-sm flex-1",
              }) : null,
            ),
            workspaceKind === "scratch" ? h("div", {
              className: "text-xs text-destructive",
              role: "alert",
            }, tx(t, "workspaceScratchWarning",
              "This workspace and any files left in it are deleted when the task completes.")) : null,
          ),
          h("div", { className: "flex flex-col gap-1" },
            fieldLabel(tx(t, "parentLabel", "Parent task"),
              tx(t, "parentLabelHint", "(child stays blocked until the parent is done)")),
            h(Select, Object.assign({
              value: parent,
              className: "h-8 text-sm",
              title: "Optional parent task. A child stays blocked in its current column until the parent is marked done.",
            }, selectChangeHandler(setParent)),
              h(SelectOption, { value: "" }, tx(t, "noParent", "— no parent —")),
              (props.allTasks || []).map(function (task) {
                return h(SelectOption, { key: task.id, value: task.id },
                  `${task.id} — ${(task.title || "").slice(0, 50)}`);
              }),
            ),
          ),
          h("div", { className: "flex gap-2 items-center" },
            h("label", {
              className: "flex items-center gap-1.5 text-xs cursor-pointer select-none",
              title: "Goal mode: the worker keeps going in the same session until a judge agrees the card is done (or the turn budget runs out, which blocks it for review). Best for open-ended cards one shot rarely finishes.",
            },
              h("input", {
                type: "checkbox",
                checked: goalMode,
                onChange: function (e) { setGoalMode(!!e.target.checked); },
                className: "h-3.5 w-3.5 accent-current",
              }),
              tx(t, "goalMode", "goal mode"),
            ),
            goalMode ? h(Input, {
              type: "number",
              value: goalMaxTurns,
              onChange: function (e) { setGoalMaxTurns(e.target.value); },
              placeholder: tx(t, "goalMaxTurns", "max turns (default 20)"),
              className: "h-8 text-sm w-44",
              title: "Turn budget for the goal loop. Blank = backend default (20).",
              min: 1,
            }) : null,
          ),
        ),
        h("div", { className: "hermes-kanban-dialog-actions" },
          h(Button, {
            type: "button",
            onClick: props.onCancel,
            size: "sm",
          }, tx(t, "cancel", "Cancel")),
          h(Button, {
            type: "submit",
            size: "sm",
            disabled: !title.trim(),
          }, tx(t, "create", "Create")),
        ),
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Next-ten drawer section: R4 acceptance compare, R6 reviewer packet
  // download, R8 attachment provenance. All read through the same /api
  // boundary; every state but PASS renders its reason visibly.
  // -------------------------------------------------------------------------
  function DrawerNextTenSection(props) {
    const board = props.boardSlug;
    const card = props.cardId;
    // R4: pick the two most recent DISTINCT run ids from the bounded runs
    // page for the compare affordance; fewer than two runs disables it
    // honestly (an absent historical receipt is UNKNOWN, never invented).
    const runs = (props.runs && Array.isArray(props.runs.items)) ? props.runs.items : [];
    const runIds = runs.map(function (r) { return r && typeof r.id === "number" ? r.id : null; })
      .filter(function (id) { return id != null; });
    const currentRunId = runIds.length > 0 ? runIds[runIds.length - 1] : null;
    const previousRunId = runIds.length > 1 ? runIds[runIds.length - 2] : null;

    const [compare, setCompare] = useState(null);
    const [comparing, setComparing] = useState(false);
    const [packet, setPacket] = useState(null);
    const [downloading, setDownloading] = useState(false);
    const [provenance, setProvenance] = useState(null);

    function doCompare() {
      if (currentRunId == null || previousRunId == null) return;
      setComparing(true);
      setCompare(null);
      fetchAcceptanceCompare(board, card, currentRunId, previousRunId).then(function (env) {
        setComparing(false);
        setCompare(env);
      });
    }

    function doDownload() {
      setDownloading(true);
      setPacket(null);
      downloadReviewerPacket(board, card).then(function (res) {
        setDownloading(false);
        setPacket(res);
      });
    }

    // R8: provenance for the first loaded attachment (bounded; the drawer's
    // attachment list itself stays the full inventory).
    const attachmentId = (props.attachments && props.attachments.length > 0 && props.attachments[0].id != null)
      ? props.attachments[0].id : null;
    useEffect(function () {
      if (attachmentId == null) return undefined;
      let alive = true;
      fetchAttachmentProvenance(board, card, attachmentId).then(function (env) {
        if (alive) setProvenance(env);
      });
      return function () { alive = false; };
    }, [board, card, attachmentId]);

    const cmpEv = (compare && compare.state === "PASS" && compare.evidence) ? compare.evidence : null;
    // R4: the adapter labels each check's provenance with a `source` field
    // ("parent" = parent attestation, "machine" = machine validation);
    // anything else is an honest UNKNOWN origin, never guessed.
    function checkOriginLabel(c) {
      if (c && c.source === "parent") return "parent attestation";
      if (c && c.source === "machine") return "machine validation";
      return "origin unknown";
    }
    const cmpRows = cmpEv && Array.isArray(cmpEv.checks)
      ? cmpEv.checks.map(function (c, i) {
          return h("div", {
            key: (c.name || "check") + ":" + i,
            className: "hermes-kanban-workflow-check",
            "data-acceptance-check": c.name || "",
          },
            h("span", { className: "hermes-kanban-workflow-check-name" }, c.name || ""),
            h("span", { className: "hermes-kanban-workflow-check-state", style: { color: workflowStateTone(c.change === "reverified" ? "PASS" : c.change === "regressed" ? "FAIL" : "UNKNOWN") } }, c.change || "unproved"),
            h("span", { className: "hermes-kanban-workflow-check-reason" }, checkOriginLabel(c)));
        })
      : [];

    return h("div", { className: "hermes-kanban-section", "data-next-ten-drawer": "true" },
      h("div", { className: "hermes-kanban-section-head" }, "Release evidence"),
      // R4 acceptance compare.
      h("div", { "data-acceptance-compare": "true" },
        h("div", { className: "text-xs text-muted-foreground" },
          currentRunId != null && previousRunId != null
            ? "Compare acceptance between run " + previousRunId + " and run " + currentRunId + "."
            : "Acceptance compare needs two recorded runs; fewer is honest UNKNOWN, never invented."),
        currentRunId != null && previousRunId != null
          ? h("button", {
              type: "button",
              className: "hermes-kanban-workflow-btn",
              "data-acceptance-compare-btn": "true",
              disabled: comparing,
              onClick: doCompare,
            }, comparing ? "Comparing\u2026" : "Compare acceptance")
          : null,
        compare && compare.state !== "PASS"
          ? h("div", { className: "text-xs", style: { color: workflowStateTone(compare.state) } },
              compare.reason || ("compare " + compare.state))
          : null,
        cmpEv
          ? h("div", { className: "hermes-kanban-workflow-list", "data-acceptance-compare-result": "true" },
              h("div", { className: "text-xs" },
                "run " + (cmpEv.previous && cmpEv.previous.run_id) + " \u2192 run " + (cmpEv.current && cmpEv.current.run_id) +
                " \u00b7 previous " + ((cmpEv.previous && cmpEv.previous.state) || "UNKNOWN")),
              cmpRows)
          : null,
        cmpEv && Array.isArray(cmpEv.limitations) && cmpEv.limitations.length > 0
          ? h("div", { className: "text-xs text-muted-foreground" },
              "limitations: " + cmpEv.limitations.join("; "))
          : null),
      // R6 reviewer packet download.
      h("div", { "data-reviewer-packet": "true", style: { marginTop: "6px" } },
        h("button", {
          type: "button",
          className: "hermes-kanban-workflow-btn",
          "data-reviewer-packet-btn": "true",
          disabled: downloading,
          onClick: doDownload,
        }, downloading ? "Preparing\u2026" : "Download reviewer packet"),
        packet
          ? h("div", {
              className: "text-xs",
              "data-reviewer-packet-result": "true",
              style: { color: packet.ok ? EVIDENCE_STATE_TONE.running : workflowStateTone(packet.envelope && packet.envelope.state) },
            }, packet.ok
              ? "Downloaded bounded reviewer packet JSON."
              : ((packet.envelope && packet.envelope.reason) || "packet unavailable"))
          : null),
      // R8 attachment provenance.
      h("div", { "data-attachment-provenance": "true", style: { marginTop: "6px" } },
        attachmentId != null
          ? h("div", { className: "text-xs" },
              "Attachment " + attachmentId + ": ",
              provenance && provenance.state === "PASS" && provenance.evidence
                ? h("span", { "data-attachment-provenance-line": "true" },
                    provenance.evidence.acceptance_state === "PASS"
                      ? "accepted by run " + provenance.evidence.accepted_run_id
                      : provenance.evidence.acceptance_state === "FAIL"
                        ? "explicitly not accepted"
                        : "accepted run unknown")
                : h("span", { style: { color: EVIDENCE_STATE_TONE.unknown } },
                    (provenance && provenance.reason) || "acceptance unknown"))
          : h("div", { className: "text-xs text-muted-foreground" }, "No attachments on this card.")));
  }

  // -------------------------------------------------------------------------
  // Task drawer
  // -------------------------------------------------------------------------

  function TaskDrawer(props) {
    const { t } = useI18n();
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [err, setErr] = useState(null);
    // Surface PATCH failures (e.g. 409 "parent not done") right next to
    // the drawer's action row — without it, the drawer's only error
    // surface (``err``) is hidden behind the loaded ``data`` and the
    // Ready/Block/Complete buttons feel like no-ops.  See #26744.
    const [patchErr, setPatchErr] = useState(null);
    const [newComment, setNewComment] = useState("");
    const [uploadBusy, setUploadBusy] = useState(false);
    const [uploadErr, setUploadErr] = useState(null);
    const [editing, setEditing] = useState(false);
    // Home-channel notification toggles. homeChannels is the list of platforms
    // the user has a /sethome on; each entry has a `subscribed` bool telling
    // us whether this task is currently subscribed via that platform's home.
    const [homeChannels, setHomeChannels] = useState([]);
    const [homeBusy, setHomeBusy] = useState({});
    const boardSlug = props.boardSlug;

    const load = useCallback(function () {
      // K10: on an EVO-aligned board, ask the detail route NOT to materialise
      // runs/events/attachments (and derived diagnostics) — those are paged
      // through /evidence/page instead. include_history=false is a supported
      // read option on the detail route (parent-owned); non-aligned boards and
      // all other callers keep the default include_history=true.
      const detailPath = `${API}/tasks/${encodeURIComponent(props.taskId)}`;
      const detailUrl = props.evidenceAligned
        ? withBoard(`${detailPath}?include_history=false`, boardSlug)
        : withBoard(detailPath, boardSlug);
      return SDK.fetchJSON(detailUrl)
        .then(function (d) { setData(d); setErr(null); setPatchErr(null); })
        .catch(function (e) { setErr(String(e.message || e)); })
        .finally(function () { setLoading(false); });
    }, [props.taskId, boardSlug, props.evidenceAligned]);

    const loadHomeChannels = useCallback(function () {
      const qs = new URLSearchParams({ task_id: props.taskId });
      const url = withBoard(`${API}/home-channels?${qs}`, boardSlug);
      return SDK.fetchJSON(url)
        .then(function (d) { setHomeChannels(d.home_channels || []); })
        .catch(function () { /* silent — endpoint optional on older gateways */ });
    }, [props.taskId, boardSlug]);

    // Reload when the WS stream reports new events for this task id
    // (completion, block, crash, etc. — anything that'd make the drawer
    // show stale data if we only loaded on mount).
    useEffect(function () { load(); }, [load, props.eventTick]);
    useEffect(function () { loadHomeChannels(); }, [loadHomeChannels]);
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape" && !editing) props.onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.onClose, editing]);

    const handleComment = function () {
      const body = newComment.trim();
      if (!body) return;
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/comments`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body }),
      }).then(function () {
        setNewComment("");
        load();
        props.onRefresh();
      }).catch(function (e) { setErr(String(e.message || e)); });
    };

    // File upload uses raw fetch (not SDK.fetchJSON, which JSON-encodes)
    // so the browser sets the multipart boundary. Auth rides the session
    // cookie + bearer token, matching the rest of the dashboard.
    const handleUpload = function (fileList) {
      const files = Array.prototype.slice.call(fileList || []);
      if (!files.length) return;
      setUploadBusy(true);
      setUploadErr(null);
      const url = withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/attachments`, boardSlug);
      // Upload sequentially so a partial failure leaves a clear state.
      let chain = Promise.resolve();
      files.forEach(function (f) {
        chain = chain.then(function () {
          const fd = new FormData();
          fd.append("file", f, f.name);
          // SDK.authedFetch handles auth in BOTH modes (loopback token header /
          // gated cookie) and applies the dashboard base-path prefix. The old
          // hand-rolled Authorization:Bearer + credentials:'same-origin' sent
          // an empty token and 401'd in gated mode.
          return SDK.authedFetch(url, { method: "POST", body: fd })
            .then(function (resp) {
              if (!resp.ok) {
                return resp.text().then(function (txt) {
                  throw new Error(parseApiErrorMessage(new Error(resp.status + ": " + txt)));
                });
              }
            });
        });
      });
      chain.then(function () {
        load();
        props.onRefresh();
      }).catch(function (e) {
        setUploadErr(String(e.message || e));
      }).finally(function () {
        setUploadBusy(false);
      });
    };

    const handleDeleteAttachment = function (attachmentId) {
      return SDK.fetchJSON(withBoard(`${API}/attachments/${attachmentId}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setUploadErr(String(e.message || e)); });
    };

    // doPatch is invoked by the side-drawer's StatusActions (block / unblock
    // / complete / archive), PriorityEditor, AssigneeEditor, etc. Two
    // requirements differ from the column-card drag path:
    //
    // 1. Confirmation: this happens via the in-app dialog flow exposed
    //    on `props` by the parent (KanbanPage passes a `requestDialog`
    //    function down). Falls back to a native window.confirm if the
    //    parent didn't wire one up.
    //
    // 2. Completion summary for status=done: until ConfirmDialog grows a
    //    `disabled` prop upstream (see #50547 followups), we keep the
    //    prompt + alert as a documented carve-out for this single call
    //    site. The prompt body, validation copy, and requirement are
    //    unchanged from the pre-migration implementation.
    const doPatch = function (patch, opts) {
      if (opts && opts.confirm && props.requestDialog) {
        return props.requestDialog({
          kind: "confirm",
          title: opts.confirmTitle || tx(t, "confirmTitle", "Confirm change"),
          description: opts.confirm,
          confirmLabel: opts.confirmLabel || tx(t, "common.confirm", "Confirm"),
          destructive: !!opts.destructive,
        }).then(function (r) {
          if (!r.confirmed) return null;
          return applyPatch(patch);
        });
      }
      if (opts && opts.confirm && !window.confirm(opts.confirm)) {
        return Promise.resolve();
      }
      return applyPatch(patch);

      function applyPatch(patch) {
        const finalPatch = withCompletionSummary(patch);
        if (!finalPatch) return Promise.resolve();
        setPatchErr(null);
        return SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}`, boardSlug), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(finalPatch),
        }).then(function () { load(); props.onRefresh(); })
          .catch(function (e) { setPatchErr(parseApiErrorMessage(e)); });
      }
    };

    // Local completion-summary prompt used only by doPatch above.
    // Documented carve-out — see the doPatch comment.
    function withCompletionSummary(patch) {
      if (!patch || patch.status !== "done") return patch;
      const value = window.prompt(
        tx(t, "completionSummary",
          "Completion summary for this task. This is stored as the task result."),
        "",
      );
      if (value === null) return null;
      const summary = value.trim();
      if (!summary) {
        window.alert(tx(t, "completionSummaryRequired",
          "Completion summary is required before marking a task done."));
        return null;
      }
      return Object.assign({}, patch, { result: summary, summary: summary });
    }

    // Triage specifier — calls the auxiliary LLM to flesh out a rough
    // idea in the Triage column into a concrete spec (title + body with
    // goal, approach, acceptance criteria) and promotes it to todo.
    // Not a PATCH: runs through a dedicated POST endpoint because the
    // LLM call can take tens of seconds, and its outcome is richer than
    // a status flip (may update title AND body AND emit an audit
    // comment — or fail with a human-readable reason that the UI
    // surfaces inline without treating it as an HTTP error).
    const doSpecify = function () {
      return SDK.fetchJSON(
        withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/specify`, boardSlug),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      ).then(function (res) {
        load();
        props.onRefresh();
        return res;
      });
    };

    // POST /tasks/:id/decompose — fan a triage task out into a graph
    // of child tasks routed to specialist profiles by description.
    // Refreshes both the drawer (so the user sees the root flip to
    // todo) and the board (so the new children appear in the columns).
    const doDecompose = function () {
      return SDK.fetchJSON(
        withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/decompose`, boardSlug),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      ).then(function (res) {
        load();
        props.onRefresh();
        return res;
      });
    };

    const addLink = function (parentId) {
      return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_id: parentId, child_id: props.taskId }),
      }).then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const removeLink = function (parentId) {
      const qs = new URLSearchParams({ parent_id: parentId, child_id: props.taskId });
      return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const addChild = function (childId) {
      return SDK.fetchJSON(withBoard(`${API}/links`, boardSlug), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_id: props.taskId, child_id: childId }),
      }).then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };
    const removeChild = function (childId) {
      const qs = new URLSearchParams({ parent_id: props.taskId, child_id: childId });
      return SDK.fetchJSON(withBoard(`${API}/links?${qs}`, boardSlug), { method: "DELETE" })
        .then(function () { load(); props.onRefresh(); })
        .catch(function (e) { setErr(String(e.message || e)); });
    };

    const toggleHomeSubscription = function (platform, currentlySubscribed) {
      // Optimistic flip + busy flag to keep double-clicks idempotent.
      setHomeBusy(function (b) { return Object.assign({}, b, { [platform]: true }); });
      setHomeChannels(function (list) {
        return list.map(function (h) {
          return h.platform === platform
            ? Object.assign({}, h, { subscribed: !currentlySubscribed })
            : h;
        });
      });
      const method = currentlySubscribed ? "DELETE" : "POST";
      const url = withBoard(
        `${API}/tasks/${encodeURIComponent(props.taskId)}/home-subscribe/${encodeURIComponent(platform)}`,
        boardSlug,
      );
      return SDK.fetchJSON(url, { method: method })
        .then(function () { return loadHomeChannels(); })
        .catch(function (e) {
          // Revert optimistic flip on failure.
          setHomeChannels(function (list) {
            return list.map(function (h) {
              return h.platform === platform
                ? Object.assign({}, h, { subscribed: currentlySubscribed })
                : h;
            });
          });
          setErr(String(e.message || e));
        })
        .finally(function () {
          setHomeBusy(function (b) {
            const next = Object.assign({}, b);
            delete next[platform];
            return next;
          });
        });
    };

    return h("div", { className: "hermes-kanban-drawer-shade", onClick: props.onClose },
      h("div", {
        className: "hermes-kanban-drawer",
        onClick: function (e) { e.stopPropagation(); },
      },
        h("div", { className: "hermes-kanban-drawer-head" },
          h("span", { className: "text-xs text-muted-foreground" }, props.taskId),
          h("button", {
            type: "button",
            onClick: props.onClose,
            className: "hermes-kanban-drawer-close",
            title: tx(t, "close", "Close (Esc)"),
          }, "×"),
        ),
        loading ? h("div", { className: "p-4 text-sm text-muted-foreground" },
          tx(t, "loadingDetail", "Loading…")) :
        err ? h("div", { className: "p-4 text-sm text-destructive" }, err) :
        data ? h(TaskDetail, {
          data, editing, setEditing,
          renderMarkdown: props.renderMarkdown,
          allTasks: props.allTasks,
          assignees: props.assignees || [],
          boardSlug: boardSlug,
          evidenceAligned: props.evidenceAligned,
          onPatch: doPatch,
          onSpecify: doSpecify,
          onDecompose: doDecompose,
          onAddParent: addLink,
          onRemoveParent: removeLink,
          onAddChild: addChild,
          onRemoveChild: removeChild,
          homeChannels: homeChannels,
          homeBusy: homeBusy,
          onToggleHomeSub: toggleHomeSubscription,
          onRefresh: props.onRefresh,
          onUpload: handleUpload,
          onDeleteAttachment: handleDeleteAttachment,
          uploadBusy: uploadBusy,
          uploadErr: uploadErr,
          onOpenTask: function (taskId) {
            props.onClose();
            if (props.onOpenTask) props.onOpenTask(taskId);
          },
                    requestDialog: props.requestDialog,
        }) : null,
        data ? h("div", { className: "hermes-kanban-drawer-comment-foot" },
          h("div", {
            className: "hermes-kanban-comment-hint text-xs text-muted-foreground",
            title: tx(t, "commentHintTitle",
              "Comments are the channel for talking to a task's worker. They land on the thread immediately — no need to block the task first. A running worker picks the thread up on its next kanban_show() or respawn; blocking is only for when you want the worker to STOP and wait for your input."),
          },
            "ⓘ ",
            tx(t, "commentHint",
              "Comments reach the worker on its next run or kanban_show() — no need to block the task first."),
          ),
          h("div", { className: "hermes-kanban-drawer-comment-row" },
            h(Input, {
              value: newComment,
              onChange: function (e) { setNewComment(e.target.value); },
              onKeyDown: function (e) {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault(); handleComment();
                }
              },
              placeholder: tx(t, "addComment", "Add a comment… (Enter to submit)"),
              className: "h-8 text-sm flex-1",
            }),
            h(Button, {
              onClick: handleComment,
              size: "sm",
            }, tx(t, "comment", "Comment")),
          ),
        ) : null,
      ),
    );
  }

  function _fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  // Attachments section in the task drawer (#35338). Upload button +
  // list with download links and a delete (×) per row. The download
  // link hits GET /attachments/:id which streams the file; the worker
  // context surfaces the same files' absolute paths so a kanban worker
  // can read them with the file/terminal tools.
  function AttachmentsSection(props) {
    const i18n = props.i18n;
    const atts = props.attachments || [];
    const fileRef = useRef(null);
    const [dlErr, setDlErr] = useState(null);
    // Download via authenticated fetch → blob → synthetic anchor click.
    // A plain <a href> can't carry the auth the dashboard middleware requires,
    // so fetch authenticated and hand the browser a blob URL instead.
    function downloadAttachment(a) {
      // SDK.authedFetch handles auth in BOTH modes (loopback token header /
      // gated cookie) and applies the dashboard base-path prefix. The old
      // hand-rolled Authorization:Bearer + credentials:'same-origin' sent an
      // empty token and 401'd in gated mode.
      const url = withBoard(`${API}/attachments/${a.id}`, props.boardSlug);
      setDlErr(null);
      SDK.authedFetch(url)
        .then(function (resp) {
          if (!resp.ok) {
            return resp.text().then(function (txt) {
              // A 404 is a missing file, surfaced distinctly from the metadata
              // row (never a silent content claim). In evidence mode the
              // metadata came from the helper, so mark the 404 explicitly.
              throw new Error((props.evidenceMode && resp.status === 404 ? "missing file: " : "") +
                parseApiErrorMessage(new Error(resp.status + ": " + txt)));
            });
          }
          return resp.blob();
        })
        .then(function (blob) {
          const objUrl = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = objUrl;
          link.download = a.filename || "attachment";
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          setTimeout(function () { URL.revokeObjectURL(objUrl); }, 10000);
        })
        .catch(function (e) { setDlErr(String(e.message || e)); });
    }
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        `${props.evidenceMode ? "Evidence attachments" : tx(i18n, "attachments", "Attachments")} (${atts.length})`),
      h("input", {
        ref: fileRef,
        type: "file",
        multiple: true,
        style: { display: "none" },
        onChange: function (e) {
          if (props.onUpload) props.onUpload(e.target.files);
          // Reset so selecting the same file again re-triggers onChange.
          try { e.target.value = ""; } catch (_e) { /* ignore */ }
        },
      }),
      h("div", { className: "flex items-center gap-2 mb-2" },
        h(Button, {
          size: "sm",
          variant: "outline",
          disabled: !!props.uploadBusy,
          onClick: function () { if (fileRef.current) fileRef.current.click(); },
        }, props.uploadBusy
            ? tx(i18n, "uploading", "Uploading…")
            : tx(i18n, "uploadFile", "Upload file")),
      ),
      (props.uploadErr || dlErr)
        ? h("div", { className: "text-xs text-destructive mb-2" }, props.uploadErr || dlErr)
        : null,
      atts.length === 0
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(i18n, "noAttachments", "— no attachments —"))
        : atts.map(function (a) {
            return h("div", {
              key: a.id,
              className: "flex items-center justify-between gap-2 py-1 text-sm",
            },
              h("button", {
                type: "button",
                className: "hermes-kanban-attachment-link truncate",
                title: a.filename,
                onClick: function () { downloadAttachment(a); },
              }, a.filename),
              h("span", { className: "text-xs text-muted-foreground whitespace-nowrap" },
                _fmtBytes(a.size)),
              h("button", {
                type: "button",
                className: "hermes-kanban-drawer-close",
                title: tx(i18n, "removeAttachment", "Remove attachment"),
                onClick: function () {
                  if (props.requestDialog) {
                    props.requestDialog({
                      kind: "confirm",
                      title: tx(i18n, "removeAttachment", "Remove attachment"),
                      description: tx(i18n, "confirmRemoveAttachment",
                        "Remove this attachment?"),
                      confirmLabel: tx(i18n, "common.delete", "Delete"),
                      destructive: true,
                    }).then(function (r) {
                      if (r.confirmed && props.onDelete) props.onDelete(a.id);
                    }).catch(function () { /* cancelled */ });
                  } else if (window.confirm(tx(i18n, "confirmRemoveAttachment",
                      "Remove this attachment?"))) {
                    if (props.onDelete) props.onDelete(a.id);
                  }
                },
              }, "×"),
            );
          }),
      props.evidenceMode
        ? h(EvidenceLoadMoreButton, {
            resource: "attachments",
            hasMore: props.hasMore,
            omitted: props.omitted,
            loadingMore: props.loadingMore,
            onLoadMore: props.onLoadMore,
          })
        : null,
    );
  }

  function TaskDetail(props) {
    const { t: i18n } = useI18n();
    const t = props.data.task;
    const comments = props.data.comments || [];
    const links = props.data.links || { parents: [], children: [] };
    const childResults = props.data.child_results || [];

    // K10: on an EVO-aligned board, runs/events/attachments are NOT
    // materialised by the legacy detail read (TaskDrawer sends
    // include_history=false) and are instead paged through /evidence/page with
    // one stable cursor per resource. When not aligned, the legacy canonical
    // read (props.data.runs/events/attachments) is used unchanged.
    const evidenceAligned = !!props.evidenceAligned;
    const runsPage = useEvidenceResourcePage(props.boardSlug, t.id, "runs", evidenceAligned);
    const eventsPage = useEvidenceResourcePage(props.boardSlug, t.id, "events", evidenceAligned);
    const attachmentsPage = useEvidenceResourcePage(props.boardSlug, t.id, "attachments", evidenceAligned);

    const runs = evidenceAligned ? runsPage.items : (props.data.runs || []);
    const events = evidenceAligned ? eventsPage.items : (props.data.events || []);
    const attachments = evidenceAligned ? attachmentsPage.items : (props.data.attachments || []);
    const diagnosticsUnknown = props.data.diagnostics_state === "UNKNOWN";

    return h("div", { className: "hermes-kanban-drawer-body" },
      h("div", { className: "hermes-kanban-drawer-title" },
        h("span", { className: cn("hermes-kanban-dot", COLUMN_DOT[t.status]) }),
        props.editing
          ? h(TitleEditor, {
              initial: t.title || "",
              onSave: function (newTitle) {
                return props.onPatch({ title: newTitle }).then(function () { props.setEditing(false); });
              },
              onCancel: function () { props.setEditing(false); },
            })
          : h("span", {
              className: "hermes-kanban-drawer-title-text",
              title: tx(i18n, "clickToEdit", "Click to edit"),
              onClick: function () { props.setEditing(true); },
            }, t.title || tx(i18n, "untitled", "(untitled)")),
      ),
      h("div", { className: "hermes-kanban-drawer-meta" },
        h(MetaRow, { label: tx(i18n, "status", "Status"), value: t.status }),
        (t.status === "blocked" && t.block_reason) ? h(MetaRow, {
          label: tx(i18n, "blockReason", "Block reason"),
          value: t.block_reason,
        }) : null,
        h(AssigneeEditor, { task: t, onPatch: props.onPatch }),
        h(PriorityEditor, { task: t, onPatch: props.onPatch }),
        h(ModelEditor, { task: t, onPatch: props.onPatch }),
        t.tenant ? h(MetaRow, { label: tx(i18n, "tenant", "Tenant"), value: t.tenant }) : null,
        h(MetaRow, {
          label: tx(i18n, "workspace", "Workspace"),
          value: `${t.workspace_kind}${t.workspace_path ? ": " + t.workspace_path : ""}`,
        }),
        (t.skills && t.skills.length > 0) ? h(MetaRow, {
          label: tx(i18n, "skills", "Skills"),
          value: t.skills.join(", "),
        }) : null,
        t.goal_mode ? h(MetaRow, {
          label: tx(i18n, "goalMode", "Goal mode"),
          value: t.goal_max_turns
            ? `on (max ${t.goal_max_turns} turns)`
            : "on",
        }) : null,
        t.created_by ? h(MetaRow, { label: tx(i18n, "createdBy", "Created by"), value: t.created_by }) : null,
      ),
      h(StatusActions, {
        task: t,
        onPatch: props.onPatch,
        onSpecify: props.onSpecify,
        onDecompose: props.onDecompose,
      }),
      h(DiagnosticsSection, {
        task: t,
        boardSlug: props.boardSlug,
        assignees: props.assignees,
        diagnostics: t.diagnostics || [],
        diagnosticsUnknown: diagnosticsUnknown,
        onRefresh: props.onRefresh,
      }),
      h(HomeSubsSection, {
        homeChannels: props.homeChannels || [],
        homeBusy: props.homeBusy || {},
        onToggle: props.onToggleHomeSub,
      }),
      h(BodyEditor, {
        task: t,
        renderMarkdown: props.renderMarkdown,
        onPatch: props.onPatch,
      }),
      h(DependencyEditor, {
        task: t,
        links, allTasks: props.allTasks,
        onAddParent: props.onAddParent,
        onRemoveParent: props.onRemoveParent,
        onAddChild: props.onAddChild,
        onRemoveChild: props.onRemoveChild,
      }),
      (function () {
        var finalResult = t.result || t.latest_summary || null;
        var isDone = t.status === "done";
        var isParent = links.children.length > 0;
        if (finalResult) {
          var label = t.result
            ? tx(i18n, "result", "Result")
            : tx(i18n, "finalResult", "Final Result (run summary)");
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, label),
            h(MarkdownBlock, { source: finalResult, enabled: props.renderMarkdown }),
          );
        }
        if (isDone && isParent) {
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")),
            h("div", { className: "hermes-kanban-done-no-result hermes-kanban-done-parent-note" },
              tx(i18n, "doneParentNote",
                "This card is an orchestrator / parent task. Review the child results section for the substantive work."),
            ),
          );
        }
        if (isDone) {
          return h("div", { className: "hermes-kanban-section" },
            h("div", { className: "hermes-kanban-section-head" }, tx(i18n, "result", "Result")),
            h("div", { className: "hermes-kanban-done-no-result" },
              tx(i18n, "doneNoResult",
                "No final result was recorded. Check Run History, Logs, or Child Tasks for the worker output."),
            ),
          );
        }
        return null;
      })(),
      childResults.length > 0 ? h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          `${tx(i18n, "childResults", "Child Results")} (${childResults.length})`),
        childResults.map(function (child) {
          var childResult = child.result || child.latest_summary || null;
          return h("div", { key: child.id, className: "hermes-kanban-comment" },
            h("div", { className: "hermes-kanban-comment-head" },
              h("span", { className: "hermes-kanban-comment-author" },
                `${child.id} · ${child.title || tx(i18n, "untitled", "(untitled)")}`),
              h(Badge, { variant: "outline" }, child.status),
              h("button", {
                type: "button",
                className: "hermes-kanban-diag-action-btn",
                onClick: function () { if (props.onOpenTask) props.onOpenTask(child.id); },
              }, tx(i18n, "open", "Open")),
            ),
            childResult
              ? h(MarkdownBlock, { source: childResult, enabled: props.renderMarkdown })
              : h("div", { className: "text-xs text-muted-foreground" },
                  tx(i18n, "noChildResult", "No result recorded yet.")),
          );
        }),
      ) : null,
      h(AttachmentsSection, {
        attachments: attachments,
        boardSlug: props.boardSlug,
        onUpload: props.onUpload,
        onDelete: props.onDeleteAttachment,
        uploadBusy: props.uploadBusy,
        uploadErr: props.uploadErr,
        i18n: i18n,
        requestDialog: props.requestDialog,
        evidenceMode: evidenceAligned,
        hasMore: evidenceAligned ? attachmentsPage.hasMore : false,
        omitted: evidenceAligned ? attachmentsPage.omitted : null,
        loadingMore: evidenceAligned ? attachmentsPage.loadingMore : false,
        onLoadMore: evidenceAligned ? attachmentsPage.loadMore : null,
      }),
      h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          `${tx(i18n, "comments", "Comments")} (${comments.length})`),
        comments.length === 0
          ? h("div", { className: "text-xs text-muted-foreground" },
              tx(i18n, "noComments", "— no comments —"))
          : comments.map(function (c) {
              return h("div", { key: c.id, className: "hermes-kanban-comment" },
                h("div", { className: "hermes-kanban-comment-head" },
                  h("span", { className: "hermes-kanban-comment-author" }, c.author || "anon"),
                  h("span", { className: "hermes-kanban-comment-ago" },
                    timeAgo ? timeAgo(c.created_at) : ""),
                ),
                h(MarkdownBlock, { source: c.body, enabled: props.renderMarkdown }),
              );
            }),
      ),
      h("div", { className: "hermes-kanban-section" },
        h("div", { className: "hermes-kanban-section-head" },
          `${tx(i18n, "events", "Events")} (${events.length})`),
        (evidenceAligned ? events.slice().reverse() : events.slice().reverse().slice(0, 20)).map(function (e) {
          const isDiag = isDiagnosticEvent(e.kind);
          const phantoms = isDiag ? phantomIdsFromEvent(e) : [];
          return h("div", {
            key: e.id,
            className: cn(
              "hermes-kanban-event",
              isDiag ? "hermes-kanban-event--hallucination" : "",
            ),
          },
            isDiag
              ? h("div", { className: "hermes-kanban-event-header" },
                  h("span", { className: "hermes-kanban-event-warning-icon" }, "⚠"),
                  h("span", { className: "hermes-kanban-event-warning-label" },
                    getDiagnosticEventLabel(i18n, e.kind) || e.kind),
                  h("span", { className: "hermes-kanban-event-ago" },
                    timeAgo ? timeAgo(e.created_at) : ""),
                )
              : h("div", { className: "hermes-kanban-event-header-plain" },
                  h("span", { className: "hermes-kanban-event-kind" }, e.kind),
                  h("span", { className: "hermes-kanban-event-ago" },
                    timeAgo ? timeAgo(e.created_at) : ""),
                ),
            isDiag && phantoms.length > 0
              ? h("div", { className: "hermes-kanban-event-phantom-row" },
                  h("span", { className: "hermes-kanban-event-phantom-label" },
                    tx(i18n, "phantomIds", "Phantom ids:")),
                  phantoms.map(function (pid) {
                    return h("code", {
                      key: pid,
                      className: "hermes-kanban-event-phantom-chip",
                    }, pid);
                  }),
                )
              : null,
            e.payload && !isDiag
              ? h("code", { className: "hermes-kanban-event-payload" },
                  JSON.stringify(e.payload))
              : null,
          );
        }),
        evidenceAligned
          ? h(EvidenceLoadMoreButton, {
              resource: "events",
              hasMore: eventsPage.hasMore,
              omitted: eventsPage.omitted,
              loadingMore: eventsPage.loadingMore,
              onLoadMore: eventsPage.loadMore,
            })
          : null,
      ),
      h(WorkerLogSection, { taskId: t.id, boardSlug: props.boardSlug }),
      props.evidenceAligned
        ? h(WorkerEvidenceSection, { boardSlug: props.boardSlug, cardId: t.id })
        : null,
      h(RunHistorySection, {
        runs: runs,
        paged: evidenceAligned,
        total: evidenceAligned ? runsPage.total : null,
        hasMore: evidenceAligned ? runsPage.hasMore : false,
        omitted: evidenceAligned ? runsPage.omitted : null,
        loadingMore: evidenceAligned ? runsPage.loadingMore : false,
        onLoadMore: evidenceAligned ? runsPage.loadMore : null,
      }),
      evidenceAligned ? h(WorkflowTimelineSection, { boardSlug: props.boardSlug, cardId: t.id }) : null,
      evidenceAligned ? h(WorkflowReadinessSection, { boardSlug: props.boardSlug, cardId: t.id, task: t }) : null,
      evidenceAligned ? h(DrawerNextTenSection, { boardSlug: props.boardSlug, cardId: t.id, task: t, runs: runsPage, attachments: attachmentsPage.items }) : null,
      evidenceAligned ? h(WorkflowContinuationSection, { boardSlug: props.boardSlug, cardId: t.id, task: t, onOpenTask: props.onOpenTask }) : null,
      evidenceAligned ? h(WorkflowHoldSection, { boardSlug: props.boardSlug, cardId: t.id, task: t }) : null,
    );
  }

  // Per-attempt history. Closed runs first (most recent last), then the
  // active run if any. Each row shows profile / outcome / elapsed /
  // summary. Collapsed by default when there are more than three runs.
  function RunHistorySection(props) {
    const { t } = useI18n();
    const runs = props.runs || [];
    const [expanded, setExpanded] = useState(false);
    const paged = !!props.paged;
    const hasLoadMore = paged && !!props.hasMore;
    if (runs.length === 0 && !hasLoadMore) return null;
    // Paged (bounded evidence) shows every loaded row plus a Load-more; the
    // "+N earlier" collapse only applies to a fully-loaded unbounded list.
    const showAll = paged ? true : (expanded || runs.length <= 3);
    const visible = showAll ? runs : runs.slice(-3);
    const countLabel = paged && props.total != null ? props.total : runs.length;

    const fmtElapsed = function (run) {
      if (!run || !run.started_at) return "";
      const end = run.ended_at || Math.floor(Date.now() / 1000);
      const secs = Math.max(0, end - run.started_at);
      if (secs < 60) return `${secs}s`;
      if (secs < 3600) return `${Math.round(secs / 60)}m`;
      return `${(secs / 3600).toFixed(1)}h`;
    };

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          `${tx(t, "runHistory", "Run history")} (${countLabel})`),
        !showAll
          ? h("button", {
              type: "button",
              onClick: function () { setExpanded(true); },
              className: "hermes-kanban-edit-link",
              title: tx(t, "showAllAttempts", "Show all attempts"),
            }, `+${runs.length - 3} earlier`)
          : null,
      ),
      visible.map(function (r) {
        const outcomeClass = r.ended_at
          ? `hermes-kanban-run--${r.outcome || r.status || "ended"}`
          : "hermes-kanban-run--active";
        return h("div", { key: r.id, className: cn("hermes-kanban-run", outcomeClass) },
          h("div", { className: "hermes-kanban-run-head" },
            h("span", { className: "hermes-kanban-run-outcome" },
              r.ended_at ? (r.outcome || r.status || tx(t, "ended", "ended")) : tx(t, "active", "active")),
            h("span", { className: "hermes-kanban-run-profile" },
              r.profile ? `@${r.profile}` : tx(t, "noProfile", "(no profile)")),
            h("span", { className: "hermes-kanban-run-elapsed" }, fmtElapsed(r)),
            h("span", { className: "hermes-kanban-run-ago" },
              timeAgo ? timeAgo(r.started_at) : ""),
          ),
          r.summary
            ? h("div", { className: "hermes-kanban-run-summary" }, r.summary)
            : null,
          r.error
            ? h("div", { className: "hermes-kanban-run-error" }, r.error)
            : null,
          (r.metadata && Object.keys(r.metadata).length > 0)
            ? (function () {
                var json = JSON.stringify(r.metadata, null, 2);
                var collapsed = json.length > 300;
                return h("details", {
                    className: "hermes-kanban-run-meta-block",
                    open: !collapsed,
                  },
                  h("summary", { className: "hermes-kanban-run-meta-label" }, "Metadata"),
                  h("code", { className: "hermes-kanban-run-meta" }, json),
                );
              })()
            : null,
        );
      }),
      hasLoadMore
        ? h(EvidenceLoadMoreButton, {
            resource: "runs",
            hasMore: props.hasMore,
            omitted: props.omitted,
            loadingMore: props.loadingMore,
            onLoadMore: props.onLoadMore,
          })
        : null,
    );
  }

  // Worker log: loads lazily (one GET on mount), refresh button, tail cap.
  function WorkerLogSection(props) {
    const { t } = useI18n();
    const [state, setState] = useState({ loading: false, data: null, err: null });
    const load = useCallback(function () {
      setState({ loading: true, data: null, err: null });
      SDK.fetchJSON(withBoard(`${API}/tasks/${encodeURIComponent(props.taskId)}/log?tail=100000`, props.boardSlug))
        .then(function (d) { setState({ loading: false, data: d, err: null }); })
        .catch(function (e) { setState({ loading: false, data: null, err: String(e.message || e) }); });
    }, [props.taskId, props.boardSlug]);

    // Auto-load when the section mounts; the user opened the drawer so the
    // cost is one small HTTP round-trip.
    useEffect(function () { load(); }, [load]);

    const data = state.data;
    let body;
    if (state.loading) {
      body = h("div", { className: "text-xs text-muted-foreground" },
        tx(t, "loadingLog", "Loading log…"));
    } else if (state.err) {
      body = h("div", { className: "text-xs text-destructive" }, state.err);
    } else if (!data || !data.exists) {
      body = h("div", { className: "text-xs text-muted-foreground italic" },
        tx(t, "noWorkerLog",
          "— no worker log yet (task hasn't spawned or log was rotated away) —"));
    } else {
      body = h("pre", { className: "hermes-kanban-pre hermes-kanban-log" },
        data.content || "(empty)");
    }

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" },
          tx(t, "workerLog", "Worker log") + (data && data.size_bytes ? ` (${data.size_bytes} B)` : "")),
        h("button", {
          type: "button",
          onClick: load,
          className: "hermes-kanban-edit-link",
          title: "Refresh log",
        }, "refresh"),
      ),
      body,
      data && data.truncated
        ? h("div", { className: "text-xs text-muted-foreground" },
            tx(t, "logTruncated", "(showing last 100 KB — full log at "),
            data.path,
            tx(t, "logAt", ")"))
        : null,
    );
  }

  function MetaRow(props) {
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, props.label),
      h("span", { className: "hermes-kanban-meta-value" }, props.value),
    );
  }

  function TitleEditor(props) {
    const { t } = useI18n();
    const [v, setV] = useState(props.initial);
    const save = function () {
      const trimmed = v.trim();
      if (!trimmed) return;
      props.onSave(trimmed);
    };
    return h("div", { className: "hermes-kanban-edit-row" },
      h(Input, {
        value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") props.onCancel();
        },
        className: "h-8 text-sm flex-1",
      }),
      h(Button, { onClick: save,
        size: "sm",
      }, tx(t, "save", "Save")),
      h(Button, { onClick: props.onCancel,
        size: "sm",
      }, tx(t, "cancel", "Cancel")),
    );
  }

  function AssigneeEditor(props) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(props.task.assignee || "");
    useEffect(function () { setV(props.task.assignee || ""); }, [props.task.assignee]);
    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
        h("span", {
          className: "hermes-kanban-meta-value hermes-kanban-editable",
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEditAssignee", "Click to edit assignee"),
        }, props.task.assignee || tx(t, "unassigned", "unassigned")),
      );
    }
    const save = function () {
      props.onPatch({ assignee: v.trim() || "" }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "assignee", "Assignee")),
      h(Input, {
        value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") setEditing(false);
        },
        placeholder: tx(t, "emptyAssignee", "(empty = unassign)"),
        className: "h-7 text-xs flex-1",
        style: { textTransform: "none" },
        autoCapitalize: "none",
        autoCorrect: "off",
        spellCheck: false,
      }),
    );
  }

  function PriorityEditor(props) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(String(props.task.priority || 0));
    useEffect(function () { setV(String(props.task.priority || 0)); }, [props.task.priority]);
    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
        h("span", {
          className: "hermes-kanban-meta-value hermes-kanban-editable",
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEdit", "Click to edit"),
        }, String(props.task.priority)),
      );
    }
    const save = function () {
      props.onPatch({ priority: Number(v) || 0 }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "priority", "Priority")),
      h(Input, {
        type: "number", value: v, autoFocus: true,
        onChange: function (e) { setV(e.target.value); },
        onKeyDown: function (e) {
          if (e.key === "Enter") { e.preventDefault(); save(); }
          if (e.key === "Escape") setEditing(false);
        },
        className: "h-7 text-xs w-20",
      }),
    );
  }

  // Module-level cache for the model-options catalog so opening several
  // task drawers doesn't refetch. { providers: [{slug,label,models}] }
  let _modelCatalogCache = null;
  let _modelCatalogPromise = null;
  function fetchModelCatalog() {
    if (_modelCatalogCache) return Promise.resolve(_modelCatalogCache);
    if (_modelCatalogPromise) return _modelCatalogPromise;
    _modelCatalogPromise = SDK.fetchJSON(`${API}/model-options`)
      .then(function (data) {
        _modelCatalogCache = data && Array.isArray(data.providers) ? data : { providers: [] };
        return _modelCatalogCache;
      })
      .catch(function () {
        _modelCatalogPromise = null; // allow retry on next open
        return { providers: [] };
      });
    return _modelCatalogPromise;
  }

  // Per-task model override dropdown. Value encoding: "" = profile
  // default; "<slug>\u0000<model>" = provider+model pair (the separator
  // can't appear in either half). A catalog fetch failure degrades to a
  // free-text input so the override is still settable.
  function ModelEditor(props) {
    const { t } = useI18n();
    const task = props.task;
    const [editing, setEditing] = useState(false);
    const [catalog, setCatalog] = useState(_modelCatalogCache);
    const [busy, setBusy] = useState(false);
    const [freeText, setFreeText] = useState("");

    useEffect(function () {
      if (!editing || catalog) return;
      let alive = true;
      fetchModelCatalog().then(function (data) {
        if (alive) setCatalog(data);
      });
      return function () { alive = false; };
    }, [editing, catalog]);

    const current = task.model_override
      ? (task.provider_override
          ? `${task.provider_override}: ${task.model_override}`
          : task.model_override)
      : tx(t, "modelProfileDefault", "profile default");

    if (!editing) {
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
        h("span", {
          className: cn(
            "hermes-kanban-meta-value hermes-kanban-editable",
            !task.model_override ? "text-muted-foreground" : "",
          ),
          onClick: function () { setEditing(true); },
          title: tx(t, "clickToEditModel",
            "Click to override the model for this task's next run"),
        }, current),
      );
    }

    const apply = function (patch) {
      setBusy(true);
      props.onPatch(patch).then(function () {
        setEditing(false);
      }).catch(function () {
        // onPatch surfaces its own toast; just re-enable the control.
      }).then(function () { setBusy(false); });
    };

    const onPick = function (value) {
      if (value === "") {
        apply({ clear_model_override: true });
        return;
      }
      const sep = value.indexOf("\u0000");
      if (sep === -1) {
        apply({ model_override: value });
        return;
      }
      apply({
        provider_override: value.slice(0, sep),
        model_override: value.slice(sep + 1),
      });
    };

    const providers = (catalog && catalog.providers) || [];
    const loading = editing && !catalog;
    const currentValue = task.model_override
      ? (task.provider_override
          ? `${task.provider_override}\u0000${task.model_override}`
          : task.model_override)
      : "";

    // Free-text fallback when the catalog is empty (inventory unavailable
    // or zero authenticated providers).
    if (!loading && providers.length === 0) {
      const saveFree = function () {
        const v = freeText.trim();
        if (!v) { apply({ clear_model_override: true }); return; }
        apply({ model_override: v });
      };
      return h("div", { className: "hermes-kanban-meta-row" },
        h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
        h(Input, {
          value: freeText, autoFocus: true, disabled: busy,
          placeholder: tx(t, "modelFreeTextPlaceholder", "model name (empty = profile default)"),
          onChange: function (e) { setFreeText(e.target.value); },
          onKeyDown: function (e) {
            if (e.key === "Enter") { e.preventDefault(); saveFree(); }
            if (e.key === "Escape") setEditing(false);
          },
          className: "h-7 text-xs flex-1",
          style: { textTransform: "none" },
          autoCapitalize: "none", autoCorrect: "off", spellCheck: false,
        }),
      );
    }

    // Ensure the current override is selectable even when it's not in the
    // catalog (e.g. set from the CLI with a model the catalog doesn't list).
    let currentInCatalog = currentValue === "";
    for (let i = 0; i < providers.length && !currentInCatalog; i++) {
      const p = providers[i];
      for (let j = 0; j < p.models.length; j++) {
        const enc = `${p.slug}\u0000${p.models[j]}`;
        if (enc === currentValue || p.models[j] === currentValue) {
          currentInCatalog = true;
          break;
        }
      }
    }

    return h("div", { className: "hermes-kanban-meta-row" },
      h("span", { className: "hermes-kanban-meta-label" }, tx(t, "model", "Model")),
      loading
        ? h("span", { className: "hermes-kanban-meta-value text-muted-foreground" },
            tx(t, "modelLoading", "loading models…"))
        : h("select", {
            className: "hermes-kanban-recovery-select",
            value: currentValue,
            disabled: busy,
            autoFocus: true,
            onChange: function (e) { onPick(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === "Escape") setEditing(false);
            },
          },
            h("option", { value: "" },
              tx(t, "modelProfileDefaultOption", "(profile default)")),
            !currentInCatalog
              ? h("option", { value: currentValue }, current)
              : null,
            providers.map(function (p) {
              return h("optgroup", { key: p.slug, label: p.label || p.slug },
                p.models.map(function (m) {
                  return h("option", {
                    key: `${p.slug}\u0000${m}`,
                    value: `${p.slug}\u0000${m}`,
                  }, m);
                }),
              );
            }),
          ),
    );
  }

  function BodyEditor(props) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [v, setV] = useState(props.task.body || "");
    useEffect(function () { setV(props.task.body || ""); }, [props.task.body]);
    const save = function () {
      props.onPatch({ body: v }).then(function () { setEditing(false); });
    };
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head-row" },
        h("span", { className: "hermes-kanban-section-head" }, tx(t, "description", "Description")),
        editing
          ? h("div", { className: "flex gap-1" },
              h(Button, { onClick: save,
                size: "sm",
              }, tx(t, "save", "Save")),
              h(Button, { onClick: function () { setEditing(false); setV(props.task.body || ""); },
                size: "sm",
              }, tx(t, "cancel", "Cancel")),
            )
          : h("button", {
              type: "button",
              onClick: function () { setEditing(true); },
              className: "hermes-kanban-edit-link",
              title: "Edit description",
            }, tx(t, "edit", "edit")),
      ),
      editing
        ? h("textarea", {
            className: "hermes-kanban-textarea",
            value: v,
            rows: 8,
            onChange: function (e) { setV(e.target.value); },
          })
        : props.task.body
          ? h(MarkdownBlock, { source: props.task.body, enabled: props.renderMarkdown })
          : h("div", { className: "text-xs text-muted-foreground italic" },
              tx(t, "noDescription", "— no description —")),
    );
  }

  function DependencyEditor(props) {
    const { t } = useI18n();
    const { task, links, allTasks } = props;
    const [newParent, setNewParent] = useState("");
    const [newChild, setNewChild] = useState("");
    // Filter out self + existing links when offering the "add" dropdown.
    const candidatesFor = function (excludeSet) {
      return (allTasks || []).filter(function (tk) {
        return tk.id !== task.id && !excludeSet.has(tk.id);
      });
    };
    const parentExclude = new Set([task.id, ...(links.parents || [])]);
    const childExclude  = new Set([task.id, ...(links.children || [])]);

    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" }, tx(t, "dependencies", "Dependencies")),
      h("div", { className: "hermes-kanban-deps-row" },
        h("span", { className: "hermes-kanban-deps-label" }, tx(t, "parents", "Parents:")),
        h("div", { className: "hermes-kanban-deps-chips" },
          (links.parents || []).length === 0
            ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
            : (links.parents || []).map(function (id) {
                return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                  id,
                  h("button", {
                    type: "button",
                    className: "hermes-kanban-dep-chip-x",
                    onClick: function () { props.onRemoveParent(id); },
                    title: tx(t, "removeDependency", "Remove dependency"),
                  }, "×"),
                );
              }),
        ),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h(Select, Object.assign({
          value: newParent,
          className: "h-7 text-xs flex-1",
        }, selectChangeHandler(setNewParent)),
          h(SelectOption, { value: "" }, tx(t, "addParent", "— add parent —")),
          candidatesFor(parentExclude).map(function (tk) {
            return h(SelectOption, { key: tk.id, value: tk.id },
              `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!newParent) return;
            props.onAddParent(newParent).then(function () { setNewParent(""); });
          },
          disabled: !newParent,
          size: "sm",
        }, "+ parent"),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h("span", { className: "hermes-kanban-deps-label" }, tx(t, "children", "Children:")),
        h("div", { className: "hermes-kanban-deps-chips" },
          (links.children || []).length === 0
            ? h("span", { className: "hermes-kanban-deps-empty" }, tx(t, "none", "none"))
            : (links.children || []).map(function (id) {
                return h("span", { key: id, className: "hermes-kanban-dep-chip" },
                  id,
                  h("button", {
                    type: "button",
                    className: "hermes-kanban-dep-chip-x",
                    onClick: function () { props.onRemoveChild(id); },
                    title: tx(t, "removeDependency", "Remove dependency"),
                  }, "×"),
                );
              }),
        ),
      ),
      h("div", { className: "hermes-kanban-deps-row" },
        h(Select, Object.assign({
          value: newChild,
          className: "h-7 text-xs flex-1",
        }, selectChangeHandler(setNewChild)),
          h(SelectOption, { value: "" }, tx(t, "addChild", "— add child —")),
          candidatesFor(childExclude).map(function (tk) {
            return h(SelectOption, { key: tk.id, value: tk.id },
              `${tk.id} — ${(tk.title || "").slice(0, 50)}`);
          }),
        ),
        h(Button, {
          onClick: function () {
            if (!newChild) return;
            props.onAddChild(newChild).then(function () { setNewChild(""); });
          },
          disabled: !newChild,
          size: "sm",
        }, "+ child"),
      ),
    );
  }

  function StatusActions(props) {
    const { t } = useI18n();
    const task = props.task;
    const [specifyBusy, setSpecifyBusy] = useState(false);
    const [specifyMsg, setSpecifyMsg] = useState(null);
    const [decomposeBusy, setDecomposeBusy] = useState(false);
    const [decomposeMsg, setDecomposeMsg] = useState(null);
    const b = function (label, patch, enabled, confirmMsg) {
      return h(Button, {
        onClick: function () {
          if (enabled === false) return;
          var finalPatch = patch;
          if (patch.status === "blocked") {
            var reason = window.prompt(
              tx(t, "blockReasonPromptOne",
                "Why is this task blocked? This reason is shown until it is unblocked."),
              "",
            );
            if (reason === null) return;
            reason = reason.trim();
            if (!reason) {
              window.alert(tx(t, "blockReasonRequired",
                "A block reason is required before marking a task blocked."));
              return;
            }
            finalPatch = Object.assign({}, patch, { block_reason: reason });
          }
          props.onPatch(finalPatch, { confirm: confirmMsg });
        },
        disabled: enabled === false,
        size: "sm",
      }, label);
    };

    // "Specify" appears only when the task is in the Triage column — the
    // one column where an auxiliary LLM pass is meaningful. Elsewhere
    // the backend would return ok:false with "not in triage" anyway,
    // so hiding the button keeps the action row uncluttered.
    const specifyButton = (task.status === "triage" && props.onSpecify)
      ? h(Button, {
          onClick: function () {
            if (specifyBusy) return;
            setSpecifyBusy(true);
            setSpecifyMsg(null);
            props.onSpecify().then(function (res) {
              if (res && res.ok) {
                const suffix = res.new_title
                  ? ` — retitled: ${res.new_title}`
                  : "";
                setSpecifyMsg({ ok: true, text: `Specified${suffix}` });
              } else {
                setSpecifyMsg({
                  ok: false,
                  text: "Specify failed: " + ((res && res.reason) || "unknown error"),
                });
              }
            }).catch(function (err) {
              setSpecifyMsg({
                ok: false,
                text: "Specify failed: " + (err.message || String(err)),
              });
            }).then(function () {
              setSpecifyBusy(false);
            });
          },
          disabled: specifyBusy,
          size: "sm",
        }, specifyBusy ? "Specifying…" : "✨ Specify")
      : null;

    // "Decompose" is the built-in decomposer fan-out. Like Specify, only
    // makes sense on triage-column tasks — elsewhere the backend short-
    // circuits with ok:false. When the decomposer returns fanout:false
    // we render the same single-task message as Specify; when it fans
    // out we report the child count for quick at-a-glance verification.
    const decomposeButton = (task.status === "triage" && props.onDecompose)
      ? h(Button, {
          onClick: function () {
            if (decomposeBusy) return;
            setDecomposeBusy(true);
            setDecomposeMsg(null);
            props.onDecompose().then(function (res) {
              if (res && res.ok) {
                if (res.fanout && res.child_ids && res.child_ids.length) {
                  setDecomposeMsg({
                    ok: true,
                    text: `Decomposed into ${res.child_ids.length} children: ${res.child_ids.join(", ")}`,
                  });
                } else {
                  const suffix = res.new_title
                    ? ` — retitled: ${res.new_title}`
                    : "";
                  setDecomposeMsg({
                    ok: true,
                    text: `Single task (no fanout)${suffix}`,
                  });
                }
              } else {
                setDecomposeMsg({
                  ok: false,
                  text: "Decompose failed: " + ((res && res.reason) || "unknown error"),
                });
              }
            }).catch(function (err) {
              setDecomposeMsg({
                ok: false,
                text: "Decompose failed: " + (err.message || String(err)),
              });
            }).then(function () {
              setDecomposeBusy(false);
            });
          },
          disabled: decomposeBusy,
          size: "sm",
        }, decomposeBusy ? "Decomposing…" : "⚗ Decompose")
      : null;

    return h("div", null,
      h("div", { className: "hermes-kanban-actions" },
        specifyButton,
        decomposeButton,
        b("→ triage",  { status: "triage" },   task.status !== "triage"),
        b("→ ready",   { status: "ready" },    task.status !== "ready"),
        // No direct → running button: /tasks/:id PATCH rejects status=running
        // with 400 (issue #19535). Tasks enter running only through the
        // dispatcher's claim_task path, which atomically creates the run row,
        // claim lock, and worker process metadata.
        b(tx(t, "block", "Block"),     { status: "blocked" },
          task.status === "running" || task.status === "ready",
          getDestructiveConfirm(t, "blocked")),
        b(tx(t, "unblock", "Unblock"),   { status: "ready" },    task.status === "blocked"),
        b(tx(t, "complete", "Complete"),  { status: "done" },
          task.status === "running" || task.status === "ready" || task.status === "blocked",
          getDestructiveConfirm(t, "done")),
        b(tx(t, "archive", "Archive"),   { status: "archived" }, task.status !== "archived",
          getDestructiveConfirm(t, "archived")),
      ),
      specifyMsg ? h("div", {
        className: specifyMsg.ok
          ? "hermes-kanban-msg-ok"
          : "hermes-kanban-msg-err",
      }, specifyMsg.text) : null,
      decomposeMsg ? h("div", {
        className: decomposeMsg.ok
          ? "hermes-kanban-msg-ok"
          : "hermes-kanban-msg-err",
      }, decomposeMsg.text) : null,
    );
  }


  // One toggle per gateway platform the user has a home channel set on
  // (telegram, discord, slack, etc.). Toggling on creates a kanban_notify_subs
  // row routed to that platform's home; toggling off removes it. Nothing
  // renders when no platforms have a home configured — this section stays
  // invisible for users who haven't set one up.
  function HomeSubsSection(props) {
    const { t } = useI18n();
    const channels = props.homeChannels || [];
    if (channels.length === 0) return null;
    const busy = props.homeBusy || {};
    return h("div", { className: "hermes-kanban-section" },
      h("div", { className: "hermes-kanban-section-head" },
        tx(t, "notifyHomeChannels", "Notify home channels")),
      h("div", { className: "hermes-kanban-home-subs" },
        channels.map(function (hc) {
          const isBusy = !!busy[hc.platform];
          const label = hc.subscribed ? "✓ " + hc.platform : hc.platform;
          const target = `${hc.name} (${hc.chat_id}${hc.thread_id ? " / " + hc.thread_id : ""})`;
          const title = hc.subscribed
            ? `${tx(t, "sendingUpdates", "Sending updates to")} ${target}. Click to stop.`
            : `${tx(t, "sendNotifications", "Send completed / blocked / gave_up notifications to")} ${target}.`;
          return h(Button, {
            key: hc.platform,
            size: "sm",
            title: title,
            disabled: isBusy || !props.onToggle,
            onClick: function () {
              if (props.onToggle) props.onToggle(hc.platform, hc.subscribed);
            },
            className: hc.subscribed
              ? "hermes-kanban-home-sub hermes-kanban-home-sub--on"
              : "hermes-kanban-home-sub",
          }, label);
        })
      )
    );
  }

  // -------------------------------------------------------------------------
  // Register
  // -------------------------------------------------------------------------

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("kanban", KanbanPage);
  }
})();
