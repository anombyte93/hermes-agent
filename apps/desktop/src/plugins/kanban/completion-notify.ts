/**
 * Native kanban terminal-event notification (completion, blocker, failure,
 * and review handoffs).
 *
 * No maintained exact-fit OSS exists and the SDK
 * has no kanban event door, so this module rides the kanban plugin's EXISTING
 * /events socket (api.ts onEventsFrame). No new WebSocket, no new process,
 * no DB, no auth, no persistence — cursor is an in-memory per-board high-water
 * mark. Notifies on the same terminal kinds the gateway watcher pings
 * (gateway/kanban_watchers.py): 'completed' (kanban_db.complete_task —
 * payload: summary + artifacts), 'blocked' (payload: reason), 'gave_up'
 * (payload: error), 'crashed', 'timed_out', and 'block_loop_detected'
 * (payload: reason — the routed-to-triage human handoff), plus the review
 * handoffs 'review_requested' (payload: summary) and 'changes_requested'
 * (payload: reason).
 *
 * Two delivery doors, complementary by design:
 *  - `host.notify` — the in-app toast, covers the foreground case;
 *  - `ctx.os.notify` (when bound) — the native OS notification, which the
 *    desktop shell fires only while the user is AWAY from Hermes. This is the
 *    door that covers "walked away and the worker hit a blocker".
 *
 * Cursor contract: first observation of a board baselines
 * seen[board] from a bounded /events/baseline read (baseline-now returns the
 * current high-water event id, never a full /board poll). Events id <= seen
 * are historical/replay — never notified, no cursor change. id > seen advances
 * cursor for EVERY kind; only terminal kinds emit. Reconnect replays from 0;
 * cursor filters. Board switch never mixes cursors; returning reuses prior
 * cursor (never reset to current MAX). Fail-closed: while a board's baseline
 * is unknown, no event can be classified so none is notified. Empty slug ('')
 * suppressed.
 *
 * Exact-card navigation: a terminal toast's action carries board/card through
 * the shared `$openCard` atom, consumed by KanbanBoardPage whether it is
 * already mounted or mounting next — never invented router query support.
 */

import { atom, host, type PluginOs, type PluginRestOptions, type PluginTranslate } from '@hermes/plugin-sdk'

import { en } from './i18n'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

export interface CompletionEvent {
  id?: unknown
  task_id?: string
  kind?: string
  payload?: Record<string, unknown> | null
  /** The run that produced this event (present on terminal kinds from the
   *  /events socket). A re-block under a NEW run id is a new intervention,
   *  even when the reason text is identical. */
  run_id?: null | number
}

/** Exact card open request, carried by a terminal toast's action and consumed
 *  by the mounted KanbanBoardPage. board + card are the real selected board
 *  and the event's task id — no invented router query. */
export const $openCard = atom<{ board: string; card: string } | null>(null)

type ToastKind = 'error' | 'info' | 'success' | 'warning'

/** Terminal kinds → toast severity + i18n title key. Mirrors the gateway
 *  watcher's ping set (gateway/kanban_watchers.py) minus the intentionally
 *  silent kinds (status/archived/unblocked, which only advance the cursor),
 *  plus the review handoffs. */
const TERMINAL_NOTIFY = new Map<string, { titleKey: string; toast: ToastKind }>([
  ['blocked', { titleKey: 'notify.blockedTitle', toast: 'warning' }],
  ['block_loop_detected', { titleKey: 'notify.blockLoopTitle', toast: 'warning' }],
  ['completed', { titleKey: 'notify.completedTitle', toast: 'success' }],
  ['crashed', { titleKey: 'notify.crashedTitle', toast: 'error' }],
  ['gave_up', { titleKey: 'notify.gaveUpTitle', toast: 'error' }],
  ['timed_out', { titleKey: 'notify.timedOutTitle', toast: 'warning' }],
  ['review_requested', { titleKey: 'notify.reviewRequestedTitle', toast: 'info' }],
  ['changes_requested', { titleKey: 'notify.changesRequestedTitle', toast: 'warning' }]
])

const seenEventIdByBoard = new Map<string, number>()
const baselinePending = new Set<string>()

