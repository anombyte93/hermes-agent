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
 * The continuation fingerprint is an optimistic-concurrency check that binds
 * the resolved commission (workspace/profile/provider/model/creator/title/
 * max_runtime_minutes) plus the entered checks and note. `continueCard`
 * therefore sends the EXACT commission + checks + note that came back in the
 * accepted `draft.evidence` — it never re-derives from the current inputs.
 *
 * CardWorkflowPanel is exported for the parent to insert into the real
 * TaskDrawer once the separate drawer worker returns. BoardWorkflowPanel is
 * mounted inside KanbanBoardPage (see board.tsx).
 */

import { Button, Codicon, Input, Loader, Textarea, useMutation, useQuery, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import {
  $boardSlug,
  BOARDS_KEY,
  type ContinuationInput,
  continueCard,
  draftContinuation,
  fetchAttentionQueue,
  fetchBoards,
  fetchChanges,
  fetchTimeline,
  holdCard,
  runReadiness
} from './api'
import { $openCard } from './completion-notify'
import { useEvidenceContext } from './evidence'
import type { EvidenceEnvelope } from './types'
import type {
  AttentionData,
  ContinuationDraft,
  ContinueReceipt,
  HoldReceipt,
  ReadinessReceipt,
  TimelineInterval
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

/** Build the continue body from the ACCEPTED draft receipt: the exact resolved
 *  commission + checks + note the fingerprint bound. Never re-derived from the
 *  current inputs, and never omits the commission (the fingerprint would not
 *  match). */
function continueBodyFromDraft(card: string, draft: ContinuationDraft, fingerprint: string): ContinuationInput & { fingerprint: string } {
  const commission = draft.commission ?? {}
  const str = (v: unknown): string | undefined => (typeof v === 'string' && v.length > 0 ? v : undefined)
  const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

  return {
    card,
    passed_checks: Array.isArray(draft.passed_checks) ? draft.passed_checks : [],
    remaining_checks: Array.isArray(draft.remaining_checks) ? draft.remaining_checks : [],
    verification_note: typeof draft.verification_note === 'string' ? draft.verification_note : '',
    workspace: str(commission.workspace),
    profile: str(commission.profile),
    provider: str(commission.provider),
    model: str(commission.model),
    creator: str(commission.creator),
    title: str(commission.title),
    max_runtime_minutes: num(commission.max_runtime_minutes),
    fingerprint
  }
}

// ── CardWorkflowPanel ────────────────────────────────────────────────────────

type RemainingRow = { check: string; evidence: string; acceptance: string }

export function CardWorkflowPanel({ card, slug }: { card: string; slug: string }) {
  const [checkModel, setCheckModel] = useState(false)
  const [passedText, setPassedText] = useState('')
  const [remaining, setRemaining] = useState<RemainingRow[]>([])
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState<EvidenceEnvelope<ContinuationDraft> | null>(null)
  const [continued, setContinued] = useState<EvidenceEnvelope<ContinueReceipt> | null>(null)
  const [holdReason, setHoldReason] = useState('')

  // Generation counter: bumped on any card/board change so a late mutation
  // success (fired for a previous card) is ignored, never painted.
  const genRef = useRef(0)

  const [readinessResult, setReadinessResult] = useState<EvidenceEnvelope<ReadinessReceipt> | null>(null)
  const [holdResult, setHoldResult] = useState<EvidenceEnvelope<HoldReceipt> | null>(null)

  const readiness = useMutation({
    mutationFn: () => runReadiness(slug, card, checkModel),
    onSuccess: (data, gen: number) => {
      if (gen === genRef.current) {
        setReadinessResult(data)
      }
    }
  })

  const draftMut = useMutation({
    mutationFn: () =>
      draftContinuation(slug, {
        card,
        passed_checks: parsePassedChecks(passedText),
        remaining_checks: remaining.map(({ check, evidence, acceptance }) => ({ check, evidence, acceptance })),
        verification_note: note
      }),
    onSuccess: (data, gen: number) => {
      if (gen === genRef.current) {
        setDraft(data)
      }
    }
  })

  const continueMut = useMutation({
    mutationFn: ({ fingerprint, gen }: { fingerprint: string; gen: number }) => {
      const receipt = draftReceipt(draft)

      if (!receipt) {
        return Promise.reject(new Error('no accepted draft'))
      }

      return continueCard(slug, continueBodyFromDraft(card, receipt, fingerprint))
    },
    onSuccess: (data, vars: { fingerprint: string; gen: number }) => {
      if (vars.gen === genRef.current) {
        setContinued(data)
      }
    }
  })

  const holdMut = useMutation({
    mutationFn: () => holdCard(slug, card, holdReason.trim() || 'held for review'),
    onSuccess: (data, gen: number) => {
      if (gen === genRef.current) {
        setHoldResult(data)
      }
    }
  })

  // Reset every workflow datum when the card or board changes; bump the
  // generation so in-flight mutations for the previous card are dropped.
  // genRef is a request token (generation counter), not an atom mirror.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    genRef.current += 1
    setCheckModel(false)
    setPassedText('')
    setRemaining([])
    setNote('')
    setDraft(null)
    setContinued(null)
    setReadinessResult(null)
    setHoldResult(null)
    setHoldReason('')
    readiness.reset()
    draftMut.reset()
    continueMut.reset()
    holdMut.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [card, slug])

  // Editing any draft input invalidates the accepted draft + continue result:
  // the fingerprint now describes a different intent.
  const invalidateDraft = () => {
    setDraft(null)
    setContinued(null)
    continueMut.reset()
  }

  const setPassedAndInvalidate = (value: string) => {
    setPassedText(value)
    invalidateDraft()
  }

  const setRemainingAndInvalidate = (rows: RemainingRow[]) => {
    setRemaining(rows)
    invalidateDraft()
  }

  const setNoteAndInvalidate = (value: string) => {
    setNote(value)
    invalidateDraft()
  }

  const readinessEnvelope = readinessResult
  const readinessReceipt = readinessEnvelope?.evidence
  const draftReceiptVal = draftReceipt(draft)
  const continuePayload = draftReceiptVal?.fingerprint

  const remainingComplete = remaining.length === 0 || remaining.every(r => r.check.trim() && r.evidence.trim() && r.acceptance.trim())
  const remainingPartial = remaining.some(r => (r.check.trim() || r.evidence.trim() || r.acceptance.trim()) && !(r.check.trim() && r.evidence.trim() && r.acceptance.trim()))

  return (
    <div className="flex flex-col gap-4 py-1 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">
      <section className="flex flex-col gap-2">
        <SectionLabel>Readiness</SectionLabel>
        <label className="flex cursor-pointer items-center gap-2 text-[0.75rem]">
          <input checked={checkModel} onChange={event => setCheckModel(event.target.checked)} type="checkbox" />
          Check the exact provider and model
        </label>
        <div>
          <Button disabled={readiness.isPending} onClick={() => readiness.mutate(genRef.current)} size="sm" variant="outline">
            {readiness.isPending ? 'Checking…' : 'Check readiness'}
          </Button>
        </div>

        {readinessEnvelope && readinessEnvelope.state !== 'PASS' && (
          <span className="text-destructive">
            Readiness {readinessEnvelope.state.toLowerCase()}: {readinessEnvelope.reason ?? 'unavailable'}
            {readinessEnvelope.remedy ? ` — ${readinessEnvelope.remedy}` : ''}
          </span>
        )}
        {/* Checks render even when the outer envelope is FAIL/UNKNOWN: a
            permission or model failure is exactly when the operator needs the
            per-check detail. */}
        {readinessReceipt?.checks?.length ? <ReadinessChecks receipt={readinessReceipt} /> : null}
        {readiness.isError && <span className="text-destructive">Readiness could not be checked.</span>}
      </section>

      <section className="flex flex-col gap-2">
        <SectionLabel>Continuation draft</SectionLabel>
        <Field label="Passed checks (one per line)">
          <Textarea onChange={event => setPassedAndInvalidate(event.target.value)} placeholder="Parent source validation passed" value={passedText} />
        </Field>
        <Field label="Remaining checks">
          {remaining.map((row, index) => (
            <div className="flex items-start gap-1" key={index}>
              <Input
                onChange={event => setRemainingAndInvalidate(withRow(remaining, index, 'check', event.target.value))}
                placeholder="check"
                value={row.check}
              />
              <Input
                onChange={event => setRemainingAndInvalidate(withRow(remaining, index, 'evidence', event.target.value))}
                placeholder="evidence"
                value={row.evidence}
              />
              <Input
                onChange={event => setRemainingAndInvalidate(withRow(remaining, index, 'acceptance', event.target.value))}
                placeholder="acceptance"
                value={row.acceptance}
              />
              <Button
                aria-label={`Remove remaining check ${index + 1}`}
                onClick={() => setRemainingAndInvalidate(remaining.filter((_, i) => i !== index))}
                size="icon-xs"
                variant="ghost"
              >
                <Codicon name="close" size="0.7rem" />
              </Button>
            </div>
          ))}
          <Button onClick={() => setRemainingAndInvalidate([...remaining, { check: '', evidence: '', acceptance: '' }])} size="sm" variant="outline">
            Add remaining check
          </Button>
          {remainingPartial && (
            <span className="text-amber-500">Each remaining check needs all three fields: check, evidence, and acceptance.</span>
          )}
        </Field>
        <Field label="Verification note">
          <Textarea onChange={event => setNoteAndInvalidate(event.target.value)} placeholder="What you verified, in your own words" value={note} />
        </Field>
        <div>
          <Button disabled={draftMut.isPending || !remainingComplete} onClick={() => draftMut.mutate(genRef.current)} size="sm" variant="outline">
            {draftMut.isPending ? 'Drafting…' : 'Draft continuation'}
          </Button>
        </div>

        {draft && draft.state !== 'PASS' && <span className="text-destructive">Draft unavailable: {draft.reason ?? 'read failed'}</span>}

        {draftReceiptVal && (
          <div className="flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2">
            <span className="font-mono text-[0.625rem] text-(--ui-text-quaternary)">{draftReceiptVal.fingerprint}</span>
            {draftReceiptVal.original && (
              <span className="text-[0.625rem] italic text-(--ui-text-quaternary)">
                Original{draftReceiptVal.original.status ? ` status ${draftReceiptVal.original.status}` : ''}
                {draftReceiptVal.original.assignee ? `, assignee ${draftReceiptVal.original.assignee}` : ''}:{' '}
                {draftReceiptVal.original.source ?? 'unverified source excerpt'}
              </span>
            )}
            {draftReceiptVal.worker && (
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">
                Worker verdict: {draftReceiptVal.worker.verdict ?? 'UNKNOWN'}
                {draftReceiptVal.worker.reason ? ` — ${draftReceiptVal.worker.reason}` : ''}
              </span>
            )}
            {(draftReceiptVal.passed_checks?.length || 0) > 0 && (
              <span className="text-[0.625rem] text-(--ui-text-tertiary)">Passed: {draftReceiptVal.passed_checks!.join(', ')}</span>
            )}
            {(draftReceiptVal.remaining_checks?.length || 0) > 0 && (
              <span className="text-[0.625rem] text-(--ui-text-tertiary)">
                Remaining: {draftReceiptVal.remaining_checks!.map(c => c.check).join(', ')}
              </span>
            )}
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <SectionLabel>Continue</SectionLabel>
        <Button
          disabled={continueMut.isPending || !continuePayload}
          onClick={() => continuePayload && continueMut.mutate({ fingerprint: continuePayload, gen: genRef.current })}
          size="sm"
        >
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
          <Button disabled={holdMut.isPending} onClick={() => holdMut.mutate(genRef.current)} size="sm" variant="outline">
            {holdMut.isPending ? 'Holding…' : 'Hold'}
          </Button>
        </div>
        {holdResult && holdResult.state === 'PASS' && (
          <span className="text-(--ui-text-tertiary)">Card held for review. History preserved; a live worker is not stopped.</span>
        )}
        {holdResult && holdResult.state !== 'PASS' && (
          <span className="text-destructive">{holdResult.reason ?? holdResult.evidence?.reason ?? 'Hold unavailable'}</span>
        )}
      </section>
    </div>
  )
}

function withRow(rows: RemainingRow[], index: number, key: keyof RemainingRow, value: string): RemainingRow[] {
  return rows.map((row, i) => (i === index ? { ...row, [key]: value } : row))
}

function draftReceipt(draft: EvidenceEnvelope<ContinuationDraft> | null): ContinuationDraft | null {
  return draft && draft.state === 'PASS' ? (draft.evidence ?? null) : null
}

/** Every readiness check, with the board permission as its own separate check
 *  (never folded into a readiness verdict). Permission alone is never
 *  readiness, and readiness never dispatches. */
function ReadinessChecks({ receipt }: { receipt: ReadinessReceipt }) {
  const checks = Array.isArray(receipt.checks) ? receipt.checks : []

  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-2">
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

  // Reset accumulated pages + cursor on a board switch so one board's cards
  // never render under another board's queue.
  useEffect(() => {
    setCursor(null)
    setPages([])
    setOpen(false)
  }, [slug])

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['kanban', 'workflow', 'attention', slug, cursor],
    queryFn: () => fetchAttentionQueue(slug, 50, cursor),
    enabled: open && slug !== '',
    retry: false
  })

  const last = data?.state === 'PASS' ? data.evidence : null
  const allCards = [...pages.flatMap(page => page.cards ?? []), ...(last?.cards ?? [])]

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
      {allCards.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {allCards.map(card => (
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
  const [cursor, setCursor] = useState<null | string>(null)

  useEffect(() => {
    setCursor(null)
    setOpen(false)
  }, [slug])

  const { data, isFetching, isError } = useQuery({
    queryKey: ['kanban', 'workflow', 'changes', slug, cursor],
    queryFn: () => fetchChanges(slug, 50, cursor),
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
          {/* Retained cursor: advancing pages re-read from the returned cursor,
              never a repeated null (baseline-now) read. */}
          {ev.has_more && (
            <Button disabled={isFetching} onClick={() => ev.next_cursor && setCursor(ev.next_cursor)} size="sm" variant="outline">
              Load more changes
            </Button>
          )}
        </>
      )}
    </section>
  )
}

function TimelineSection({ slug }: { slug: string }) {
  const [card, setCard] = useState('')
  const [requested, setRequested] = useState('')
  const [cursor, setCursor] = useState<null | string>(null)
  const [extraIntervals, setExtraIntervals] = useState<TimelineInterval[]>([])

  useEffect(() => {
    setCursor(null)
    setExtraIntervals([])
    setRequested('')
    setCard('')
  }, [slug])

  const { data, isFetching, isError } = useQuery({
    queryKey: ['kanban', 'workflow', 'timeline', slug, requested, cursor],
    queryFn: () => fetchTimeline(slug, requested, 50, cursor),
    enabled: requested !== '' && slug !== '',
    retry: false
  })

  const ev = data?.state === 'PASS' ? data.evidence : null
  const allIntervals = [...extraIntervals, ...(ev?.intervals ?? [])]

  const loadMore = () => {
    if (!ev?.next_cursor) {
      return
    }

    if (ev.intervals) {
      setExtraIntervals(prev => [...prev, ...ev.intervals!])
    }

    setCursor(ev.next_cursor)
  }

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
            {allIntervals.map((interval, index) => (
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
          {ev.has_more && (
            <Button disabled={isFetching} onClick={loadMore} size="sm" variant="outline">
              Load more
            </Button>
          )}
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
