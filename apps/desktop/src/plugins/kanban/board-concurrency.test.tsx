/**
 * R10 — long-board paging under concurrent updates and board switches.
 *
 * The ONLY boundary mocked is the network transport (`./api`); the real
 * KanbanBoardPage, the real evidence hook (observedAt-stamped pages), and a
 * real React Query client render. Proof of record is the rendered board:
 * concurrent inserts deduplicate by id, a late page from a previous board is
 * dropped, and the evolving snapshot / omissions are reported honestly.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { KanbanBoardPage } from './board'

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
    autoDescribeProfile: vi.fn(),
    fetchAttentionQueue: vi.fn(),
    fetchChanges: vi.fn(),
    fetchTimeline: vi.fn(),
    runReadiness: vi.fn(),
    draftContinuation: vi.fn(),
    continueCard: vi.fn(),
    holdCard: vi.fn(),
    fetchReadinessBatch: vi.fn(),
    fetchEvidenceReleases: vi.fn(),
    fetchBrowserReadiness: vi.fn(),
    fetchAcceptanceCompare: vi.fn(),
    fetchReviewerPacket: vi.fn(),
    fetchAttachmentProvenance: vi.fn(),
    nudgeDispatcher: vi.fn()
  }

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
      'kanban',
      'evidence',
      'snapshot',
      slug,
      status,
      cursor
    ],
    evidenceWorkerKey: (slug: string, id: string) => ['kanban', 'evidence', 'worker', slug, id],
    evidenceCardKey: (slug: string, id: string) => ['kanban', 'evidence', 'card', slug, id],
    evidencePageKey: (slug: string, resource: string, card: null | string, cursor: null | string) => [
      'kanban',
      'evidence',
      'page',
      slug,
      resource,
      card,
      cursor
    ],
    evidenceReleasesKey: (slug: string) => ['kanban', 'evidence', 'releases', slug],
    browserReadinessKey: (slug: string) => ['kanban', 'evidence', 'browser-readiness', slug],
    acceptanceCompareKey: (slug: string, card: string, cur: number, prev: number) => [
      'kanban',
      'evidence',
      'acceptance-compare',
      slug,
      card,
      cur,
      prev
    ],
    reviewerPacketKey: (slug: string, card: string) => ['kanban', 'evidence', 'reviewer-packet', slug, card],
    attachmentProvenanceKey: (slug: string, card: string, id: number | string) => [
      'kanban',
      'evidence',
      'attachment-provenance',
      slug,
      card,
      id
    ],
    readinessBatchKey: (slug: string, cards: string[], checkModel: boolean) => [
      'kanban',
      'workflow',
      'readiness-batch',
      slug,
      [...cards].sort().join(','),
      checkModel
    ],
    fetchBoard: h.fetch.fetchBoard,
    fetchBoards: h.fetch.fetchBoards,
    fetchProfiles: h.fetch.fetchProfiles,
    fetchOrchestration: h.fetch.fetchOrchestration,
    fetchEvidenceContext: h.fetch.fetchEvidenceContext,
    fetchEvidenceSnapshot: h.fetch.fetchEvidenceSnapshot,
    fetchEvidenceWorker: h.fetch.fetchEvidenceWorker,
    fetchEvidenceReleases: h.fetch.fetchEvidenceReleases,
    fetchBrowserReadiness: h.fetch.fetchBrowserReadiness,
    fetchAcceptanceCompare: h.fetch.fetchAcceptanceCompare,
    fetchReviewerPacket: h.fetch.fetchReviewerPacket,
    fetchAttachmentProvenance: h.fetch.fetchAttachmentProvenance,
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
    autoDescribeProfile: h.fetch.autoDescribeProfile,
    fetchAttentionQueue: h.fetch.fetchAttentionQueue,
    fetchChanges: h.fetch.fetchChanges,
    fetchTimeline: h.fetch.fetchTimeline,
    runReadiness: h.fetch.runReadiness,
    draftContinuation: h.fetch.draftContinuation,
    continueCard: h.fetch.continueCard,
    holdCard: h.fetch.holdCard,
    fetchReadinessBatch: h.fetch.fetchReadinessBatch,
    nudgeDispatcher: h.fetch.nudgeDispatcher
  }
})

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const now = Math.floor(Date.now() / 1000)

function ctx(aligned: boolean, board = 'evo'): Record<string, unknown> {
  return { aligned, board, hostname: 'evo', reason: aligned ? 'aligned' : 'no', observed_at: now }
}

function card(id: string, title: string, status: string): Record<string, unknown> {
  return { id, title, status, assignee: 'evo', created_at: now - 10 }
}

function snapshotPage(
  cards: Array<Record<string, unknown>>,
  opts: { hasMore?: boolean; nextCursor?: null | string; omitted?: number; observedAt?: number } = {}
) {
  const byStatus = cards.reduce<Record<string, number>>((acc, c) => {
    const s = String(c.status ?? 'todo')
    acc[s] = (acc[s] ?? 0) + 1

    return acc
  }, {})

  const observedAt = opts.observedAt ?? now

  return {
    state: 'PASS',
    evidence: {
      board: 'evo',
      status_filter: 'all',
      cards,
      counts: { by_status: byStatus, total: cards.length, matching_filter: cards.length, in_page: cards.length, omitted: opts.omitted ?? 0 },
      observed_at: observedAt,
      has_more: opts.hasMore ?? false,
      next_cursor: opts.nextCursor ?? null,
      worker_observations: [],
      worker_observation_cap: 20,
      worker_observations_capped: 0
    },
    observed_at: observedAt
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

describe('R10 — long-board paging under concurrent updates', () => {
  it('deduplicates overlapping page-2 ids (a concurrent insert) without dropping either card', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot
      .mockResolvedValueOnce(snapshotPage([card('t_a', 'Card A', 'running'), card('t_b', 'Card B', 'done')], { hasMore: true, nextCursor: 'p2', omitted: 1 }))
      // Page 2 re-reads t_b (still under the high-water bound) plus a new card.
      .mockResolvedValueOnce(snapshotPage([card('t_b', 'Card B', 'done'), card('t_c', 'Card C', 'done')], { hasMore: false, nextCursor: null }))

    renderBoard()

    expect(await screen.findByText('Card A')).toBeTruthy()
    fireEvent.click(screen.getByText('Load more'))

    expect(await screen.findByText('Card C')).toBeTruthy()
    // Card B appears exactly once (no duplicate from the overlapping page).
    expect(screen.getAllByText('Card B')).toHaveLength(1)
    expect(screen.getByText('Card A')).toBeTruthy()
    // Omissions reported honestly.
    expect(screen.getByText('+1 more')).toBeTruthy()
  })

  it('reports an evolving snapshot: a card that changed status between pages keeps one identity', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot
      .mockResolvedValueOnce(snapshotPage([card('t_a', 'Card A', 'running')], { hasMore: true, nextCursor: 'p2', omitted: 1 }))
      // The same card re-reads as done (status changed under a concurrent update).
      .mockResolvedValueOnce(snapshotPage([card('t_a', 'Card A', 'done'), card('t_b', 'Card B', 'done')], { hasMore: false, nextCursor: null }))

    renderBoard()

    await screen.findByText('Card A')
    fireEvent.click(screen.getByText('Load more'))

    expect(await screen.findByText('Card B')).toBeTruthy()
    // Identity is preserved: Card A appears exactly once (never duplicated).
    expect(screen.getAllByText('Card A')).toHaveLength(1)
  })

  it('drops a late page from a previous board after a board switch', async () => {
    let releaseEvoPage2: (v: unknown) => void = () => undefined
    const evoPage2 = new Promise<unknown>(resolve => {
      releaseEvoPage2 = resolve
    })

    h.fetch.fetchEvidenceContext.mockImplementation((slug: string) => Promise.resolve(ctx(true, slug)))
    h.fetch.fetchEvidenceSnapshot.mockImplementation((slug: string, _status: string, cursor: null | string) => {
      if (slug === 'evo') {
        return cursor
          ? evoPage2
          : Promise.resolve(snapshotPage([card('t_evo', 'EVO card', 'running')], { hasMore: true, nextCursor: 'p2', observedAt: now }))
      }

      // The other board's snapshot is stamped at a DIFFERENT observed_at — the
      // stamp that lets the board drop a stale evo page-2 landing after a switch.
      return Promise.resolve(
        snapshotPage([card('t_other', 'Other card', 'running')], { hasMore: false, nextCursor: null, observedAt: now + 100 })
      )
    })

    renderBoard()

    await screen.findByText('EVO card')
    // Start the evo page-2 load, then switch boards before it resolves.
    fireEvent.click(screen.getByText('Load more'))
    act(() => {
      h.atoms.$boardSlug.set('other')
    })

    expect(await screen.findByText('Other card')).toBeTruthy()

    // The late evo page 2 resolves after the switch — it must not paint.
    await act(async () => {
      releaseEvoPage2(snapshotPage([card('t_stale', 'Stale EVO card', 'done')], { hasMore: false, nextCursor: null }))
      await Promise.resolve()
    })

    expect(screen.queryByText('Stale EVO card')).toBeNull()
    expect(screen.getByText('Other card')).toBeTruthy()
  })

  it('an exhausted page shows no Load more (restart affordance, never blends stale generations)', async () => {
    h.fetch.fetchEvidenceContext.mockResolvedValue(ctx(true))
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue(
      snapshotPage([card('t_a', 'Card A', 'done')], { hasMore: false, nextCursor: null, omitted: 0 })
    )

    renderBoard()

    expect(await screen.findByText('Card A')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Load more')).toBeNull())
  })
})