// R5 — meaningful-intervention dedup. Kinds that ask Hayden for a decision
// (blocked / block_loop_detected / changes_requested) are fingerprint-deduped
// per (board, task): a repeated UNCHANGED intervention is quiet, a changed
// reason/remedy or a new run notifies. Completion notifications are NEVER
// fingerprint-deduped — they always fire. Recovery (unblock/reclaim) or a new
// run (claim/spawn) clears the stored fingerprint so a later identical block
// notifies again. The cursor (seenEventIdByBoard) remains board-isolated; this
// map is keyed by board+task so board switches never mix fingerprints.
const INTERVENTION_KINDS = new Set(['blocked', 'block_loop_detected', 'changes_requested'])

const CLEAR_INTERVENTION_KINDS = new Set([
  'unblocked',
  'claimed',
  'spawned',
  'reclaimed',
  'completed',
  'crashed',
  'gave_up',
  'timed_out',
  'review_requested'
])

const interventionByTask = new Map<string, string>()

let rest: Rest | null = null
let translate: PluginTranslate | null = null
let osDoor: PluginOs | null = null

/** Resolve a dot-path against the plugin's own English bundle — the same
 *  last-rung fallback the plugin i18n registry applies, usable before (or
 *  without) a bound translator. */
function fallbackT(key: string, ...args: unknown[]): string {
  let node: unknown = en

  for (const part of key.split('.')) {
    node = (node as Record<string, unknown> | undefined)?.[part]
  }

  if (typeof node === 'function') {
    return (node as (...a: unknown[]) => string)(...args)
  }

  return typeof node === 'string' ? node : key
}

function t(key: string, ...args: unknown[]): string {
  const translated = translate?.(key, ...args)

  // The registry returns the raw key when the bundle isn't registered yet.
  return translated && translated !== key ? translated : fallbackT(key, ...args)
}

export function bindCompletionNotify(r: Rest, pluginTranslate?: PluginTranslate, os?: PluginOs): void {
  rest = r
  translate = pluginTranslate ?? null
  osDoor = os ?? null
}

/** Baseline from a bounded changes read, never a full /board poll. The first
 *  /events/baseline call is "baseline-now": it returns the current high-water
 *  event id (evidence.baseline_id) without replaying history, and is capped by
 *  the server's own page bound. */
async function ensureBaseline(slug: string): Promise<void> {
  if (seenEventIdByBoard.has(slug) || baselinePending.has(slug)) {
    return
  }

  baselinePending.add(slug)

  try {
    const changes = (await rest!<{ state?: unknown; board?: unknown; evidence?: { baseline_id?: unknown } }>(
      `/events/baseline?board=${encodeURIComponent(slug)}`
    )) as { state?: unknown; board?: unknown; evidence?: { baseline_id?: unknown } }

    const baselineId = changes?.evidence?.baseline_id

    // Only a PASS, board-exact, nonnegative finite integer baseline is
    // authoritative. A FAIL/UNKNOWN or malformed HTTP 200 response must NOT
    // fall back to baseline 0 (that would notify historical events): leave the
    // board unknown so notifications stay suppressed and a later frame can
    // recover with a fresh read.
    const authoritative =
      changes?.state === 'PASS' &&
      changes?.board === slug &&
      typeof baselineId === 'number' &&
      Number.isFinite(baselineId) &&
      Number.isInteger(baselineId) &&
      baselineId >= 0

    if (authoritative) {
      seenEventIdByBoard.set(slug, baselineId as number)
    }
  } catch {
    // Fail-closed: unknown baseline → notifications stay suppressed.
  } finally {
    baselinePending.delete(slug)
  }
}

