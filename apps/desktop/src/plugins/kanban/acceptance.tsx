/**
 * R4 / R6 — drawer acceptance comparison + reviewer-packet export.
 *
 * R4  Compare the current and previous run acceptance receipts for one card.
 *     Two explicit run ids in, one bounded GET /evidence/acceptance-compare
 *     out; each check's change is reported (reverified|regressed|new|unproved)
 *     with an honest UNKNOWN when a historical receipt is missing.
 * R6  Export a compact, allowlisted reviewer packet as a REAL Blob download
 *     (GET /evidence/reviewer-packet), never "copy text pretending export".
 *
 * Both are read-only: no dispatch, release, or config write. Rendered only on
 * the EVO-aligned drawer (the parent passes `slug` already resolved).
 */

import { Button, Codicon, Input, Loader, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  acceptanceCompareKey,
  fetchAcceptanceCompare,
  fetchReviewerPacket
} from './api'
import { triggerDownload } from './drawer-evidence'
import type { EvidenceEnvelope } from './types'
import { errText, Section } from './ui'
import type { AcceptanceCompareData } from './workflow-api'

const CHANGE_TONE: Record<string, string> = {
  reverified: '#34d399',
  regressed: '#f87171',
  new: '#60a5fa',
  unproved: '#fbbf24'
}

const changeTone = (change: string | undefined): string => CHANGE_TONE[change ?? ''] ?? 'var(--ui-text-tertiary)'

/** R4 — current vs previous run acceptance comparison. Two numeric run ids in,
 *  an explicit compare click, then the per-check change verdict. */
export function AcceptanceCompareSection({ card, slug }: { card: string; slug: string }) {
  const [current, setCurrent] = useState('')
  const [previous, setPrevious] = useState('')
  const [currentId, setCurrentId] = useState<null | number>(null)
  const [previousId, setPreviousId] = useState<null | number>(null)
  const [enabled, setEnabled] = useState(false)

  const compare = useQuery({
    queryKey: acceptanceCompareKey(slug, card, currentId ?? 0, previousId ?? 0),
    queryFn: () => fetchAcceptanceCompare(slug, card, currentId!, previousId!),
    enabled: enabled && currentId != null && previousId != null && currentId !== previousId,
    retry: false
  })

  const envelope = compare.data as EvidenceEnvelope<AcceptanceCompareData> | undefined
  const data = envelope?.state === 'PASS' ? envelope.evidence : null

  const run = () => {
    const cur = Number(current)
    const prev = Number(previous)

    if (!Number.isFinite(cur) || !Number.isFinite(prev) || cur === prev) {
      return
    }

    setCurrentId(cur)
    setPreviousId(prev)
    setEnabled(true)
  }

  return (
    <Section label="Acceptance comparison">
      <div className="flex items-center gap-2">
        <Input
          aria-label="Current run id"
          onChange={event => {
            setCurrent(event.target.value)
            setEnabled(false)
          }}
          placeholder="current run id"
          value={current}
        />
        <Input
          aria-label="Previous run id"
          onChange={event => {
            setPrevious(event.target.value)
            setEnabled(false)
          }}
          placeholder="previous run id"
          value={previous}
        />
        <Button disabled={!current.trim() || !previous.trim() || compare.isFetching} onClick={run} size="xs" variant="outline">
          {compare.isFetching ? 'Comparing…' : 'Compare'}
        </Button>
      </div>

      {compare.isError && <span className="text-[0.6875rem] text-destructive">{errText(compare.error)}</span>}
      {envelope && envelope.state !== 'PASS' && (
        <span className="text-[0.6875rem] text-amber-500">{envelope.reason ?? 'Acceptance comparison unavailable — a historical receipt may be missing.'}</span>
      )}

      {data && (
        <div className="flex flex-col gap-1">
          {(data.checks ?? []).map(check => {
            const origin = check.source === 'parent' ? 'parent attestation' : check.source === 'machine' ? 'machine validation' : 'unknown origin'

            return (
              <div className="flex items-baseline gap-2 text-[0.6875rem]" key={check.name}>
                <span className="size-1.5 shrink-0 rounded-full" style={{ backgroundColor: changeTone(check.change) }} />
                <span className="min-w-0 font-medium text-(--ui-text-secondary)">{check.name}</span>
                <span className="shrink-0 font-medium" style={{ color: changeTone(check.change) }}>
                  {check.change}
                </span>
                <span className="min-w-0 truncate text-[0.625rem] text-(--ui-text-quaternary)">
                  {check.previous} → {check.current}
                </span>
                <span
                  className="shrink-0 text-[0.625rem]"
                  style={{ color: check.source === 'unknown' ? '#fbbf24' : 'var(--ui-text-quaternary)' }}
                  title={origin}
                >
                  {origin}
                </span>
              </div>
            )
          })}
          {(data.limitations ?? []).length > 0 && (
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">{data.limitations!.join(' ')}</span>
          )}
          {(!data.checks || data.checks.length === 0) && (
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">No comparable acceptance checks.</span>
          )}
        </div>
      )}
    </Section>
  )
}

/** R6 — download the compact reviewer packet as a real JSON Blob. */
export function ReviewerPacketSection({ card, slug }: { card: string; slug: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)

  const exportPacket = async () => {
    setBusy(true)
    setError(null)

    try {
      const envelope = await fetchReviewerPacket(slug, card)

      if (envelope.state !== 'PASS' || !envelope.evidence) {
        setError(envelope.reason ?? 'Reviewer packet unavailable for this card.')

        return
      }

      // A REAL Blob download — the bounded allowlisted packet serialised to
      // JSON and handed to the OS download boundary, never clipboard text.
      const blob = new Blob([JSON.stringify(envelope.evidence, null, 2)], { type: 'application/json' })
      triggerDownload(blob, `reviewer-packet-${card}.json`)
    } catch (err) {
      setError(errText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Section label="Reviewer packet">
      <div className="flex items-center gap-2">
        <Button disabled={busy} onClick={() => void exportPacket()} size="xs" variant="outline">
          <Codicon name={busy ? 'sync' : 'download'} size="0.75rem" spinning={busy} />
          Export packet (JSON)
        </Button>
        {busy && <Loader type="lemniscate-bloom" />}
      </div>
      {error && <span className="text-[0.6875rem] text-destructive">{error}</span>}
      <span className="text-[0.625rem] text-(--ui-text-quaternary)">Bounded allowlisted fields — no raw body, result, log, comments, argv, env, or stored paths.</span>
    </Section>
  )
}
