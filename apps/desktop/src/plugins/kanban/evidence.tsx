/**
 * Worker evidence + identity alignment for the kanban UI.
 *
 * This is the ONLY surface that reads the read-only /evidence/* bridge, which
 * always reads the physical EVO host's database. It never borrows a local
 * board's cards/actions/downloads merely because a slug matches: every query
 * here is gated on `/evidence/context` reporting that the selected board's
 * local DB IS the same EVO database. When alignment is false (or unresolved)
 * the original local UI renders unchanged — no EVO badges or downloads, and
 * the mismatch is NEVER silenced: it says "choose the EVO connection".
 *
 * Board binding is explicit: every fetcher receives the resolved slug at call
 * time (never the global atom), and a blank slug is resolved from
 * /boards.current, so a late response from a previous board can never land
 * under a new board's query key.
 */

import { Codicon, useQuery, useValue } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  $boardSlug,
  BOARDS_KEY,
  evidenceContextKey,
  evidenceSnapshotKey,
  evidenceWorkerKey,
  fetchBoards,
  fetchEvidenceContext,
  fetchEvidenceSnapshot,
  fetchEvidenceWorker
} from './api'
import type {
  EvidenceContext,
  EvidenceSnapshotData,
  KanbanColumn,
  KanbanTask,
  WorkerEvidenceData,
  WorkerObservation
} from './types'
import { Callout, Section } from './ui'

export type WorkerState = 'running' | 'stopped' | 'unknown' | 'unavailable'

const STATE_TONE: Record<WorkerState, string> = {
  running: '#34d399',
  stopped: 'var(--ui-text-tertiary)',
  unknown: '#fbbf24',
  unavailable: 'var(--ui-text-quaternary)'
}

const STATE_LABEL: Record<WorkerState, string> = {
  running: 'Running',
  stopped: 'Stopped',
  unknown: 'Unknown',
  unavailable: 'Unavailable'
}

/**
 * Positive exact identity for a running observation: ALL FOUR must hold.
 * A bare `process_present` is not enough — a recycled pid can look present
 * while belonging to a different task.
 */
export function isRunningObservation(obs: WorkerObservation): boolean {
  return (
    obs.state === 'PASS' &&
    obs.process_present === true &&
    obs.workspace_matches === true &&
    obs.run_start_matches === true
  )
}

/**
 * Reduce a /evidence/worker payload to one of the four honest states.
 *
 * Running is POSITIVE EXACT identity only (see isRunningObservation).
 *
 * Stopped reads the NESTED `data.aggregate` (the released helper does NOT
 * emit top-level `complete`/`running`/`unknown`): aggregate.complete===true,
 * aggregate.running===0 and aggregate.unknown===0, PLUS actual stopped
 * evidence (a non-zero stopped or completion_records count — a complete
 * aggregate with zero runs is not "stopped"). A missing/empty observation or
 * a missing aggregate is UNKNOWN, never Stopped.
 */
export function resolveWorkerState(data: WorkerEvidenceData | null | undefined): WorkerState {
  if (!data) {
    return 'unknown'
  }

  const observations = Array.isArray(data.observations) ? data.observations : []

  if (observations.some(isRunningObservation)) {
    return 'running'
  }

  const aggregate = data.aggregate

  if (aggregate) {
    const runningCount = typeof aggregate.running === 'number' ? aggregate.running : 0
    const unknownCount = typeof aggregate.unknown === 'number' ? aggregate.unknown : 0
    const stoppedCount = typeof aggregate.stopped === 'number' ? aggregate.stopped : 0

    // Stopped needs the helper's own complete verdict PLUS actual stopped-run
    // evidence (aggregate.stopped > 0). A completion_records-only aggregate is
    // BOOKKEEPING (an upstream-authored row with no observed worker stop) —
    // that is UNKNOWN here, never Stopped.
    if (aggregate.complete === true && runningCount === 0 && unknownCount === 0 && stoppedCount > 0) {
      return 'stopped'
    }
  }

  return 'unknown'
}

