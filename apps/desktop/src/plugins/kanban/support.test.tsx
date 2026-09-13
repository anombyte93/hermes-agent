/**
 * R1 / R2 / R7 — the rendered support panel.
 *
 * The ONLY boundary mocked is the network transport (`./api`) and the OS
 * version door (`window.hermesDesktop.getVersion`); the real SupportPanel,
 * the real /evidence/* hooks, and a real React Query client are rendered.
 * Proof of record is rendered output, never a helper predicate.
 *
 * R1  frontend/backend/adapter identity together; frontend stamp comes from
 *     the live loaded build metadata (never a hardcoded SHA).
 * R2  reachable / authenticated / board_readable as THREE separate facts —
 *     never inferred from a 200 or from reachability.
 * R7  real timing + checked-vs-skipped process checks from the shared snapshot.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SupportPanel } from './support'

const apiMock = vi.hoisted(() => ({
  fetchBoards: vi.fn(),
  fetchEvidenceContext: vi.fn(),
  fetchEvidenceSnapshot: vi.fn(),
  fetchEvidenceReleases: vi.fn(),
  fetchBrowserReadiness: vi.fn()
}))

const versionMock = vi.hoisted(() => ({
  getVersion: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const rq = await import('@tanstack/react-query')

  return {
    useQuery: (opts: Parameters<typeof rq.useQuery>[0]) => rq.useQuery(opts),
    useValue: () => '',
    Codicon: () => null,
    Loader: () => null
  }
})

vi.mock('./api', () => ({
  $boardSlug: { get: () => '', set: () => {}, listen: () => () => {} },
  BOARDS_KEY: ['kanban', 'boards'],
  browserReadinessKey: (slug: string) => ['kanban', 'evidence', 'browser-readiness', slug],
  evidenceReleasesKey: (slug: string) => ['kanban', 'evidence', 'releases', slug],
  evidenceContextKey: (slug: string) => ['kanban', 'evidence', 'context', slug],
  evidenceSnapshotKey: (slug: string, status: string, cursor: null | string) => [
    'kanban',
    'evidence',
    'snapshot',
    slug,
    status,
    cursor
  ],
  fetchBoards: apiMock.fetchBoards,
  fetchEvidenceContext: apiMock.fetchEvidenceContext,
  fetchEvidenceSnapshot: apiMock.fetchEvidenceSnapshot,
  fetchEvidenceReleases: apiMock.fetchEvidenceReleases,
  fetchBrowserReadiness: apiMock.fetchBrowserReadiness
}))

vi.mock('./ui', () => ({
  Section: ({ children, label }: { children?: ReactNode; label: string }) => (
    <div data-testid="section">
      <div>{label}</div>
      {children}
    </div>
  ),
  Callout: ({ children, title }: { children?: ReactNode; title: ReactNode }) => (
    <div data-testid="callout">
      <div>{title}</div>
      {children}
    </div>
  )
}))

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })
}

function renderPanel() {
  render(
    <QueryClientProvider client={makeClient()}>
      <SupportPanel />
    </QueryClientProvider>
  )
}

const ctx = (aligned: boolean) => ({
  aligned,
  board: 'evo',
  hostname: 'evo',
  reason: aligned ? 'aligned' : 'no',
  observed_at: 1
})

const envelope = (state: 'PASS' | 'FAIL' | 'UNKNOWN', evidence?: unknown, reason?: string) => ({
  state,
  evidence,
  reason,
  observed_at: 1789216932.8,
  timing: { helper_roundtrip_ms: 42, collection_ms: 7 }
})

beforeEach(() => {
  apiMock.fetchBoards.mockResolvedValue({ boards: [], current: 'evo' })
  apiMock.fetchEvidenceContext.mockResolvedValue(ctx(true))
  apiMock.fetchEvidenceSnapshot.mockResolvedValue(
    envelope('PASS', {
      board: 'evo',
      cards: [],
      counts: { by_status: {}, total: 0 },
      observed_at: 1789216932.8,
      worker_observations: [],
      worker_observation_cap: 20,
      worker_observations_capped: 3
    })
  )
  // @ts-expect-error installing a minimal bridge for the frontend-stamp read
  window.hermesDesktop = { getVersion: versionMock.getVersion }
  versionMock.getVersion.mockResolvedValue({
    appVersion: '0.17.0',
    bundleOutOfSync: false,
    rendererCommit: 'abc123def4567890',
    rendererStampSource: 'ci',
    rendererBuiltAt: '2026-09-13T00:00:00Z',
    rendererDirty: false
  })
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

describe('R1 — release identity', () => {
  it('shows the renderer build commit, backend and adapter identity together — renderer commit is its own identity, never appVersion', async () => {
    apiMock.fetchEvidenceReleases.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        adapter: { state: 'PASS', revision: 'adapter-abc123', source: 'git' },
        backend: { state: 'PASS', revision: 'backend-def456', source: 'pip' },
        observed_at: 1789216932.8
      })
    )

    renderPanel()

    // The renderer build row shows the LOADED bundle's commit (the stamp), not
    // the backend appVersion and never bundleCommitsBehind.
    expect(await screen.findByText('Renderer build')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('abc123def4567890')).toBeTruthy())
    expect(screen.getByText('Backend')).toBeTruthy()
    expect(screen.getByText('Adapter')).toBeTruthy()
    expect(screen.getByText('adapter-abc123')).toBeTruthy()
    expect(screen.getByText('backend-def456')).toBeTruthy()
    // The backend appVersion (0.17.0) is NOT the renderer identity and is not
    // painted as such.
    expect(screen.queryByText('0.17.0')).toBeNull()
  })

  it('the renderer build is UNKNOWN when the stamp is absent (never hardcoded), while backend identity still shows', async () => {
    versionMock.getVersion.mockResolvedValue({ appVersion: '0.17.0', bundleOutOfSync: false, rendererCommit: null })
    apiMock.fetchEvidenceReleases.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        adapter: { state: 'PASS', revision: 'adapter-abc123', source: 'git' },
        backend: { state: 'PASS', revision: 'backend-def456', source: 'pip' },
        observed_at: 1
      })
    )

    renderPanel()

    await waitFor(() => expect(screen.getByText('Renderer build')).toBeTruthy())
    // Missing renderer stamp → UNKNOWN (the row has no invented SHA), while
    // the backend/adapter identities remain.
    await waitFor(() => expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0))
    expect(screen.getByText('backend-def456')).toBeTruthy()
    expect(screen.getByText('adapter-abc123')).toBeTruthy()
  })

  it('an absent/invalid adapter identity is UNKNOWN, not invented', async () => {
    apiMock.fetchEvidenceReleases.mockResolvedValue(
      envelope('PASS', { board: 'evo', adapter: { state: 'UNKNOWN', revision: null, source: null }, observed_at: 1 })
    )

    renderPanel()

    await waitFor(() => expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0))
  })
})

describe('R2 — browser readiness (separate from reachability)', () => {
  it('renders reachable / authenticated / board readable as three separate facts', async () => {
    apiMock.fetchBrowserReadiness.mockResolvedValue(
      envelope(
        'PASS',
        { reachable: true, authenticated: false, board_readable: false, observed_at: 1 },
        'login required'
      )
    )

    renderPanel()

    expect(await screen.findByText('Reachable')).toBeTruthy()
    expect(screen.getByText('Authenticated')).toBeTruthy()
    expect(screen.getByText('Board readable')).toBeTruthy()

    // Reachable yes does NOT imply authenticated — never inferred from a 200.
    expect(await screen.findByText('yes')).toBeTruthy()
    expect(screen.getAllByText('no').length).toBe(2)
  })

  it('a non-PASS readiness envelope surfaces its reason, never an inferred pass', async () => {
    apiMock.fetchBrowserReadiness.mockResolvedValue(envelope('UNKNOWN', null, 'auth boundary could not be reached'))

    renderPanel()

    expect(await screen.findByText(/auth boundary could not be reached/)).toBeTruthy()
    expect(screen.queryByText('Reachable')).toBeNull()
  })

  it('classifies a 401 transport failure as authentication (distinct remedy), never an inferred pass', async () => {
    apiMock.fetchBrowserReadiness.mockRejectedValue(new Error('401: {"detail":"not authenticated"}'))

    renderPanel()

    expect(await screen.findByText(/Browser readiness — unauthorized/)).toBeTruthy()
    expect(screen.getByText(/Authentication required — sign in to the EVO gateway/)).toBeTruthy()
    expect(screen.queryByText('Reachable')).toBeNull()
  })

  it('classifies a 403 transport failure as forbidden (distinct remedy)', async () => {
    apiMock.fetchBrowserReadiness.mockRejectedValue(new Error('403: {"detail":"forbidden"}'))

    renderPanel()

    expect(await screen.findByText(/Browser readiness — forbidden/)).toBeTruthy()
    expect(screen.getByText(/check board access, not reachability/)).toBeTruthy()
  })

  it('classifies a login-HTML envelope reason as unauthenticated (distinct from reachability)', async () => {
    apiMock.fetchBrowserReadiness.mockResolvedValue(envelope('FAIL', null, 'reachable but returned a login page'))

    renderPanel()

    expect(await screen.findByText(/Browser readiness — login/)).toBeTruthy()
    expect(screen.getByText(/not authenticated/)).toBeTruthy()
  })
})

describe('R7 — refresh timing + checked/skipped process checks', () => {
  it('shows the ACTUAL evidence timing (query_seconds/collection_seconds) and checked-vs-skipped counters', async () => {
    apiMock.fetchEvidenceSnapshot.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        cards: [],
        counts: { by_status: {}, total: 0 },
        observed_at: 1789216932.8,
        worker_observations: [
          { task_id: 't_1', state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: true },
          { task_id: 't_2', state: 'PASS', process_present: true }
        ],
        worker_observation_cap: 20,
        worker_observations_capped: 3,
        // The adapter reports measured page/collection timing here, NOT the
        // envelope's helper_roundtrip_ms / collection_ms.
        timing: { query_seconds: 0.042, collection_seconds: 0.007 }
      })
    )

    renderPanel()

    expect(await screen.findByText('Refresh & coverage')).toBeTruthy()
    expect(await screen.findByText(/query 0.042 s/)).toBeTruthy()
    expect(screen.getByText(/collection 0.007 s/)).toBeTruthy()
    expect(screen.getByText(/2 checked · 3 skipped \(cap 20\)/)).toBeTruthy()
    // The invented envelope timing is never rendered.
    expect(screen.queryByText(/helper 42 ms/)).toBeNull()
  })

  it('a valid measured zero (present empty worker_observations) renders zero, never UNKNOWN', async () => {
    apiMock.fetchEvidenceSnapshot.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        cards: [],
        counts: { by_status: {}, total: 0 },
        observed_at: 1789216932.8,
        worker_observations: [],
        worker_observation_cap: 20,
        worker_observations_capped: 0
      })
    )

    renderPanel()

    expect(await screen.findByText(/0 checked · 0 skipped \(cap 20\)/)).toBeTruthy()
  })

  it('MISSING worker_observations is UNKNOWN (never coerced to zero)', async () => {
    apiMock.fetchEvidenceSnapshot.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        cards: [],
        counts: { by_status: {}, total: 0 },
        observed_at: 1789216932.8
        // no worker_observations, no cap, no capped — all absent
      })
    )

    renderPanel()

    // checked renders "unknown", and no "0 checked" / "0 skipped" is invented.
    expect(await screen.findByText(/checked unknown/)).toBeTruthy()
    expect(screen.queryByText(/0 checked/)).toBeNull()
    expect(screen.getByText(/Worker observations were not reported by this snapshot/)).toBeTruthy()
  })
})
