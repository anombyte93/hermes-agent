/**
 * Worker-evidence state resolution + the rendered WorkerEvidenceSection.
 *
 * The HTTP boundary is the ONLY thing mocked: `./api` returns controlled
 * /evidence/context + /evidence/worker payloads, and the real component tree
 * (render → resolve → paint) is asserted. No source-grep or helper-predicate
 * assertion is the proof of record — the rendered output is.
 *
 * The resolver tests use the REAL released helper shape: worker verdicts are
 * NESTED under `data.aggregate`, and running is positive exact identity
 * (state=PASS + process_present + workspace_matches + run_start_matches).
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
  fetchBoards: vi.fn(),
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
  BOARDS_KEY: ['kanban', 'boards'],
  evidenceContextKey: (slug: string) => ['kanban', 'evidence', 'context', slug],
  evidenceWorkerKey: (slug: string, id: string) => ['kanban', 'evidence', 'worker', slug, id],
  fetchBoards: apiMock.fetchBoards,
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
// Pure state reducer — the behavior contract, using the REAL nested shape.
// ---------------------------------------------------------------------------

describe('resolveWorkerState', () => {
  it('running only on exact positive identity (all four)', () => {
    const positive = {
      task_id: 't_1',
      observations: [
        { task_id: 't_1', state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: true }
      ]
    }
    expect(resolveWorkerState(positive)).toBe('running')
  })

  it('a bare process_present is NOT running (too weak)', () => {
    expect(
      resolveWorkerState({ task_id: 't_1', observations: [{ task_id: 't_1', state: 'PASS', process_present: true }] })
    ).toBe('unknown')
  })

  it('process_present without workspace/run-start match is NOT running', () => {
    expect(
      resolveWorkerState({
        task_id: 't_1',
        observations: [
          { task_id: 't_1', state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: false }
        ]
      })
    ).toBe('unknown')
  })

  it('stopped only on nested aggregate: complete, running=0, unknown=0, with evidence', () => {
    expect(
      resolveWorkerState({
        task_id: 't_1',
        observations: [],
        aggregate: { overall: 'STOPPED', running: 0, stopped: 0, completion_records: 1, unknown: 0, complete: true }
      })
    ).toBe('stopped')
  })

  it('complete aggregate with zero runs is unknown, not stopped', () => {
    expect(
      resolveWorkerState({
        task_id: 't_1',
        observations: [],
        aggregate: { overall: 'STOPPED', running: 0, stopped: 0, completion_records: 0, unknown: 0, complete: true }
      })
    ).toBe('unknown')
  })

  it('aggregate with running>0 is not stopped even when complete', () => {
    expect(
      resolveWorkerState({
        task_id: 't_1',
        observations: [],
        aggregate: { overall: 'RUNNING', running: 1, stopped: 0, completion_records: 0, unknown: 0, complete: true }
      })
    ).toBe('unknown')
  })

  it('missing observation is unknown, never stopped', () => {
    expect(resolveWorkerState(undefined)).toBe('unknown')
    expect(resolveWorkerState(null)).toBe('unknown')
    expect(resolveWorkerState({ task_id: 't_1', observations: [] })).toBe('unknown')
  })

  it('a running observation wins over a stopped aggregate', () => {
    expect(
      resolveWorkerState({
        task_id: 't_1',
        observations: [
          { task_id: 't_1', state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: true }
        ],
        aggregate: { overall: 'STOPPED', running: 0, stopped: 0, completion_records: 1, unknown: 0, complete: true }
      })
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
  apiMock.fetchBoards.mockResolvedValue({ boards: [], current: 'default' })
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

describe('WorkerEvidenceSection (rendered)', () => {
  it('shows Running for exact positive identity on an aligned board', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(
      envelope('PASS', {
        task_id: 't_1',
        observations: [
          { task_id: 't_1', state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: true }
        ]
      })
    )

    await renderSection('t_1')

    expect(await screen.findByText('Worker evidence')).toBeTruthy()
    await waitFor(() => expect(screen.getByText('Running')).toBeTruthy())
  })

  it('shows Unknown (not Running) for a bare process_present observation', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(
      envelope('PASS', { task_id: 't_1', observations: [{ task_id: 't_1', state: 'PASS', process_present: true }] })
    )

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unknown')).toBeTruthy())
  })

  it('shows Unknown (not Stopped) when there is no observation', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('PASS', { task_id: 't_1', observations: [] }))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unknown')).toBeTruthy())
  })

  it('shows Stopped for a nested complete aggregate with a completion record', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(
      envelope('PASS', {
        task_id: 't_1',
        observations: [],
        aggregate: { overall: 'STOPPED', running: 0, stopped: 0, completion_records: 1, unknown: 0, complete: true },
        completion_runs: [{ run_id: 43, summary: 'done', started_at: 1, ended_at: 1 }]
      })
    )

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Stopped')).toBeTruthy())
    expect(screen.getByText('done')).toBeTruthy()
  })

  it('shows Unavailable (not red) for a non-PASS envelope', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('UNKNOWN', undefined, 'helper unavailable'))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unavailable')).toBeTruthy())
    expect(await screen.findByText('helper unavailable')).toBeTruthy()
  })

  it('shows the choose-EVO remedy (not silence) when the board is not aligned', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(false))
    // The worker query must never fire when alignment is false.
    apiMock.fetchEvidenceWorker.mockResolvedValue(envelope('PASS', { task_id: 't_1', observations: [] }))

    await renderSection('t_1')

    await waitFor(() => expect(apiMock.fetchEvidenceContext).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByText('Choose the EVO connection')).toBeTruthy())
    expect(apiMock.fetchEvidenceWorker).not.toHaveBeenCalled()
  })

  it('shows the choose-EVO remedy when the context query errors (no hang)', async () => {
    apiMock.fetchEvidenceContext.mockRejectedValue(new Error('network down'))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Choose the EVO connection')).toBeTruthy())
    expect(apiMock.fetchEvidenceWorker).not.toHaveBeenCalled()
  })

  it('shows Unavailable (not infinite Loading) when the worker query errors', async () => {
    apiMock.fetchEvidenceContext.mockResolvedValue(context(true))
    apiMock.fetchEvidenceWorker.mockRejectedValue(new Error('boom'))

    await renderSection('t_1')

    await waitFor(() => expect(screen.getByText('Unavailable')).toBeTruthy())
  })
})

// Keep the type import used so the isolated module surface stays explicit.
export type { WorkerState }