function trimmed(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** The human handoff carried in the event payload, per kind (mirrors the
 *  payload contract the gateway watcher reads). */
function bodyFor(kind: string, ev: CompletionEvent): string {
  const payload = ev.payload

  if (kind === 'completed' || kind === 'review_requested') {
    return trimmed(payload?.summary)
  }

  if (kind === 'blocked' || kind === 'block_loop_detected' || kind === 'changes_requested') {
    return trimmed(payload?.reason)
  }

  if (kind === 'gave_up') {
    return trimmed(payload?.error)
  }

  return ''
}

/** R5 fingerprint for an intervention kind: the kind, its reason/remedy
 *  payload, AND the run it came from. Two events with the same fingerprint are
 *  the same intervention. Including the run id means a worker that re-blocks
 *  with an identical reason under a NEW run id (with no claim/spawn frame
 *  reaching the renderer in between — a socket gap) is a NEW intervention and
 *  notifies, closing the quiet-window the kind+reason-only fingerprint had. */
function interventionFingerprint(kind: string, ev: CompletionEvent): string {
  const payload = ev.payload ?? {}
  const remedy = typeof payload.remedy === 'string' ? payload.remedy.trim() : ''
  const runId = typeof ev.run_id === 'number' && Number.isFinite(ev.run_id) ? String(ev.run_id) : ''

  return `${kind}\u0000${bodyFor(kind, ev)}\u0000${remedy}\u0000${runId}`
}

/** Map key for one (board, task) intervention slot — board-isolated. */
function interventionKey(slug: string, taskId: string): string {
  return `${slug}\u0000${taskId}`
}

function notifyOne(slug: string, kind: string, spec: { titleKey: string; toast: ToastKind }, ev: CompletionEvent): void {
  const taskId = (ev.task_id ?? '').trim()
  const body = bodyFor(kind, ev)

  const artifacts =
    kind === 'completed' && Array.isArray(ev.payload?.artifacts)
      ? (ev.payload!.artifacts as unknown[])
          .filter((a): a is string => typeof a === 'string' && a.trim().length > 0)
          .map(a => a.trim())
      : []

  const artifactText =
    artifacts.length === 1
      ? artifacts[0].split(/[\\/]/).pop() || artifacts[0]
      : artifacts.length > 1
        ? t('notify.artifacts', artifacts.length)
        : ''

  const detail = [taskId, artifactText].filter(Boolean).join(' · ')
  const title = t(spec.titleKey)
  const message = body || taskId || title
  host.notify({
    kind: spec.toast,
    title,
    message,
    ...(detail ? { detail } : {}),
    // Exact card open: carry board + card through the shared atom, then land
    // on the board page. The page consumes it on mount OR while already
    // mounted — no invented router query.
    action: {
      label: t('notify.openKanban'),
      onClick: () => {
        $openCard.set(taskId ? { board: slug, card: taskId } : null)
        host.navigate('/kanban')
      }
    }
  })

  // Native OS notification — the desktop shell fires it only while the user
  // is away from Hermes (the toast above covers the foreground case). Isolated:
  // a missing/broken shell must not mark the toast as unfired.
  try {
    osDoor?.notify({ title, body: [message, detail].filter(Boolean).join('\n') })
  } catch {
    /* swallowed */
  }
}

/** Consume one /events frame for a board. Returns true when a terminal-event
 *  notification was fired. Never throws: notification failure cannot
 *  interfere with api.ts cache invalidation. */
export async function onKanbanEventsFrame(slug: string, events?: CompletionEvent[]): Promise<boolean> {
  if (!events?.length || slug === '' || !rest) {
    return false
  }

  await ensureBaseline(slug)
  const seen = seenEventIdByBoard.get(slug)

  if (seen === undefined) {
    return false
  } // fail-closed

  let fired = false
  let cursor = seen

  for (const ev of events) {
    if (typeof ev.id !== 'number' || ev.id <= cursor) {
      continue
    }

    cursor = ev.id
    seenEventIdByBoard.set(slug, cursor)
    const kind = ev.kind ?? ''
    const taskId = (ev.task_id ?? '').trim()
    const slot = taskId ? interventionKey(slug, taskId) : null

    // Recovery / new run clears the stored intervention fingerprint so a later
    // identical block notifies again (never fingerprints a dead intervention).
    if (slot && CLEAR_INTERVENTION_KINDS.has(kind)) {
      interventionByTask.delete(slot)
    }

    const spec = TERMINAL_NOTIFY.get(kind)

    if (spec) {
      // R5: unchanged intervention is quiet; changed reason or a new run fires.
      if (slot && INTERVENTION_KINDS.has(kind)) {
        const fingerprint = interventionFingerprint(kind, ev)

        if (interventionByTask.get(slot) === fingerprint) {
          continue
        }

        interventionByTask.set(slot, fingerprint)
      }

      try {
        notifyOne(slug, kind, spec, ev)
        fired = true
      } catch {
        /* swallowed */
      }
    }
  }

  return fired
}