/**
 * The selected board's real slug. A blank `$boardSlug` means "the server's
 * current board"; resolve it from /boards.current so the query keys and the
 * requests both carry the same, explicit board identity.
 */
function useResolvedBoardSlug(): string {
  const slug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  return slug || boards?.current || ''
}

/** Identity alignment for the selected board (short-lived, retry-off). The
 *  slug is captured explicitly and passed to the fetcher, so the request can
 *  never read a board that changed after the query was issued. */
export function useEvidenceContext(slug: string) {
  return useQuery({
    queryKey: evidenceContextKey(slug),
    queryFn: () => fetchEvidenceContext(slug),
    staleTime: 15_000,
    retry: false
  })
}

/** Bounded board snapshot (one per aligned refresh) — running/stopped/unknown
 *  worker evidence rolled up from the snapshot's own worker_observations. */
export function useEvidenceSnapshot(slug: string, aligned: boolean) {
  return useQuery({
    queryKey: evidenceSnapshotKey(slug, 'all', null),
    queryFn: () => fetchEvidenceSnapshot(slug, 'all', null),
    enabled: aligned,
    staleTime: 20_000,
    refetchInterval: 60_000,
    retry: false
  })
}

/** Worker-state rollup from a snapshot's worker_observations. Missing
 *  observation = UNKNOWN, never Stopped. */
export function resolveSnapshotWorkerStates(data: EvidenceSnapshotData | null | undefined): {
  running: number
  unknown: number
} {
  const observations = Array.isArray(data?.worker_observations) ? data.worker_observations : []

  let running = 0
  let unknown = 0

  for (const obs of observations) {
    if (isRunningObservation(obs)) {
      running += 1
    } else {
      unknown += 1
    }
  }

  return { running, unknown }
}

/** Per-card worker state map from a snapshot's worker_observations. A card
 *  with a positive observation maps to `running`, one with a non-positive
 *  observation to `unknown`; a card absent from the page has NO entry (the
 *  board falls back to its local status). */
export function snapshotWorkerStateMap(data: EvidenceSnapshotData | null | undefined): Map<string, WorkerState> {
  const observations = Array.isArray(data?.worker_observations) ? data.worker_observations : []
  const map = new Map<string, WorkerState>()

  for (const obs of observations) {
    if (obs.task_id) {
      map.set(obs.task_id, isRunningObservation(obs) ? 'running' : 'unknown')
    }
  }

  return map
}

/**
 * The board data source, resolved from the read-only /evidence/* bridge.
 *
 * `phase` is the discriminator the board renders from:
 *
 * - `resolving`: the /evidence/context identity check is still in flight. The
 *   board shows its loader and MUST NOT fall back to a local /board read, a
 *   local read here would silently present a board that may not be the EVO DB.
 * - `unaligned`: context resolved with `aligned === false`. The local /board is
 *   the honest source, and the board states the choose-the-EVO remedy.
 * - `context-error`: the identity check itself failed. Visible, never a silent
 *   local fallback.
 * - `aligned`: the local DB IS the EVO DB; the grid, the counts, and every
 *   worker badge come from the bounded snapshot. A failed snapshot or helper
 *   read sets `error` (visible), never a silent local substitute.
 */
export type BoardEvidence =
  | { phase: 'resolving' | 'unaligned' | 'context-error' }
  | {
      phase: 'aligned'
      states: Map<string, WorkerState>
      running: number
      unknown: number
      byStatus: Record<string, number>
      total: number
      omitted: number
      cards: KanbanTask[]
      hasMore: boolean
      nextCursor: null | string
      observedAt: null | number
      /** True when the first page's observed_at advanced while later pages were
       *  loaded (a refresh/board-switch dropped them) — the snapshot changed and
       *  paging restarted. Rendered as an explicit "snapshot changed" note. */
      snapshotChanged: boolean
      error: null | string
      loadMoreError: null | string
      loadMore: () => void
      loadingMore: boolean
    }

