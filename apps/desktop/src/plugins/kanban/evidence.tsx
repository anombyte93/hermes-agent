/**
 * Worker evidence + identity alignment for the kanban UI.
 *
 * This is the ONLY surface that reads the read-only /evidence/* bridge, which
 * always reads the physical EVO host's database. It never borrows a local
 * board's cards/actions/downloads merely because a slug matches: every query
 * here is gated on `/evidence/context` reporting that the selected board's
 * local DB IS the same EVO database. When alignment is false (or unresolved)
 * the original local UI renders unchanged, with no EVO badges or downloads.
 */

import { Codicon, Loader, useQuery, useValue } from '@hermes/plugin-sdk'

import {
  $boardSlug,
  evidenceContextKey,
  evidenceWorkerKey,
  fetchEvidenceContext,
  fetchEvidenceWorker
} from './api'
import { Callout, Section } from './ui'
import type { EvidenceContext, WorkerEvidenceData, WorkerObservation } from './types'

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
 * Reduce a /evidence/worker payload to one of the four honest states.
 *
 * Running is POSITIVE exact evidence only (a process is present); Stopped is
 * only the explicit full aggregate with `complete: true` and no
 * unknown/running; a missing or empty observation is UNKNOWN, never Stopped —
 * a fresh snapshot absence must not overwrite a stronger observation.
 */
export function resolveWorkerState(data: WorkerEvidenceData | null | undefined): WorkerState {
  if (!data) {
    return 'unknown'
  }

  const observations = Array.isArray(data.observations) ? data.observations : []
  const running = data.running === true || observations.some(obs => obs.process_present === true)

  if (running) {
    return 'running'
  }

  if (data.complete === true && data.unknown !== true) {
    return 'stopped'
  }

  return 'unknown'
}

/** Identity alignment for the selected board (short-lived, retry-off). */
export function useEvidenceContext(slug: string) {
  return useQuery({
    queryKey: evidenceContextKey(slug),
    queryFn: fetchEvidenceContext,
    staleTime: 15_000,
    retry: false
  })
}

/** One worker observation line: state + reason, with the pid/workspace match
 *  surfaced when the adapter provided them. */
function ObservationLine({ observation }: { observation: WorkerObservation }) {
  const state = observation.state ?? 'unknown'
  const present = observation.process_present === true

  return (
    <li className="flex items-baseline gap-2 text-[0.71rem]">
      <span
        className="inline-flex items-center gap-1 font-medium"
        style={{ color: present ? STATE_TONE.running : STATE_TONE.unknown }}
      >
        <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: present ? STATE_TONE.running : STATE_TONE.unknown }} />
        {state}
      </span>
      {observation.reason && <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)">{observation.reason}</span>}
    </li>
  )
}

/**
 * The drawer's worker-evidence panel. Reads the read-only /evidence/worker
 * aggregate for one card and renders the actual worker state, only when the
 * selected board is identity-aligned with the EVO database.
 */
export function WorkerEvidenceSection({ id }: { id: string }) {
  const slug = useValue($boardSlug)
  const { data: context } = useEvidenceContext(slug)

  const { data: envelope, isFetching } = useQuery({
    queryKey: evidenceWorkerKey(slug, id),
    queryFn: () => fetchEvidenceWorker(id),
    enabled: context?.aligned === true,
    refetchInterval: 15_000
  })

  if (!context) {
    // Alignment still resolving: show nothing (the local drawer is intact).
    return null
  }

  if (!context.aligned) {
    // Local board (or a different server): no EVO evidence, no EVO badges.
    return null
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

/** Re-export the alignment payload type for callers that branch on it. */
export type { EvidenceContext }
