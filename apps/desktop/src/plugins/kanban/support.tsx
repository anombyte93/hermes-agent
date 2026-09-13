/**
 * R1 / R2 / R7 — the concise expandable support panel.
 *
 * R1  Release identity: frontend (the running desktop build's own app version,
 *     read live from the loaded build metadata via `window.hermesDesktop.
 *     getVersion()` — NEVER a hardcoded SHA), backend, and adapter identity,
 *     from GET /evidence/releases. Absent or invalid identity is UNKNOWN.
 * R2  Browser readiness: reachable / authenticated / board_readable rendered
 *     as THREE separate facts (never inferred from a 200). 401/403/login-HTML/
 *     malformed are classified by the backend and surfaced from reason/remedy.
 * R7  Refresh timing + checked-vs-skipped process checks, from the SAME bounded
 *     snapshot the board renders (one shared query key, no second poll).
 *
 * This is a read-only surface: no endpoint here dispatches, releases, mutates,
 * or changes config. Everything is gated behind the panel being open, and
 * every read is board-scoped through an explicit slug.
 */

import { Codicon, useQuery, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'

import {
  $boardSlug,
  BOARDS_KEY,
  browserReadinessKey,
  evidenceReleasesKey,
  fetchBoards,
  fetchBrowserReadiness,
  fetchEvidenceReleases
} from './api'
import { useEvidenceContext, useEvidenceSnapshot } from './evidence'
import type { EvidenceEnvelope, EvidenceSnapshotData } from './types'
import { Callout, Section } from './ui'
import type { BrowserReadinessEvidence, ReleasesData } from './workflow-api'

const STATE_TONE: Record<string, string> = {
  PASS: '#34d399',
  FAIL: '#f87171',
  UNKNOWN: '#fbbf24'
}

const tone = (state: string | undefined): string => STATE_TONE[state ?? ''] ?? 'var(--ui-text-tertiary)'

/** The running desktop build's own version, read from the loaded build
 *  metadata. Absent bridge / invalid value → UNKNOWN, never invented. */
interface FrontendStamp {
  appVersion: null | string
  bundleOutOfSync: null | boolean
}

function useFrontendStamp(): FrontendStamp {
  const [stamp, setStamp] = useState<FrontendStamp>({ appVersion: null, bundleOutOfSync: null })

  useEffect(() => {
    let alive = true

    const bridge = (window as { hermesDesktop?: { getVersion?: () => Promise<{ appVersion?: unknown; bundleOutOfSync?: unknown }> } })
      .hermesDesktop

    if (!bridge?.getVersion) {
      return
    }

    bridge
      .getVersion()
      .then(info => {
        if (!alive) {
          return
        }

        setStamp({
          appVersion: typeof info?.appVersion === 'string' && info.appVersion ? info.appVersion : null,
          bundleOutOfSync: typeof info?.bundleOutOfSync === 'boolean' ? info.bundleOutOfSync : null
        })
      })
      .catch(() => {
        // The stamp is best-effort: a missing bridge is UNKNOWN, not an error.
      })

    return () => {
      alive = false
    }
  }, [])

  return stamp
}

/** Resolve the selected board's real slug (blank atom = server current board). */
function useSupportSlug(): string {
  const slug = useValue($boardSlug)
  const { data: boards } = useQuery({ queryKey: BOARDS_KEY, queryFn: fetchBoards, staleTime: 30_000 })

  return slug || boards?.current || ''
}

/** One identity line: label + state + revision + source. */
function IdentityRow({ label, state, revision, source }: { label: string; state?: string; revision?: null | string; source?: null | string }) {
  const s = state === 'PASS' ? 'PASS' : state === 'FAIL' ? 'FAIL' : 'UNKNOWN'

  return (
    <div className="flex items-baseline gap-2 text-[0.71rem]">
      <span className="shrink-0 font-medium text-(--ui-text-secondary)">{label}</span>
      <span className="inline-flex items-center gap-1 font-medium" style={{ color: tone(s) }}>
        <span className="size-1.5 rounded-full" style={{ backgroundColor: tone(s) }} />
        {s}
      </span>
      {revision && <span className="min-w-0 truncate font-mono text-[0.625rem] text-(--ui-text-quaternary)">{revision}</span>}
      {source && <span className="shrink-0 text-[0.625rem] text-(--ui-text-quaternary)">{source}</span>}
    </div>
  )
}

/** R2: one readiness fact, rendered as an explicit boolean — never inferred
 *  from a 200 or from reachability. */
function ReadinessFact({ label, value }: { label: string; value: boolean | null | undefined }) {
  const known = typeof value === 'boolean'
  const s = known ? (value ? 'PASS' : 'FAIL') : 'UNKNOWN'

  return (
    <div className="flex items-baseline gap-2 text-[0.71rem]">
      <span className="shrink-0 text-(--ui-text-secondary)">{label}</span>
      <span className="inline-flex items-center gap-1 font-medium" style={{ color: tone(s) }}>
        <span className="size-1.5 rounded-full" style={{ backgroundColor: tone(s) }} />
        {known ? (value ? 'yes' : 'no') : 'unknown'}
      </span>
    </div>
  )
}

/** R7: refresh timing + checked/skipped process checks from the shared
 *  snapshot envelope. */
function RefreshCoverageSection({ slug, aligned }: { slug: string; aligned: boolean }) {
  const { data: envelope, isPending } = useEvidenceSnapshot(slug, aligned)
  const snapshotEnvelope = envelope as EvidenceEnvelope<EvidenceSnapshotData> | undefined
  const data = snapshotEnvelope?.state === 'PASS' ? snapshotEnvelope.evidence : null

  const observations = Array.isArray(data?.worker_observations) ? data.worker_observations : []
  const checked = observations.length
  const cap = typeof data?.worker_observation_cap === 'number' ? data.worker_observation_cap : null
  const skipped = typeof data?.worker_observations_capped === 'number' ? data.worker_observations_capped : 0
  const roundtrip = snapshotEnvelope?.timing?.helper_roundtrip_ms
  const collection = snapshotEnvelope?.timing?.collection_ms
  const observedAt = typeof snapshotEnvelope?.observed_at === 'number' ? snapshotEnvelope.observed_at : null

  return (
    <Section label="Refresh & coverage">
      {isPending ? (
        <span className="text-[0.625rem] text-(--ui-text-quaternary)">Checking snapshot coverage…</span>
      ) : (
        <>
          {observedAt ? (
            <div className="flex items-baseline gap-2 text-[0.71rem] text-(--ui-text-secondary)">
              <span>Snapshot</span>
              <span className="text-(--ui-text-quaternary)">observed {new Date(observedAt * 1000).toLocaleTimeString()}</span>
            </div>
          ) : null}
          {(roundtrip != null || collection != null) && (
            <div className="flex items-baseline gap-2 text-[0.71rem] text-(--ui-text-secondary)">
              <span>Timing</span>
              <span className="tabular-nums text-(--ui-text-quaternary)">
                {roundtrip != null ? `helper ${roundtrip} ms` : ''}
                {roundtrip != null && collection != null ? ' · ' : ''}
                {collection != null ? `collection ${collection} ms` : ''}
              </span>
            </div>
          )}
          <div className="flex items-baseline gap-2 text-[0.71rem] text-(--ui-text-secondary)">
            <span>Process checks</span>
            <span className="tabular-nums text-(--ui-text-quaternary)">
              {checked} checked
              {skipped > 0 ? ` · ${skipped} skipped` : ''}
              {cap != null ? ` (cap ${cap})` : ''}
            </span>
          </div>
        </>
      )}
    </Section>
  )
}

/**
 * The whole support panel. Mounted by the board header only while the user
 * has it open; every read inside is board-scoped and bounded. The frontend
 * stamp is the running build's own loaded metadata, never a hardcoded value.
 */
export function SupportPanel() {
  const slug = useSupportSlug()
  const { data: context, isError: contextError } = useEvidenceContext(slug)
  const aligned = context?.aligned === true

  const { data: releasesEnvelope, isError: releasesError } = useQuery({
    queryKey: evidenceReleasesKey(slug),
    queryFn: () => fetchEvidenceReleases(slug),
    enabled: slug !== '',
    staleTime: 60_000,
    retry: false
  })

  const { data: readinessEnvelope, isError: readinessError } = useQuery({
    queryKey: browserReadinessKey(slug),
    queryFn: () => fetchBrowserReadiness(slug),
    enabled: slug !== '',
    staleTime: 60_000,
    retry: false
  })

  const stamp = useFrontendStamp()

  const releases = releasesEnvelope?.state === 'PASS' ? (releasesEnvelope.evidence as ReleasesData | null | undefined) : null
  const readiness = readinessEnvelope?.state === 'PASS' ? (readinessEnvelope.evidence as BrowserReadinessEvidence | null | undefined) : null

  return (
    <div className="flex flex-col gap-4 border-t border-(--ui-stroke-secondary) px-4 py-3 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)">
      {/* R1 — release identity */}
      <Section label="Release identity">
        <IdentityRow
          label="Frontend"
          revision={stamp.appVersion}
          source={stamp.bundleOutOfSync ? 'bundle out of sync' : stamp.appVersion ? 'desktop build' : undefined}
          state={stamp.appVersion ? 'PASS' : 'UNKNOWN'}
        />
        {releasesError ? (
          <span className="text-[0.71rem] text-destructive">Release identities could not be read.</span>
        ) : releasesEnvelope === undefined ? (
          <span className="text-[0.625rem] text-(--ui-text-quaternary)">Checking release identity…</span>
        ) : releasesEnvelope.state !== 'PASS' ? (
          <Callout title="Release identity unavailable" tone={STATE_TONE.UNKNOWN}>
            <p className="text-[0.71rem]">{releasesEnvelope.reason ?? 'The served adapter/backend identity is unavailable.'}</p>
          </Callout>
        ) : (
          <>
            <IdentityRow
              label="Backend"
              revision={releases?.backend?.revision}
              source={releases?.backend?.source}
              state={releases?.backend?.state}
            />
            <IdentityRow
              label="Adapter"
              revision={releases?.adapter?.revision}
              source={releases?.adapter?.source}
              state={releases?.adapter?.state}
            />
          </>
        )}
      </Section>

      {/* R2 — browser readiness (separate from connection reachability) */}
      <Section label="Browser readiness">
        {readinessError ? (
          <span className="text-[0.71rem] text-destructive">Browser readiness could not be checked.</span>
        ) : readinessEnvelope === undefined ? (
          <span className="text-[0.625rem] text-(--ui-text-quaternary)">Checking browser readiness…</span>
        ) : readinessEnvelope.state !== 'PASS' ? (
          <Callout title="Browser readiness unknown" tone={STATE_TONE.UNKNOWN}>
            <p className="text-[0.71rem]">
              {readinessEnvelope.reason ?? 'Browser readiness could not be established.'}
              {readinessEnvelope.remedy ? ` ${readinessEnvelope.remedy}` : ''}
            </p>
          </Callout>
        ) : (
          <>
            <ReadinessFact label="Reachable" value={readiness?.reachable} />
            <ReadinessFact label="Authenticated" value={readiness?.authenticated} />
            <ReadinessFact label="Board readable" value={readiness?.board_readable} />
            {readinessEnvelope?.remedy && (
              <span className="text-[0.625rem] text-(--ui-text-quaternary)">{readinessEnvelope.remedy}</span>
            )}
          </>
        )}
      </Section>

      {/* R7 — timing + checked/skipped (only meaningful when EVO-aligned) */}
      {aligned ? (
        <RefreshCoverageSection aligned={aligned} slug={slug} />
      ) : contextError ? (
        <span className="text-[0.71rem] text-(--ui-text-quaternary)">Refresh coverage needs the EVO connection.</span>
      ) : null}

      <div className="flex items-center gap-1.5 text-[0.625rem] text-(--ui-text-quaternary)">
        <Codicon name="info" size="0.7rem" />
        Read-only support data — no action here dispatches, releases, or changes configuration.
      </div>
    </div>
  )
}