/** An empty aligned result for the two failure shapes (snapshot read failed,
 *  helper receipt not PASS). The board renders a visible error, never cards. */
function alignedError(error: string): Extract<BoardEvidence, { phase: 'aligned' }> {
  return {
    phase: 'aligned',
    states: new Map(),
    running: 0,
    unknown: 0,
    byStatus: {},
    total: 0,
    omitted: 0,
    cards: [],
    hasMore: false,
    nextCursor: null,
    observedAt: null,
    snapshotChanged: false,
    error,
    loadMoreError: null,
    loadMore: () => undefined,
    loadingMore: false
  }
}

export function useBoardEvidence(): BoardEvidence {
  const slug = useResolvedBoardSlug()
  const { data: context, isError: contextError } = useEvidenceContext(slug)
  const aligned = context?.aligned === true
  const { data: snapshotEnvelope, isError: snapshotError } = useEvidenceSnapshot(slug, aligned)

  // Load-more pages: cards appended beyond the first page, plus the cursor and
  // has_more of the LAST page loaded. Stamped with the first page's observed_at
  // so a refresh (or a board switch) advances the stamp and the stale pages are
  // ignored, a refresh can never mix old worker claims under a new timestamp.
  const [extra, setExtra] = useState<{ observedAt: null | number; cards: KanbanTask[]; cursor: null | string; hasMore: boolean }>({
    observedAt: null,
    cards: [],
    cursor: null,
    hasMore: false
  })

  const [loadingMore, setLoadingMore] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState<null | string>(null)
  const [snapshotChanged, setSnapshotChanged] = useState(false)
  const loadingRef = useRef(false)

  const snapshot = snapshotEnvelope?.state === 'PASS' ? snapshotEnvelope.evidence : null
  const observedAt = typeof snapshot?.observed_at === 'number' ? snapshot.observed_at : null

  // Guard against the past: pages stamped with an older observed_at (or board)
  // are dropped once the first page's timestamp advances.
  const liveExtra = extra.observedAt === observedAt ? extra : { observedAt, cards: [], cursor: null, hasMore: false }

  // When a refresh/board-switch advances the first page's stamp while later
  // pages were loaded, those pages are dropped and paging restarted — surface
  // it as an explicit "snapshot changed" signal (R10).
  useEffect(() => {
    if (extra.observedAt !== null && extra.observedAt !== observedAt) {
      setSnapshotChanged(true)
    }
  }, [extra.observedAt, observedAt])

  const firstCards = useMemo(() => snapshotCardsToViews(snapshot), [snapshot])

  const cards = liveExtra.cards.length > 0 ? [...firstCards, ...liveExtra.cards] : firstCards
  const hasMore = liveExtra.cards.length > 0 ? liveExtra.hasMore : snapshot?.has_more === true
  const nextCursor = liveExtra.cards.length > 0 ? liveExtra.cursor : (snapshot?.next_cursor ?? null)

  // Per-card worker state for EVERY loaded card: a positive observation maps to
  // running, everything else (weak OR missing) to unknown. A missing observation
  // is UNKNOWN, never the card's local status.
  const observedStates = snapshotWorkerStateMap(snapshot)
  const states = new Map<string, WorkerState>()

  for (const card of cards) {
    states.set(card.id, observedStates.get(card.id) ?? 'unknown')
  }

  const running = [...states.values()].filter(state => state === 'running').length
  const unknown = states.size - running

  // Load more fetches the NEXT page under the snapshot's stable cursor and
  // APPENDS fresh cards (duplicate ids from a keyset page under the high-water
  // bound are dropped). A board switch or refresh mid-flight stamps the append
  // with the old observed_at, so it can never relabel the current view.
  const loadMore = useCallback(() => {
    if (loadingRef.current) {
      return
    }

    const cursor = nextCursor

    if (!cursor) {
      return
    }

    loadingRef.current = true
    setLoadingMore(true)
    setLoadMoreError(null)
    setSnapshotChanged(false)

    fetchEvidenceSnapshot(slug, 'all', cursor)
      .then(envelope => {
        if (envelope.state !== 'PASS' || !envelope.evidence) {
          setLoadMoreError(envelope.reason ?? 'evidence page failed')

          return
        }

        const pageCards = snapshotCardsToViews(envelope.evidence)
        const known = new Set([...firstCards, ...liveExtra.cards].map(card => card.id))
        const fresh = pageCards.filter(card => !known.has(card.id))

        setExtra({
          observedAt,
          cards: [...liveExtra.cards, ...fresh],
          cursor: envelope.evidence.next_cursor ?? null,
          hasMore: envelope.evidence.has_more === true
        })
      })
      .catch(() => setLoadMoreError('evidence page failed'))
      .finally(() => {
        loadingRef.current = false
        setLoadingMore(false)
      })
  }, [slug, nextCursor, observedAt, firstCards, liveExtra.cards])

  if (contextError) {
    return { phase: 'context-error' }
  }

  if (!context) {
    return { phase: 'resolving' }
  }

  if (!context.aligned) {
    return { phase: 'unaligned' }
  }

  if (snapshotError) {
    return alignedError('evidence snapshot failed')
  }

  if (!snapshotEnvelope || snapshotEnvelope.state !== 'PASS' || !snapshot) {
    return alignedError(snapshotEnvelope?.reason ?? 'worker evidence unavailable')
  }

  return {
    phase: 'aligned',
    states,
    running,
    unknown,
    byStatus: snapshot.counts?.by_status ?? {},
    total: typeof snapshot.counts?.total === 'number' ? snapshot.counts.total : 0,
    omitted: typeof snapshot.counts?.omitted === 'number' ? snapshot.counts.omitted : 0,
    cards,
    hasMore,
    nextCursor,
    observedAt,
    snapshotChanged,
    error: null,
    loadMoreError,
    loadMore,
    loadingMore
  }
}

