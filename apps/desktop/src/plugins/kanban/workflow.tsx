/**
 * Workflow UI for the kanban board: the explicit-action readiness /
 * continuation / hold surface (CardWorkflowPanel) and the read-only attention
 * queue / changes / timeline surface (BoardWorkflowPanel).
 *
 * This is the React half of WORKFLOW-UI-CONTRACT.md, built against the final
 * WORKFLOW-API-CONTRACT.md envelope: every route returns the standard envelope
 * (state + evidence + reason/remedy); the root helper receipt is forwarded as
 * `evidence`. Every read is bounded and board-scoped through the explicit
 * slug; every write is an explicit user click (readiness/continuation/hold
 * never fire automatically), and NOTHING here dispatches, unblocks, or grants
 * mutation authority. Truthful states only: a FAIL/UNKNOWN envelope renders
 * its reason/remedy, never an empty success.
 *
 * CardWorkflowPanel is exported for the parent to insert into the real
 * TaskDrawer once the separate drawer worker returns. BoardWorkflowPanel is
 * mounted inside KanbanBoardPage (see board.tsx).
 */

import { Button, Codicon, Input, Loader, Textarea, useMutation, useQuery, useValue } from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $boardSlug,
  BOARDS_KEY,
  continueCard,
  draftContinuation,
  fetchAttentionQueue,
  fetchBoards,
  fetchChanges,
  fetchTimeline,
  holdCard,
  runReadiness,
  type ContinuationInput
} from './api'
import { $openCard } from './completion-notify'
import { useEvidenceContext } from './evidence'
import type { EvidenceEnvelope } from './types'
import type {
  AttentionData,
  ContinueReceipt,
  ContinuationDraft,
  ReadinessReceipt,
  RemainingCheck
} from './workflow-api'

// ── shared helpers ───────────────────────────────────────────────────────────

const STATE_TONE: Record<string, string> = {
  PASS: '#34d399',
  FAIL: '#f87171',
  UNKNOWN: '#fbbf24'
}

function stateTone(state: string | undefined): string {
  return STATE_TONE[state ?? ''] ?? 'var(--ui-text-tertiary)'
}

/** Resolve the selected board's real slug (blank atom = server current board). */
function useWorkflowSlug(): string {
  const slug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  return slug || boards?.current || ''
}

/** Parse a newline list of passed check strings (one per line). */
export function parsePassedChecks(text: string): string[] {
  return text
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean)
}

/** Parse a newline list of remaining checks, one `check :: evidence :: acceptance`
 *  per line. Malformed lines are skipped (a blank or partial row is never sent
 *  as a fake check). */
export function parseRemainingChecks(text: string): RemainingCheck[] {
  const out: RemainingCheck[] = []

  for (const line of text.split('\n')) {
    const parts = line.split('::').map(part => part.trim())

    if (parts.length !== 3 || parts.some(part => part === '')) {
      continue
    }

    out.push({ check: parts[0], evidence: parts[1], acceptance: parts[2] })
  }

  return out
}

function SectionLabel({ children }: { children: string }) {
  return <h3 className="text-[0.6875rem] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)">{children}</h3>
}

function StateDot({ state }: { state: string | undefined }) {
  return <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: stateTone(state) }} />
}

function Field({ children, label }: { children: React.ReactNode; label: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[0.625rem] text-(--ui-text-quaternary)">{label}</span>
      {children}
    </label>
  )
}

/** Alignment gate: workflow reads/writes only against the selected EVO board. */
function AlignmentGate({ slug, children }: { slug: string; children: React.ReactNode }) {
  const { data: context, isError } = useEvidenceContext(slug)

  if (isError) {
    return (
      <p className="text-[0.75rem] text-(--ui-text-secondary)">
        Workflow evidence could not be checked: the EVO identity check failed. Choose the EVO connection.
      </p>
    )
  }

  if (!context) {
    return null
  }

  if (!context.aligned) {
    return <p className="text-[0.75rem] text-(--ui-text-secondary)">Choose the EVO connection to see workflow evidence.</p>
  }

  return <>{children}</>
}

