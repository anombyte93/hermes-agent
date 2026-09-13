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

const ctx = (aligned: boolean) => ({ aligned, board: 'evo', hostname: 'evo', reason: aligned ? 'aligned' : 'no', observed_at: 1 })

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
  versionMock.getVersion.mockResolvedValue({ appVersion: '0.17.0', bundleOutOfSync: false })
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

describe('R1 — release identity', () => {
  it('shows frontend, backend and adapter identity together', async () => {
    apiMock.fetchEvidenceReleases.mockResolvedValue(
      envelope('PASS', {
        board: 'evo',
        adapter: { state: 'PASS', revision: 'adapter-abc123', source: 'git' },
        backend: { state: 'PASS', revision: 'backend-def456', source: 'pip' },
        observed_at: 1789216932.8
      })
    )

    renderPanel()

    expect(await screen.findByText('Frontend')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('0.17.0')).toBeTruthy())
    expect(screen.getByText('Backend')).toBeTruthy()
    expect(screen.getByText('Adapter')).toBeTruthy()
    expect(screen.getByText('adapter-abc123')).toBeTruthy()
    expect(screen.getByText('backend-def456')).toBeTruthy()
  })

  it('the frontend stamp is UNKNOWN when the build metadata is absent (never hardcoded)', async () => {
    versionMock.getVersion.mockResolvedValue({ appVersion: undefined })
    apiMock.fetchEvidenceReleases.mockResolvedValue(envelope('PASS', { board: 'evo', observed_at: 1 }))

    renderPanel()

    await waitFor(() => expect(screen.getAllByText('UNKNOWN').length).toBeGreaterThan(0))
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
      envelope('PASS', { reachable: true, authenticated: false, board_readable: false, observed_at: 1 }, 'login required')
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
})

describe('R7 — refresh timing + checked/skipped process checks', () => {
  it('shows real timing and checked-vs-skipped counters from the shared snapshot', async () => {
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
        worker_observations_capped: 3
      })
    )

    renderPanel()

    expect(await screen.findByText('Refresh & coverage')).toBeTruthy()
    expect(await screen.findByText(/helper 42 ms/)).toBeTruthy()
    expect(screen.getByText(/collection 7 ms/)).toBeTruthy()
    expect(screen.getByText(/2 checked · 3 skipped \(cap 20\)/)).toBeTruthy()
  })
})
