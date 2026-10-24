/**
 * Mounted KanbanBoardPage test — the load-bearing proof that the aligned board
 * renders from the bounded snapshot, pages through Load more, and never falls
 * back to a silent local /board read.
 *
 * The ONLY boundary mocked is the network transport (`./api`): every fetch
 * returns a controlled fixture, and the real component tree (KanbanBoardPage →
 * Column → Card → the real evidence hook) is rendered with the real SDK and a
 * real React Query client. No source-grep or helper-predicate assertion is the
 * proof of record — the rendered output is.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { KanbanBoardPage } from './board'

// ---------------------------------------------------------------------------
// Network transport boundary (./api) + the app's own REST client (@/hermes).
// The SDK and every component are the real ones.
// ---------------------------------------------------------------------------

const h = vi.hoisted(() => {
  const fetch = {
    fetchBoards: vi.fn(),
    fetchBoard: vi.fn(),
    fetchProfiles: vi.fn(),
    fetchOrchestration: vi.fn(),
    fetchEvidenceContext: vi.fn(),
    fetchEvidenceSnapshot: vi.fn(),
    fetchEvidenceWorker: vi.fn(),
    patchTask: vi.fn(),
    createTask: vi.fn(),
    deleteTask: vi.fn(),
    bulkTasks: vi.fn(),
    estimateNew: vi.fn(),
    fetchTask: vi.fn(),
    fetchLog: vi.fn(),
    fetchProjects: vi.fn(),
    estimateTask: vi.fn(),
    addComment: vi.fn(),
    reassignTask: vi.fn(),
    reclaimTask: vi.fn(),
    uploadAttachment: vi.fn(),
    createBoard: vi.fn(),
    updateBoard: vi.fn(),
    saveOrchestration: vi.fn(),
    saveProfileDescription: vi.fn(),
    autoDescribeProfile: vi.fn()
  }

  // Populated by the ./api mock factory with real nanostore atoms.

  const atoms: Record<string, any> = {}

  return { fetch, atoms }
})

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: vi.fn().mockResolvedValue({ providers: [] }),
  setApiRequestProfile: vi.fn()
}))

vi.mock('./api', async () => {
  const { atom } = await import('nanostores')

  const $boardSlug = atom<string>('')
  const $collapsedLanes = atom<Record<string, boolean>>({})
  const $introDismissed = atom<boolean>(false)
  const $lanesByProfile = atom<boolean>(false)

  h.atoms.$boardSlug = $boardSlug
  h.atoms.$collapsedLanes = $collapsedLanes
  h.atoms.$introDismissed = $introDismissed
  h.atoms.$lanesByProfile = $lanesByProfile

  return {
    $boardSlug,
    $collapsedLanes,
    $introDismissed,
    $lanesByProfile,
    boardKey: (slug: string, archived: boolean) => ['kanban', 'board', slug, archived],
    taskKey: (slug: string, id: string) => ['kanban', 'task', slug, id],
    logKey: (slug: string, id: string) => ['kanban', 'log', slug, id],
    BOARDS_KEY: ['kanban', 'boards'],
    PROFILES_KEY: ['kanban', 'profiles'],
    PROJECTS_KEY: ['kanban', 'projects'],
    ORCHESTRATION_KEY: ['kanban', 'orchestration'],
    evidenceContextKey: (slug: string) => ['kanban', 'evidence', 'context', slug],
    evidenceSnapshotKey: (slug: string, status: string, cursor: null | string) => [
      'kanban', 'evidence', 'snapshot', slug, status, cursor
    ],
    evidenceWorkerKey: (slug: string, id: string) => ['kanban', 'evidence', 'worker', slug, id],
    evidenceCardKey: (slug: string, id: string) => ['kanban', 'evidence', 'card', slug, id],
    evidencePageKey: (slug: string, resource: string, card: null | string, cursor: null | string) => [
      'kanban', 'evidence', 'page', slug, resource, card, cursor
    ],
    fetchBoard: h.fetch.fetchBoard,
    fetchBoards: h.fetch.fetchBoards,
    fetchProfiles: h.fetch.fetchProfiles,
    fetchOrchestration: h.fetch.fetchOrchestration,
    fetchEvidenceContext: h.fetch.fetchEvidenceContext,
    fetchEvidenceSnapshot: h.fetch.fetchEvidenceSnapshot,
    fetchEvidenceWorker: h.fetch.fetchEvidenceWorker,
    patchTask: h.fetch.patchTask,
    createTask: h.fetch.createTask,
    deleteTask: h.fetch.deleteTask,
    bulkTasks: h.fetch.bulkTasks,
    estimateNew: h.fetch.estimateNew,
    fetchTask: h.fetch.fetchTask,
    fetchLog: h.fetch.fetchLog,
    fetchProjects: h.fetch.fetchProjects,
    estimateTask: h.fetch.estimateTask,
    addComment: h.fetch.addComment,
    reassignTask: h.fetch.reassignTask,
    reclaimTask: h.fetch.reclaimTask,
    uploadAttachment: h.fetch.uploadAttachment,
    createBoard: h.fetch.createBoard,
    updateBoard: h.fetch.updateBoard,
    saveOrchestration: h.fetch.saveOrchestration,
    saveProfileDescription: h.fetch.saveProfileDescription,
    autoDescribeProfile: h.fetch.autoDescribeProfile
  }
})

// Radix calls these on open; jsdom does not implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const now = Math.floor(Date.now() / 1000)

function ctx(aligned: boolean, board = 'evo'): Record<string, unknown> {
  return { aligned, board, hostname: 'evo', reason: aligned ? 'aligned' : 'not the EVO host', observed_at: now }
}

function card(id: string, title: string, status: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  return { id, title, status, assignee: 'evo', created_at: now - 10, ...extra }
}

function positiveObs(id: string): Record<string, unknown> {
  return { task_id: id, state: 'PASS', process_present: true, workspace_matches: true, run_start_matches: true }
}

function snapshotPage(opts: {
  cards: Array<Record<string, unknown>>
  hasMore?: boolean
  nextCursor?: null | string
  omitted?: number
  byStatus?: Record<string, number>
  workerObservations?: Array<Record<string, unknown>>
}): Record<string, unknown> {
  const cards = opts.cards

  const byStatus = opts.byStatus ?? cards.reduce<Record<string, number>>((acc, c) => {
    const s = String(c.status ?? 'todo')
    acc[s] = (acc[s] ?? 0) + 1

    return acc
  }, {})

  return {
    state: 'PASS',
    evidence: {
      board: 'evo',
      status_filter: 'all',
      cards,
      counts: {
        by_status: byStatus,
        total: cards.length,
        matching_filter: cards.length,
        in_page: cards.length,
        omitted: opts.omitted ?? 0
      },
      observed_at: now,
      has_more: opts.hasMore ?? false,
      next_cursor: opts.nextCursor ?? null,
      worker_observations: opts.workerObservations ?? [],
      worker_observation_cap: 20,
      worker_observations_capped: 0
    },
    observed_at: now
  }
}

function localBoard(): Record<string, unknown> {
  return {
    columns: [
      { name: 'triage', tasks: [] },
      { name: 'todo', tasks: [] },
      { name: 'ready', tasks: [] },
      { name: 'running', tasks: [card('t_local1', 'Local run card', 'running')] },
      { name: 'blocked', tasks: [] },
      { name: 'review', tasks: [] },
      { name: 'done', tasks: [] }
    ],
    tenants: [],
    assignees: ['evo'],
    latest_event_id: 1,
    now
  }
}

function renderBoard(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <KanbanBoardPage />
    </QueryClientProvider>
  )

  return client
}

// The card root is the draggable div; scope a worker-badge assertion to it so a
// column header or the board badge with the same word can never false-positive.
function cardFor(title: string): HTMLElement {
  return screen.getByText(title).closest('[draggable="true"]') as HTMLElement
}

beforeEach(() => {
  h.fetch.fetchBoards.mockResolvedValue({
    boards: [
      { slug: 'evo', name: 'EVO' },
      { slug: 'other', name: 'Other' }
    ],
    current: 'evo'
  })
  h.fetch.fetchOrchestration.mockResolvedValue(null)
  h.fetch.fetchProfiles.mockResolvedValue({ profiles: [] })
  h.fetch.fetchBoard.mockReset()
  h.fetch.fetchEvidenceContext.mockReset()
  h.fetch.fetchEvidenceSnapshot.mockReset()
  h.fetch.fetchEvidenceWorker.mockReset()
  h.atoms.$boardSlug.set('')
  h.atoms.$introDismissed.set(true)
  h.atoms.$collapsedLanes.set({})
  h.atoms.$lanesByProfile.set(false)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('KanbanBoardPage (mounted)', () => {
  it('never reuses cached local cards after alignment changes and the helper fails', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(false))
    h.fetch.fetchBoard.mockResolvedValue(localBoard())
    const qc = renderBoard()
    await screen.findByText('Local run card')
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue({ state: 'UNKNOWN', reason: 'helper unavailable', evidence: null })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kanban', 'evidence', 'context'] }) })
    await screen.findByText(/helper unavailable/i)
    expect(screen.queryByText('Local run card')).toBeNull()
  })

  it('aligned: renders snapshot card titles and never polls /board', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue(
      snapshotPage({
        cards: [card('t_run1', 'Run card one', 'running'), card('t_done1', 'Done card one', 'done')],
        workerObservations: [positiveObs('t_run1')]
      })
    )

    renderBoard()

    expect(await screen.findByText('Run card one')).toBeTruthy()
    expect(screen.getByText('Done card one')).toBeTruthy()

    // The aligned grid never reads the local /board.
    await waitFor(() => expect(h.fetch.fetchBoard).not.toHaveBeenCalled())
    expect(h.fetch.fetchEvidenceSnapshot).toHaveBeenCalled()
  })

  it('aligned: running badge on a positive observation, Unknown on a missing one', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue(
      snapshotPage({
        cards: [card('t_run1', 'Run card one', 'running'), card('t_done1', 'Done card one', 'done')],
        workerObservations: [positiveObs('t_run1')]
      })
    )

    renderBoard()

    await screen.findByText('Run card one')

    expect(within(cardFor('Run card one')).getByText('Running')).toBeTruthy()
    // A card absent from worker_observations is UNKNOWN, never its local status.
    expect(within(cardFor('Done card one')).getByText('Unknown')).toBeTruthy()
  })

  it('aligned: Load more appends the next page and the button disappears at exhaustion', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot
      .mockResolvedValueOnce(
        snapshotPage({
          cards: [card('t_run1', 'Run card one', 'running')],
          hasMore: true,
          nextCursor: 'v1:p2',
          omitted: 1,
          workerObservations: [positiveObs('t_run1')]
        })
      )
      .mockResolvedValueOnce(
        snapshotPage({
          cards: [card('t_done2', 'Done card two', 'done')],
          hasMore: false,
          nextCursor: null,
          omitted: 0
        })
      )

    renderBoard()

    await screen.findByText('Run card one')
    expect(screen.queryByText('Done card two')).toBeNull()

    fireEvent.click(screen.getByText('Load more'))

    expect(await screen.findByText('Done card two')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Load more')).toBeNull())
  })

  it('unaligned: renders the local board and states the choose-EVO remedy', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(false, 'local'))
    h.fetch.fetchBoard.mockResolvedValue(localBoard())

    renderBoard()

    expect(await screen.findByText('Local run card')).toBeTruthy()
    expect(screen.getByText('Choose the EVO connection')).toBeTruthy()
    // The local /board IS read for the proven-unaligned board.
    await waitFor(() => expect(h.fetch.fetchBoard).toHaveBeenCalled())
    // No snapshot read for an unaligned board.
    expect(h.fetch.fetchEvidenceSnapshot).not.toHaveBeenCalled()
  })

  it('context error: a visible error, never a silent local fallback', async () => {
    h.fetch.fetchEvidenceContext.mockRejectedValue(new Error('network down'))

    renderBoard()

    expect(await screen.findByText(/EVO identity check failed/)).toBeTruthy()
    await waitFor(() => expect(h.fetch.fetchBoard).not.toHaveBeenCalled())
  })

  it('helper unavailable: a visible error, never a silent local fallback', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue({
      state: 'FAIL',
      evidence: null,
      reason: 'helper unavailable',
      observed_at: now
    })

    renderBoard()

    expect(await screen.findByText('helper unavailable')).toBeTruthy()
    await waitFor(() => expect(h.fetch.fetchBoard).not.toHaveBeenCalled())
  })

  it('switching boards drops a late response from the previous board', async () => {
    let releaseEvoSnapshot: (v: unknown) => void = () => undefined

    const evoSnapshot = new Promise<unknown>(resolve => {
      releaseEvoSnapshot = resolve
    })

    h.fetch.fetchEvidenceContext.mockImplementation((slug: string) =>
      Promise.resolve(ctx(true, slug))
    )
    h.fetch.fetchEvidenceSnapshot.mockImplementation((slug: string) => {
      if (slug === 'evo') {
        return evoSnapshot
      }

      return Promise.resolve(
        snapshotPage({ cards: [card('t_other1', 'Other board card', 'running')] })
      )
    })

    renderBoard()

    // The default board is 'evo' (blank slug resolved from /boards.current);
    // switch to 'other' while evo's snapshot is still in flight.
    act(() => {
      h.atoms.$boardSlug.set('other')
    })

    expect(await screen.findByText('Other board card')).toBeTruthy()

    // The late evo snapshot resolves AFTER the switch; it must not paint.
    await act(async () => {
      releaseEvoSnapshot(
        snapshotPage({ cards: [card('t_run1', 'Run card one', 'running')] })
      )
      await Promise.resolve()
    })

    expect(screen.queryByText('Run card one')).toBeNull()
    expect(screen.getByText('Other board card')).toBeTruthy()
  })
})
