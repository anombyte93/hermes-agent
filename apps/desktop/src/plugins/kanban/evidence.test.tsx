/**
 * Worker-evidence state resolution + the rendered WorkerEvidenceSection.
 *
 * The HTTP boundary is the ONLY thing mocked: `./api` returns controlled
 * /evidence/context + /evidence/worker payloads, and the real component tree
 * (render → resolve → paint) is asserted. No source-grep or helper-predicate
 * assertion is the proof of record — the rendered output is.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

import { resolveWorkerState, type WorkerState } from './evidence'

// ---------------------------------------------------------------------------
// Mock the HTTP/network boundary (./api) and the SDK host.
// ---------------------------------------------------------------------------

const apiMock = vi.hoisted(() => ({
  fetchEvidenceContext: vi.fn(),
  fetchEvidenceWorker: vi.fn()
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
  evidenceContextKey: (slug: string) => ['kanban', 'evidence', 'context', slug],
  evidenceWorkerKey: (slug: string, id: string) => ['kanban', 'evidence', 'worker', slug, id],
  fetchEvidenceContext: apiMock.fetchEvidenceContext,
  fetchEvidenceWorker: apiMock.fetchEvidenceWorker
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

// ---------------------------------------------------------------------------
// Pure state reducer — the behavior contract.
// ---------------------------------------------------------------------------

describe('resolveWorkerState', () => {
  it('running only on positive process evidence', () => {
    expect(resolveWorkerState({ task_id: 't_1', observations: [{ task_id: 't_1', process_present: true }] })).toBe('running')
    expect(resolveWorkerState({ task_id: 't_1', observations: [], running: true })).toBe('running')
  })

  it('stopped only on explicit complete with no unknown/running', () => {
    expect(resolveWorkerState({ task_id: 't_1', observations: [], complete: true })).toBe('stopped')
  })

  it('missing observation is unknown, never stopped', () => {
    expect(resolveWorkerState(undefined)).toBe('unknown')
    expect(resolveWorkerState(null)).toBe('unknown')
    expect(resolveWorkerState({ task_id: 't_1', observations: [] })).toBe('unknown')
  })

  it('complete with an unknown flag stays unknown', () => {
    expect(resolveWorkerState({ task_id: 't_1', observations: [], complete: true, unknown: true })).toBe('unknown')
  })

  it('a running observation wins over a complete flag', () => {
    expect(
      resolveWorkerState({ task_id: 't_1', observations: [{ task_id: 't_1', process_present: true }], complete: true })
    ).toBe('running')
  })
})

// ---------------------------------------------------------------------------
// Rendered component tree.
// ---------------------------------------------------------------------------

async function importSection() {
  const mod = await import('./evidence')

  return mod.WorkerEvidenceSection
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })
}

async function renderSection(id: string) {
  const WorkerEvidenceSection = await importSection()

  render(
    <QueryClientProvider client={makeClient()}>
      <WorkerEvidenceSection id={id} />
    </QueryClientProvider>
  )
}

const context = (aligned: boolean) => ({ aligned, board: 'default', hostname: 'evo', reason: aligned ? 'aligned' : 'no', observed_at: 1 })

const envelope = (state: 'PASS' | 'FAIL' | 'UNKNOWN', evidence?: unknown, reason?: string) => ({
  state,
  evidence,
  reason,
  observed_at: 1789216932.8
})

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

describe('WorkerEvidenceSection (rendered)', () => {
  it('shows Running for positive process evidence on an aligned board', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(
      envelope('PASS', { task_id: 't_1', observations: [{ task_id: 't_1', state: 'running', process_present: true }] })
    )

    await renderSection('t_1')

    expect(await screen.findByText('Worker evidence')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('Running')).toBeTruthy())
  })

  it('shows Unknown (not Stopped) when there is no observation', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('PASS', { task_id: 't_1', observations: [] }))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unknown')).toBeTruthy())
  })

  it('shows Unavailable (not red) for a non-PASS envelope', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('UNKNOWN', undefined, 'helper unavailable'))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unavailable')).toBeTruthy())
    expect(await screen.findByText('helper unavailable')).toBeTruthy()
  })

  it('renders nothing when the board is not EVO-aligned', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(false))
    // The worker query must never fire when alignment is false.
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('PASS', { task_id: 't_1', observations: [] }))

    await renderSection('t_1')

    await waitFor(() => expect(apiMock.fetchEvidenceContext).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('Worker evidence')).toBeNull())
    expect(apiMock.fetchEvidenceWorker).not.toHaveBeenCalled()
  })
})

// Keep the type import used so the isolated module surface stays explicit.
export type { WorkerState }