// ── CardWorkflowPanel ────────────────────────────────────────────────────────

export function CardWorkflowPanel({ card, slug }: { card: string; slug: string }) {
  const [checkModel, setCheckModel] = useState(false)
  const [passedText, setPassedText] = useState('')
  const [remainingText, setRemainingText] = useState('')
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState<EvidenceEnvelope<ContinuationDraft> | null>(null)
  const [continued, setContinued] = useState<EvidenceEnvelope<ContinueReceipt> | null>(null)
  const [holdReason, setHoldReason] = useState('')

  const readiness = useMutation({
    mutationFn: () => runReadiness(slug, card, checkModel)
  })

  const draftMut = useMutation({
    mutationFn: () =>
      draftContinuation(slug, {
        card,
        passed_checks: parsePassedChecks(passedText),
        remaining_checks: parseRemainingChecks(remainingText),
        verification_note: note
      }),
    onSuccess: setDraft
  })

  const continueMut = useMutation({
    mutationFn: (fingerprint: string) => {
      const input: ContinuationInput = {
        card,
        passed_checks: parsePassedChecks(passedText),
        remaining_checks: parseRemainingChecks(remainingText),
        verification_note: note
      }

      return continueCard(slug, { ...input, fingerprint })
    },
    onSuccess: setContinued
  })

  const holdMut = useMutation({
    mutationFn: () => holdCard(slug, card, holdReason.trim() || 'held for review')
  })

  const readinessReceipt = readiness.data?.state === 'PASS' ? readiness.data.evidence : null
  const draftReceipt = draft?.state === 'PASS' ? draft.evidence : null
  const continuePayload = draftReceipt?.fingerprint

  return (
    <div className="flex flex-col gap-4 py-1 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">
      <section className="flex flex-col gap-2">
        <SectionLabel>Readiness</SectionLabel>
        <label className="flex cursor-pointer items-center gap-2 text-[0.75rem]">
          <input checked={checkModel} onChange={event => setCheckModel(event.target.checked)} type="checkbox" />
          Check the exact provider and model
        </label>
        <div>
          <Button disabled={readiness.isPending} onClick={() => readiness.mutate()} size="sm" variant="outline">
            {readiness.isPending ? 'Checking…' : 'Check readiness'}
          </Button>
        </div>

        {readiness.data && readiness.data.state !== 'PASS' && (
          <span className="text-destructive">
            Readiness {readiness.data.state.toLowerCase()}: {readiness.data.reason ?? 'unavailable'}
            {readiness.data.remedy ? ` — ${readiness.data.remedy}` : ''}
          </span>
        )}
        {readinessReceipt && (
          <div className="flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2">
            <ReadinessChecks receipt={readinessReceipt} />
          </div>
        )}
        {readiness.isError && <span className="text-destructive">Readiness could not be checked.</span>}
      </section>

      <section className="flex flex-col gap-2">
        <SectionLabel>Continuation draft</SectionLabel>
        <Field label="Passed checks (one per line)">
          <Textarea onChange={event => setPassedText(event.target.value)} placeholder="Parent source validation passed" value={passedText} />
        </Field>
        <Field label="Remaining checks (check :: evidence :: acceptance, one per line)">
          <Textarea
            onChange={event => setRemainingText(event.target.value)}
            placeholder="Non-executing protocol acceptance :: actual transport to EVO :: create one held card"
            value={remainingText}
          />
        </Field>
        <Field label="Verification note">
          <Textarea onChange={event => setNote(event.target.value)} placeholder="What you verified, in your own words" value={note} />
        </Field>
        <div>
          <Button disabled={draftMut.isPending} onClick={() => draftMut.mutate()} size="sm" variant="outline">
            {draftMut.isPending ? 'Drafting…' : 'Draft continuation'}
          </Button>
        </div>

        {draft && draft.state !== 'PASS' && (
          <span className="text-destructive">Draft unavailable: {draft.reason ?? 'read failed'}</span>
        )}

        {draftReceipt && (
          <div className="flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2">
            <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{draftReceipt.fingerprint}</span>
            {draftReceipt.original && (
              <span className="text-[0.625rem] italic text-(--ui-text-quaternary)">
                Original{draftReceipt.original.status ? ` status ${draftReceipt.original.status}` : ''}
                {draftReceipt.original.assignee ? `, assignee ${draftReceipt.original.assignee}` : ''}:{' '}
                {draftReceipt.original.source ?? 'unverified source excerpt'}
              </span>
            )}
            {draftReceipt.worker && (
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                Worker verdict: {draftReceipt.worker.verdict ?? 'UNKNOWN'}
                {draftReceipt.worker.reason ? ` — ${draftReceipt.worker.reason}` : ''}
              </span>
            )}
            {(draftReceipt.passed_checks?.length || 0) > 0 && (
              <span className="text-[0.625rem] text-(--ui-text-tertiary)">Passed: {draftReceipt.passed_checks!.join(', ')}</span>
            )}
            {(draftReceipt.remaining_checks?.length || 0) > 0 && (
              <span className="text-[0.625rem] text-(--ui-text-tertiary)">
                Remaining: {draftReceipt.remaining_checks!.map(c => c.check).join(', ')}
              </span>
            )}
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <SectionLabel>Continue</SectionLabel>
        <Button disabled={continueMut.isPending || !continuePayload} onClick={() => continuePayload && continueMut.mutate(continuePayload)} size="sm">
          {continueMut.isPending ? 'Creating…' : 'Create held continuation card'}
        </Button>

        {continued && <ContinueResult result={continued} />}
        {continueMut.isError && <span className="text-destructive">Continuation failed.</span>}
      </section>

      <section className="flex flex-col gap-2">
        <SectionLabel>Hold for review</SectionLabel>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Field label="Reason">
              <Input onChange={event => setHoldReason(event.target.value)} placeholder="Why this card needs review" value={holdReason} />
            </Field>
          </div>
          <Button disabled={holdMut.isPending} onClick={() => holdMut.mutate()} size="sm" variant="outline">
            {holdMut.isPending ? 'Holding…' : 'Hold'}
          </Button>
        </div>
        {holdMut.data && holdMut.data.state === 'PASS' && (
          <span className="text-(--ui-text-tertiary)">Card held for review. History preserved; a live worker is not stopped.</span>
        )}
        {holdMut.data && holdMut.data.state !== 'PASS' && (
          <span className="text-destructive">{holdMut.data.reason ?? holdMut.data.evidence?.reason ?? 'Hold unavailable'}</span>
        )}
      </section>
    </div>
  )
}

/** Every readiness check, with the board permission as its own separate check
 *  (never folded into a readiness verdict). Permission alone is never
 *  readiness, and readiness never dispatches. */
function ReadinessChecks({ receipt }: { receipt: ReadinessReceipt }) {
  const checks = Array.isArray(receipt.checks) ? receipt.checks : []

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <StateDot state={receipt.state} />
        <span className="font-medium text-foreground">{receipt.ready_to_release ? 'Ready to release' : 'Not ready to release'}</span>
      </div>
      {checks.map(check => (
        <div className="flex items-baseline gap-2" key={check.name}>
          <StateDot state={check.state} />
          <span className="min-w-0 font-medium" style={{ color: stateTone(check.state) }}>
            {check.name}
            {check.mutation_authorized === false ? ' (read-only)' : ''}
          </span>
          {check.reason && <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)">{check.reason}</span>}
        </div>
      ))}
      {receipt.reason && <span className="text-[0.625rem] text-(--ui-text-quaternary)">{receipt.reason}</span>}
    </div>
  )
}

/** A continue envelope: a held card is shown, never dispatched. A stale
 *  fingerprint asks to redraft; an UNKNOWN result asks to inspect before any
 *  retry. */
function ContinueResult({ result }: { result: EvidenceEnvelope<ContinueReceipt> }) {
  const reason = (result.reason ?? '').toLowerCase()

  if (result.state === 'FAIL' && reason.includes('fingerprint')) {
    return <span className="text-amber-500">The card changed since the draft. Draft again to continue.</span>
  }

  if (result.state === 'UNKNOWN') {
    return <span className="text-amber-500">Continuation result is unknown. Inspect the card before retrying.</span>
  }

  const receipt = result.evidence

  if (result.state === 'PASS' && receipt?.new_card) {
    return (
      <span className="text-(--ui-text-secondary)">
        New held card <span className="font-mono">{receipt.new_card}</span> ({receipt.new_card_status ?? 'blocked'}, assignee{' '}
        {receipt.new_card_assignee ?? 'none'}). Held, not dispatched. Open it to review.
      </span>
    )
  }

  return <span className="text-destructive">{result.reason ?? 'Continuation did not produce a card.'}</span>
}

// ── BoardWorkflowPanel ───────────────────────────────────────────────────────

export function BoardWorkflowPanel() {
  const slug = useWorkflowSlug()

  return (
    <div className="flex flex-col gap-3 border-t border-(--ui-stroke-secondary) px-4 py-2">
      <AlignmentGate slug={slug}>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <AttentionSection slug={slug} />
          <ChangesSection slug={slug} />
          <TimelineSection slug={slug} />
        </div>
      </AlignmentGate>
    </div>
  )
}

function AttentionSection({ slug }: { slug: string }) {
  const [open, setOpen] = useState(false)
  const [cursor, setCursor] = useState<null | string>(null)
  const [pages, setPages] = useState<AttentionData[]>([])

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['kanban', 'workflow', 'attention', slug, cursor],
    queryFn: () => fetchAttentionQueue(slug, 50, cursor),
    enabled: open && slug !== '',
    retry: false
  })

  const last = data?.state === 'PASS' ? data.evidence : null

  const loadMore = () => {
    if (!last?.next_cursor) {
      return
    }

    if (last) {
      setPages(prev => [...prev, last])
    }

    setCursor(last.next_cursor)
  }

  if (!open) {
    return (
      <section className="flex flex-col gap-2">
        <SectionLabel>Attention</SectionLabel>
        <Button onClick={() => setOpen(true)} size="sm" variant="outline">
          <Codicon name="warning" size="0.8rem" />
          Show attention queue
        </Button>
      </section>
    )
  }

  return (
    <section className="flex flex-col gap-2">
      <SectionLabel>Attention</SectionLabel>
      {isFetching && <Loader type="lemniscate-bloom" />}
      {isError && <span className="text-destructive">Attention queue unavailable: {(error as Error)?.message ?? 'read failed'}</span>}
      {data && data.state !== 'PASS' && <span className="text-destructive">Attention unavailable: {data.reason ?? 'read failed'}</span>}
      {last && (
        <ul className="flex flex-col gap-1.5">
          {(last.cards ?? []).map(card => (
            <li key={card.id}>
              <button
                className="flex w-full flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) p-2 text-left hover:bg-primary/[0.06]"
                onClick={() => $openCard.set({ board: slug, card: card.id })}
                type="button"
              >
                <span className="flex items-center gap-2 text-[0.75rem] font-medium text-foreground">
                  <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: stateTone(card.status) }} />
                  <span className="truncate">{card.title || card.id}</span>
                </span>
                <span className="flex items-center gap-2 text-[0.625rem] text-(--ui-text-quaternary)">
                  <span className="font-mono">{card.id}</span>
                  {card.next_action && <span>{card.next_action}</span>}
                  {card.operator_authority_needed === true && <span className="text-amber-500">operator authority needed</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {last && (last.omitted ?? 0) > 0 && (
        <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">+{last.omitted} omitted</span>
      )}
      {last && last.has_more && (
        <Button disabled={isFetching} onClick={loadMore} size="sm" variant="outline">
          Load more
        </Button>
      )}
    </section>
  )
}

function ChangesSection({ slug }: { slug: string }) {
  const [open, setOpen] = useState(false)

  const { data, isFetching, isError } = useQuery({
    queryKey: ['kanban', 'workflow', 'changes', slug],
    queryFn: () => fetchChanges(slug, 50, null),
    enabled: open && slug !== '',
    retry: false
  })

  if (!open) {
    return (
      <section className="flex flex-col gap-2">
        <SectionLabel>Changes</SectionLabel>
        <Button onClick={() => setOpen(true)} size="sm" variant="outline">
          Show changes
        </Button>
      </section>
    )
  }

  const ev = data?.state === 'PASS' ? data.evidence : null

  return (
    <section className="flex flex-col gap-2">
      <SectionLabel>Changes</SectionLabel>
      {isFetching && <Loader type="lemniscate-bloom" />}
      {isError && <span className="text-destructive">Changes unavailable</span>}
      {data && data.state !== 'PASS' && <span className="text-destructive">{data.reason ?? 'changes unavailable'}</span>}
      {ev && (
        <>
          <span className="text-[0.625rem] text-(--ui-text-quaternary)">
            {ev.first_read_policy ?? 'baseline-now'} · fresh {ev.observed_at ? new Date(ev.observed_at * 1000).toLocaleTimeString() : ''}
          </span>
          <ul className="flex flex-col gap-1">
            {(ev.events ?? []).map((event, index) => (
              <li className="flex items-center gap-2 text-[0.6875rem]" key={event.id ?? index}>
                <span className="min-w-0 truncate text-(--ui-text-secondary)">{event.kind ?? 'event'}</span>
                {event.task_id && <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{event.task_id}</span>}
              </li>
            ))}
          </ul>
          {ev.incomplete && <span className="text-[0.625rem] text-amber-500">possible gap in change history</span>}
        </>
      )}
    </section>
  )
}

function TimelineSection({ slug }: { slug: string }) {
  const [card, setCard] = useState('')
  const [requested, setRequested] = useState('')

  const { data, isFetching, isError } = useQuery({
    queryKey: ['kanban', 'workflow', 'timeline', slug, requested],
    queryFn: () => fetchTimeline(slug, requested, 50, null),
    enabled: requested !== '' && slug !== '',
    retry: false
  })

  const ev = data?.state === 'PASS' ? data.evidence : null

  return (
    <section className="flex flex-col gap-2">
      <SectionLabel>Timeline</SectionLabel>
      <div className="flex gap-2">
        <Input onChange={event => setCard(event.target.value)} placeholder="Card id" value={card} />
        <Button disabled={!card.trim()} onClick={() => setRequested(card.trim())} size="sm" variant="outline">
          Show
        </Button>
      </div>
      {isFetching && <Loader type="lemniscate-bloom" />}
      {isError && <span className="text-destructive">Timeline unavailable</span>}
      {data && data.state !== 'PASS' && <span className="text-destructive">{data.reason ?? 'timeline unavailable'}</span>}
      {ev && (
        <>
          <ul className="flex flex-col gap-1">
            {(ev.intervals ?? []).map((interval, index) => (
              <li className="flex items-center gap-2 text-[0.6875rem]" key={index}>
                <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: timelineTone(interval.kind) }} />
                <span className="capitalize text-(--ui-text-secondary)">{interval.kind}</span>
                {typeof interval.duration_seconds === 'number' && (
                  <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{interval.duration_seconds}s wall-clock</span>
                )}
              </li>
            ))}
          </ul>
          {Array.isArray(ev.coverage?.gaps) && ev.coverage!.gaps!.length > 0 && (
            <span className="text-[0.625rem] text-amber-500">gaps in the covered window</span>
          )}
          {ev.incomplete && <span className="text-[0.625rem] text-(--ui-text-quaternary)">bounded window; not full card history</span>}
        </>
      )}
    </section>
  )
}

function timelineTone(kind: string): string {
  return (
    {
      execution: '#34d399',
      blocked: '#f87171',
      review: '#fbbf24',
      unknown: '#a78bfa'
    }[kind] ?? 'var(--ui-text-tertiary)'
  )
}
