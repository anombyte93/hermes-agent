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

/** The running desktop build's own identity, read from the loaded build
 *  metadata. Absent bridge / invalid value → UNKNOWN, never invented. */
interface FrontendStamp {
  /** Backend runtime version (resolveHermesVersion from the update root). */
  appVersion: null | string
  /** The loaded renderer bundle's own build commit (install-stamp.json). */
  rendererCommit: null | string
  /** Stamp source tag ('ci' | 'local' | 'fallback'), null when absent. */
  rendererStampSource: null | string
  /** Stamp build timestamp (ISO-8601), null when absent. */
  rendererBuiltAt: null | string
  /** Whether the packaged build was dirty at stamp time, null when absent. */
  rendererDirty: null | boolean
  bundleOutOfSync: null | boolean
}

function useFrontendStamp(): FrontendStamp {
  const [stamp, setStamp] = useState<FrontendStamp>({
    appVersion: null,
    rendererCommit: null,
    rendererStampSource: null,
    rendererBuiltAt: null,
    rendererDirty: null,
    bundleOutOfSync: null
  })

  useEffect(() => {
    let alive = true

    const bridge = (window as unknown as { hermesDesktop?: { getVersion?: () => Promise<Record<string, unknown>> } }).hermesDesktop

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
          // R1: the renderer build commit is its own identity. It is NEVER
          // derived from appVersion or bundleCommitsBehind; only the stamp's
          // commit field names the loaded bundle. Absent → UNKNOWN.
          rendererCommit: typeof info?.rendererCommit === 'string' && info.rendererCommit ? info.rendererCommit : null,
          rendererStampSource: typeof info?.rendererStampSource === 'string' && info.rendererStampSource ? info.rendererStampSource : null,
          rendererBuiltAt: typeof info?.rendererBuiltAt === 'string' && info.rendererBuiltAt ? info.rendererBuiltAt : null,
          rendererDirty: typeof info?.rendererDirty === 'boolean' ? info.rendererDirty : null,
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

/** R2: classify a browser-readiness failure into one of the distinct visible
 *  remedies the contract requires (401/403 auth vs login-HTML vs malformed vs
 *  generic). Pure: never invents a success, never bypasses auth. */
type ReadinessFailureKind = 'unauthorized' | 'forbidden' | 'login' | 'malformed' | 'unreachable' | 'unknown'

function classifyReadinessFailure(err: unknown, reason: string | null | undefined): { kind: ReadinessFailureKind; remedy: string } {
  // The electron REST bridge rejects HTTP errors as `Error("401: {...}")`.
  const raw = err instanceof Error ? err.message : err ? String(err) : ''
  const statusMatch = raw.match(/\b(40[13])\b/)

  if (statusMatch) {
    return statusMatch[1] === '401'
      ? { kind: 'unauthorized', remedy: 'Authentication required — sign in to the EVO gateway, then retry.' }
      : { kind: 'forbidden', remedy: 'This board is not readable for the current session — check board access, not reachability.' }
  }

  const lower = (reason ?? raw).toLowerCase()

  if (lower.includes('login') || lower.includes('html') || lower.includes('redirect')) {
    return { kind: 'login', remedy: 'A login page was returned — the browser is not authenticated. Sign in first.' }
  }

  if (lower.includes('malformed') || lower.includes('parse') || lower.includes('json')) {
    return { kind: 'malformed', remedy: 'The readiness response was malformed — the backend returned an unreadable payload.' }
  }

  if (lower.includes('reachable') || lower.includes('unreachable') || lower.includes('connect') || lower.includes('timeout')) {
    return { kind: 'unreachable', remedy: 'The EVO gateway could not be reached — check the connection, not the board.' }
  }

  return { kind: 'unknown', remedy: 'Browser readiness could not be established.' }
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
 *  snapshot envelope. Timing is read from the ACTUAL adapter evidence
 *  (`evidence.timing.query_seconds` / `evidence.timing.collection_seconds`),
 *  never an invented `envelope.collection_ms`. A MISSING `worker_observations`
 *  is UNKNOWN (never coerced to zero); a PRESENT array (even empty) is a
 *  measured count, so a valid measured zero renders as zero. */
function RefreshCoverageSection({ slug, aligned }: { slug: string; aligned: boolean }) {
  const { data: envelope, isPending } = useEvidenceSnapshot(slug, aligned)
  const snapshotEnvelope = envelope as EvidenceEnvelope<EvidenceSnapshotData> | undefined
  const data = snapshotEnvelope?.state === 'PASS' ? snapshotEnvelope.evidence : null

  // worker_observations: present array → measured count (0 is a valid measured
  // zero); missing/absent → UNKNOWN, never coerced to zero.
  const observations = data?.worker_observations
  const hasObservations = Array.isArray(observations)
  const checked = hasObservations ? observations.length : null
  const cap = typeof data?.worker_observation_cap === 'number' ? data.worker_observation_cap : null
  const skipped = typeof data?.worker_observations_capped === 'number' ? data.worker_observations_capped : null
  // Actual adapter timing lives under evidence.timing (seconds), not the
  // envelope's helper_roundtrip_ms/collection_ms.
  const querySeconds = typeof data?.timing?.query_seconds === 'number' ? data.timing.query_seconds : null
  const collectionSeconds = typeof data?.timing?.collection_seconds === 'number' ? data.timing.collection_seconds : null
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
          {(querySeconds != null || collectionSeconds != null) && (
            <div className="flex items-baseline gap-2 text-[0.71rem] text-(--ui-text-secondary)">
              <span>Timing</span>
              <span className="tabular-nums text-(--ui-text-quaternary)">
                {querySeconds != null ? `query ${querySeconds.toFixed(3)} s` : ''}
                {querySeconds != null && collectionSeconds != null ? ' · ' : ''}
                {collectionSeconds != null ? `collection ${collectionSeconds.toFixed(3)} s` : ''}
              </span>
            </div>
          )}
          <div className="flex items-baseline gap-2 text-[0.71rem] text-(--ui-text-secondary)">
            <span>Process checks</span>
            <span className="tabular-nums text-(--ui-text-quaternary)">
              {checked != null ? `${checked} checked` : 'checked unknown'}
              {skipped != null && skipped > 0 ? ` · ${skipped} skipped` : ''}
              {skipped === 0 ? ' · 0 skipped' : ''}
              {cap != null ? ` (cap ${cap})` : ''}
            </span>
          </div>
          {!hasObservations && (
            <span className="text-[0.625rem] text-(--ui-text-quaternary)">Worker observations were not reported by this snapshot.</span>
          )}
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

  const { data: readinessEnvelope, isError: readinessError, error: readinessErr } = useQuery({
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
      {/* R1 — release identity: renderer build / backend / adapter, each its
          own identity, never conflated. */}
      <Section label="Release identity">
        <IdentityRow
          label="Renderer build"
          revision={stamp.rendererCommit}
          source={
            stamp.rendererCommit
              ? [stamp.rendererDirty ? 'dirty' : null, stamp.bundleOutOfSync ? 'bundle out of sync' : null, 'renderer build']
                  .filter(Boolean)
                  .join(' · ')
              : undefined
          }
          state={stamp.rendererCommit ? 'PASS' : 'UNKNOWN'}
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

      {/* R2 — browser readiness (separate from connection reachability).
          A 401/403/login-HTML/malformed failure is classified with a distinct
          remedy; auth is never inferred from reachability or a 200. */}
      <Section label="Browser readiness">
        {readinessError ? (
          (() => {
            const failure = classifyReadinessFailure(readinessErr, null)

            return (
              <Callout title={`Browser readiness — ${failure.kind}`} tone={STATE_TONE.FAIL}>
                <p className="text-[0.71rem]">{failure.remedy}</p>
              </Callout>
            )
          })()
        ) : readinessEnvelope === undefined ? (
          <span className="text-[0.625rem] text-(--ui-text-quaternary)">Checking browser readiness…</span>
        ) : readinessEnvelope.state !== 'PASS' ? (
          (() => {
            const failure = classifyReadinessFailure(null, readinessEnvelope.reason)

            return (
              <Callout title={`Browser readiness — ${failure.kind}`} tone={STATE_TONE.UNKNOWN}>
                <p className="text-[0.71rem]">
                  {readinessEnvelope.reason ?? 'Browser readiness could not be established.'}
                  {readinessEnvelope.remedy ? ` ${readinessEnvelope.remedy}` : ` ${failure.remedy}`}
                </p>
              </Callout>
            )
          })()
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
