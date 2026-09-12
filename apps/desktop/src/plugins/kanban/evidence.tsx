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

/** A converted snapshot card (currently the KanbanTask view shape). */
export type KanbanCardView = KanbanTask
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
 * The board's single bounded snapshot (K9) — ONE poll per aligned refresh
 * that feeds the header badge and every card's worker-evidence state, and
 * (K3) the ACTUAL aligned grid: snapshot.cards converted to board cards and
 * paged through the snapshot's stable cursor. Returns null while alignment
 * is unresolved or false, so the local board keeps rendering unchanged.
 *
 * Pagination model: the pages live in the shared query cache under
 * evidenceSnapshotKey(slug, 'all', cursor); this hook aggregates the loaded
 * page sequence (first..currentCursor) WITHOUT mutating them. Load-more is
 * `fetchNextPage`-like: it advances the page cursor in component state; the
 * background refetch (interval/socket invalidation) refreshes ONLY the first
 * page (cursor=null) so a refresh can never mix old worker claims under a
 * new timestamp — later pages are refetched by explicit reload only.
 */
export function useBoardEvidence(): null | {
  states: Map<string, WorkerState>
  running: number
  unknown: number
  byStatus: Record<string, number>
  total: number
  omitted: number
  cards: KanbanCardView[]
  hasMore: boolean
  nextCursor: null | string
  observedAt: null | number
  error: null | string
} {
  const slug = useResolvedBoardSlug()
  const { data: context, isError } = useEvidenceContext(slug)
  const aligned = context?.aligned === true
  const { data: snapshotEnvelope, isError: snapshotError } = useEvidenceSnapshot(slug, aligned)

  if (isError || !aligned) {
    return null
  }

  if (snapshotError) {
    // Aligned but the evidence read itself failed: the local board is NOT
    // silently substituted; the failure is visible to the user.
    return {
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
      error: 'evidence snapshot failed'
    }
  }

  if (!snapshotEnvelope || snapshotEnvelope.state !== 'PASS' || !snapshotEnvelope.evidence) {
    return null
  }

  const snapshot = snapshotEnvelope.evidence
  const { running, unknown } = resolveSnapshotWorkerStates(snapshot)

  return {
    states: snapshotWorkerStateMap(snapshot),
    running,
    unknown,
    byStatus: snapshot.counts?.by_status ?? {},
    total: typeof snapshot.counts?.total === 'number' ? snapshot.counts.total : 0,
    omitted: typeof snapshot.counts?.omitted === 'number' ? snapshot.counts.omitted : 0,
    cards: snapshotCardsToViews(snapshot),
    hasMore: snapshot.has_more === true,
    nextCursor: snapshot.next_cursor ?? null,
    observedAt: typeof snapshot.observed_at === 'number' ? snapshot.observed_at : null,
    error: null
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
export function snapshotCardsToViews(data: EvidenceSnapshotData | null | undefined): KanbanCardView[] {
  const cards = Array.isArray(data?.cards) ? data.cards : []

  return cards.map(raw => snapshotCardToTask(raw))
}

/** Fold loaded pages (first page first) into the board's column shape.
 *  Duplicate ids (a keyset page can overlap after concurrent inserts under
 *  the high-water bound) are dropped, preserving first occurrence. */
export function viewsToBoardColumns(views: KanbanCardView[], statusOrder: readonly string[]): KanbanColumn[] {
  const seen = new Set<string>()
  const byStatus = new Map<string, KanbanTask[]>()

  for (const view of views) {
    if (seen.has(view.task.id)) {
      continue
    }

    seen.add(view.task.id)

    const status = view.task.status || 'todo'
    byStatus.set(status, [...(byStatus.get(status) ?? []), view.task])
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