/** Convert one snapshot card row (helper contract: kanban_page cards items)
 *  into the board's KanbanTask view shape. Field names follow the released
 *  contract (id/title/status/assignee/…); unknown keys pass through ignored. */
export function snapshotCardToTask(raw: Record<string, unknown>): KanbanTask {
  const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
  const str = (v: unknown): null | string => (typeof v === 'string' ? v : null)

  return {
    id: String(raw.id ?? ''),
    title: typeof raw.title === 'string' ? raw.title : '',
    body: str(raw.body),
    status: typeof raw.status === 'string' ? raw.status : 'todo',
    assignee: str(raw.assignee),
    priority: num(raw.priority),
    tenant: str(raw.tenant),
    created_at: num(raw.created_at),
    latest_summary: str(raw.latest_summary),
    comment_count: num(raw.comment_count),
    started_at: str(raw.started_at) === null ? num(raw.started_at) : null,
    worker_pid: num(raw.worker_pid) ?? null,
    last_heartbeat_at: num(raw.last_heartbeat_at) ?? null
  }
}

/** All cards of one snapshot page, converted. */
export function snapshotCardsToViews(data: EvidenceSnapshotData | null | undefined): KanbanTask[] {
  const cards = Array.isArray(data?.cards) ? data.cards : []

  return cards.map(raw => snapshotCardToTask(raw))
}

/** Fold loaded pages (first page first) into the board's column shape.
 *  Duplicate ids (a keyset page can overlap after concurrent inserts under
 *  the high-water bound) are dropped, preserving first occurrence. */
export function viewsToBoardColumns(views: KanbanTask[], statusOrder: readonly string[]): KanbanColumn[] {
  const seen = new Set<string>()
  const byStatus = new Map<string, KanbanTask[]>()

  for (const view of views) {
    if (seen.has(view.id)) {
      continue
    }

    seen.add(view.id)

    const status = view.status || 'todo'
    byStatus.set(status, [...(byStatus.get(status) ?? []), view])
  }

  return [...byStatus.entries()]
    .sort(([a], [b]) => {
      const ia = statusOrder.indexOf(a)
      const ib = statusOrder.indexOf(b)

      return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib) || a.localeCompare(b)
    })
    .map(([name, tasks]) => ({ name, tasks }))
}

/** One worker observation line: state + reason, with the pid/workspace match
 *  surfaced when the adapter provided them. */
function ObservationLine({ observation }: { observation: WorkerObservation }) {
  const running = isRunningObservation(observation)

  return (
    <li className="flex items-baseline gap-2 text-[0.71rem]">
      <span
        className="inline-flex items-center gap-1 font-medium"
        style={{ color: running ? STATE_TONE.running : STATE_TONE.unknown }}
      >
        <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: running ? STATE_TONE.running : STATE_TONE.unknown }} />
        {observation.state ?? 'unknown'}
      </span>
      {observation.reason && <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)">{observation.reason}</span>}
    </li>
  )
}

/**
 * The drawer's worker-evidence panel. Reads the read-only /evidence/worker
 * aggregate for one card and renders the actual worker state, only when the
 * selected board is identity-aligned with the EVO database. A false or
 * unavailable alignment is a VISIBLE note (choose the EVO connection), never
 * a silent blank, and a failed context/worker query never hangs on Loading.
 */
export function WorkerEvidenceSection({ id }: { id: string }) {
  const slug = useResolvedBoardSlug()
  const contextQuery = useEvidenceContext(slug)
  const context = contextQuery.data

  const { data: envelope, isFetching, isError } = useQuery({
    queryKey: evidenceWorkerKey(slug, id),
    queryFn: () => fetchEvidenceWorker(slug, id),
    enabled: context?.aligned === true,
    refetchInterval: 15_000,
    retry: false
  })

  // Alignment query itself failed — say so, never a silent blank.
  if (contextQuery.isError) {
    return (
      <Section label="Worker evidence">
        <Callout title="Choose the EVO connection" tone={STATE_TONE.unavailable}>
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            Worker evidence could not be checked — the EVO identity check failed. Select the EVO
            connection to see worker state for this card.
          </p>
        </Callout>
      </Section>
    )
  }

  if (!context) {
    // Alignment still resolving: show nothing (the local drawer is intact).
    return null
  }

  if (!context.aligned) {
    // Local board (or a different server): no EVO evidence, and the mismatch
    // is stated rather than silenced. The local drawer stays fully usable.
    return (
      <Section label="Worker evidence">
        <Callout title="Choose the EVO connection" tone={STATE_TONE.unavailable}>
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            This board is not the EVO database, so worker evidence is unavailable here. Select the
            EVO connection to see live worker state for this card.
          </p>
        </Callout>
      </Section>
    )
  }

  if (isError) {
    // The worker query itself failed — a real error, never an infinite Loading.
    return (
      <Section label="Worker evidence">
        <Callout title={STATE_LABEL.unavailable} tone={STATE_TONE.unavailable}>
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            Worker evidence failed to load for this card. Retrying on the next refresh.
          </p>
        </Callout>
      </Section>
    )
  }

  if (!envelope) {
    return (
      <Section label="Worker evidence">
        <div className="flex items-center gap-2 text-[0.71rem] text-(--ui-text-quaternary)">
          {isFetching ? <Codicon name="loading" size="0.75rem" spinning /> : null}
          Loading evidence…
        </div>
      </Section>
    )
  }

  if (envelope.state !== 'PASS') {
    // Missing helper / malformed receipt / wrong host → visibly unavailable,
    // never a green empty state, and not red (a past resolved failure is not
    // an error now).
    return (
      <Section label="Worker evidence">
        <Callout title={STATE_LABEL.unavailable} tone={STATE_TONE.unavailable}>
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
            {envelope.reason ?? 'Evidence is unavailable for this card.'}
          </p>
        </Callout>
      </Section>
    )
  }

  const evidence = envelope.evidence
  const state = resolveWorkerState(evidence)
  const observations = evidence?.observations ?? []
  const completionRuns = evidence?.completion_runs ?? []

  return (
    <Section label="Worker evidence">
      <div className="flex items-center gap-2 text-[0.75rem]">
        <span className="size-2 rounded-full" style={{ backgroundColor: STATE_TONE[state] }} />
        <span className="font-medium" style={{ color: STATE_TONE[state] }}>
          {STATE_LABEL[state]}
        </span>
        {envelope.observed_at && (
          <span className="text-[0.625rem] text-(--ui-text-quaternary)">
            observed {new Date(envelope.observed_at * 1000).toLocaleTimeString()}
          </span>
        )}
      </div>
      {observations.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {observations.map((observation, index) => (
            <ObservationLine key={observation.task_id ?? index} observation={observation} />
          ))}
        </ul>
      ) : (
        <p className="text-[0.71rem] text-(--ui-text-quaternary)">
          No worker observation for this card — treated as unknown, not stopped.
        </p>
      )}
      {/* Actual result metadata from the worker aggregate — completion records
          carry the finished run's summary, surfaced as evidence, not as the
          parent-owned result field. */}
      {completionRuns.length > 0 && (
        <ul className="flex flex-col gap-1.5 pt-1">
          {completionRuns.map((run, index) => (
            <li className="flex flex-col gap-0.5 text-[0.71rem]" key={run.run_id ?? index}>
              {run.summary && (
                <p className="line-clamp-3 whitespace-pre-wrap text-(--ui-text-quaternary)">{run.summary}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {evidence?.limitation && (
        <p className="text-[0.625rem] leading-relaxed text-(--ui-text-quaternary)">{evidence.limitation}</p>
      )}
    </Section>
  )
}

/** Compact evidence badge for a card (board/column level). */
export function EvidenceStateBadge({ state }: { state: WorkerState }) {
  return (
    <span
      className="inline-flex items-center gap-1 text-[0.625rem] font-medium"
      style={{ color: STATE_TONE[state] }}
    >
      <span className="size-1.5 rounded-full" style={{ backgroundColor: STATE_TONE[state] }} />
      {STATE_LABEL[state]}
    </span>
  )
}

/**
 * Board-level worker-evidence badge, driven by ONE bounded snapshot per
 * aligned board (not the local /board plus a snapshot every poll). Shows the
 * snapshot's running count, plus unknown/missing-observation count and the
 * page's omitted count, so the board header states how much worker evidence
 * it actually holds. Renders nothing when the board is not EVO-aligned.
 */
export function BoardEvidenceBadge() {
  const slug = useResolvedBoardSlug()
  const { data: context, isError } = useEvidenceContext(slug)
  const aligned = context?.aligned === true
  const { data: snapshotEnvelope } = useEvidenceSnapshot(slug, aligned)

  if (isError || !aligned) {
    // Not EVO-aligned (or alignment unresolved): no EVO badge on the board.
    return null
  }

  if (!snapshotEnvelope || snapshotEnvelope.state !== 'PASS' || !snapshotEnvelope.evidence) {
    return null
  }

  const snapshot = snapshotEnvelope.evidence
  const { running, unknown } = resolveSnapshotWorkerStates(snapshot)
  const counts = snapshot.counts
  const omitted = typeof counts?.omitted === 'number' ? counts.omitted : 0

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)"
      title="Worker evidence from the EVO snapshot"
    >
      <EvidenceStateBadge state="running" />
      <span>{running}</span>
      {unknown > 0 && (
        <>
          <EvidenceStateBadge state="unknown" />
          <span>{unknown}</span>
        </>
      )}
      {omitted > 0 && <span className="text-(--ui-text-quaternary)">+{omitted} more</span>}
    </span>
  )
}

/** Re-export the alignment payload type for callers that branch on it. */
export type { EvidenceContext }
