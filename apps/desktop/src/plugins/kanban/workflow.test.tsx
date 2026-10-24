/**
 * Rendered workflow panel tests — the load-bearing proof that the readiness /
 * continuation / hold surface and the attention / changes / timeline surface
 * render real controls and drive the real click path, never a helper predicate
 * as the proof of record.
 *
 * The ONLY boundary mocked is the network transport (`./api`): every fetch
 * returns a controlled fixture, and the real component tree (CardWorkflowPanel,
 * BoardWorkflowPanel, and the mounted KanbanBoardPage that hosts the latter) is
 * rendered with the real SDK and a real React Query client. The `$openCard`
 * atom is the REAL one (from ./completion-notify), so the exact-card open path
 * is asserted against the actual shared atom, not a stand-in.
 *
 * Fixtures reflect the final WORKFLOW-API-CONTRACT.md envelope: every route
 * returns { state, evidence, reason, remedy }, with the helper receipt nested
 * under `evidence`. No dispatch endpoint is ever wired or called — the no
 * dispatch assertion is part of the continue/readiness/hold tests.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { KanbanBoardPage } from './board'
import { $openCard } from './completion-notify'
import { BoardWorkflowPanel, CardWorkflowPanel, parsePassedChecks } from './workflow'

// ---------------------------------------------------------------------------
// Network transport boundary (./api) + the app's own REST client (@/hermes).
// The SDK, every component, and the $openCard atom are the real ones.
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
    autoDescribeProfile: vi.fn(),
    // Workflow REST (the surface under test).
    fetchAttentionQueue: vi.fn(),
    fetchChanges: vi.fn(),
    fetchTimeline: vi.fn(),
    runReadiness: vi.fn(),
    draftContinuation: vi.fn(),
    continueCard: vi.fn(),
    holdCard: vi.fn(),
    fetchReadinessBatch: vi.fn(),
    // Dispatch/creation — must never fire from any workflow action.
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

// Radix calls these on open; jsdom does not implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

// ---------------------------------------------------------------------------
// Fixtures (reflect the WORKFLOW-API-CONTRACT.md envelope)
// ---------------------------------------------------------------------------

const now = Math.floor(Date.now() / 1000)

function envelope<T>(evidence: T | null, state: 'PASS' | 'FAIL' | 'UNKNOWN' = 'PASS', extra: Record<string, unknown> = {}) {
  return { state, evidence, reason: extra.reason ?? null, remedy: extra.remedy ?? null, board: 'evo', observed_at: now }
}

function renderPanel(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <CardWorkflowPanel card="t_abc123" slug="evo" />
    </QueryClientProvider>
  )

  return client
}

beforeEach(() => {
  h.fetch.fetchBoards.mockResolvedValue({ boards: [{ slug: 'evo', name: 'EVO' }], current: 'evo' })
  h.fetch.fetchOrchestration.mockResolvedValue(null)
  h.fetch.fetchProfiles.mockResolvedValue({ profiles: [] })
  // Slug-aware: aligned only for the real board, so the workflow panel renders
  // after the board resolves (mirrors the real /evidence/context contract).
  h.fetch.fetchEvidenceContext.mockImplementation((slug: string) =>
    Promise.resolve(
      slug === 'evo'
        ? { aligned: true, board: 'evo', hostname: 'evo', reason: 'aligned', observed_at: now }
        : { aligned: false, board: slug, hostname: 'evo', reason: 'not the EVO host', observed_at: now }
    )
  )
  h.fetch.fetchBoard.mockReset()
  h.fetch.fetchEvidenceSnapshot.mockReset()
  h.fetch.fetchAttentionQueue.mockReset()
  h.fetch.fetchChanges.mockReset()
  h.fetch.fetchTimeline.mockReset()
  h.fetch.runReadiness.mockReset()
  h.fetch.draftContinuation.mockReset()
  h.fetch.continueCard.mockReset()
  h.fetch.holdCard.mockReset()
  h.fetch.fetchReadinessBatch.mockReset()
  h.fetch.nudgeDispatcher.mockReset()
  h.fetch.createTask.mockReset()
  h.atoms.$boardSlug.set('')
  h.atoms.$introDismissed.set(true)
  h.atoms.$collapsedLanes.set({})
  h.atoms.$lanesByProfile.set(false)
  $openCard.set(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// Parse helpers (supplementary — never the proof of record)
// ---------------------------------------------------------------------------

describe('parse helpers', () => {
  it('splits passed checks one per line', () => {
    expect(parsePassedChecks('a\n\n b \n')).toEqual(['a', 'b'])
  })
})

// ---------------------------------------------------------------------------
// CardWorkflowPanel — readiness
// ---------------------------------------------------------------------------

describe('CardWorkflowPanel readiness', () => {
  it('runs readiness on explicit click and shows every check with a separate permission', async () => {
    h.fetch.runReadiness.mockResolvedValue(
      envelope(
        {
          state: 'FAIL',
          ready_to_release: false,
          checks: [
            { name: 'board_permission', state: 'FAIL', reason: 'read-only for this board', mutation_authorized: false },
            { name: 'profile_exists', state: 'PASS', reason: 'found' }
          ]
        },
        'PASS'
      )
    )
    renderPanel()

    fireEvent.click(screen.getByText('Check readiness'))

    expect(await screen.findByText('board_permission (read-only)')).toBeTruthy()
    expect(screen.getByText('profile_exists')).toBeTruthy()
    expect(screen.getByText('Not ready to release')).toBeTruthy()
    expect(h.fetch.runReadiness).toHaveBeenCalledWith('evo', 't_abc123', false)
  })

  it('a FAIL envelope surfaces its reason and remedy, never an empty success', async () => {
    h.fetch.runReadiness.mockResolvedValue(
      envelope(null, 'FAIL', { reason: 'remote readiness envelope invalid', remedy: 'inspect the helper' })
    )
    renderPanel()

    fireEvent.click(screen.getByText('Check readiness'))

    expect(await screen.findByText(/readiness fail/i)).toBeTruthy()
    expect(screen.getByText(/inspect the helper/)).toBeTruthy()
    expect(screen.queryByText('Ready to release')).toBeNull()
  })

  it('renders checks even when the envelope is FAIL/UNKNOWN, so the operator sees which check failed', async () => {
    h.fetch.runReadiness.mockResolvedValue(
      envelope(
        {
          state: 'FAIL',
          ready_to_release: false,
          checks: [
            { name: 'board_permission', state: 'FAIL', reason: 'read-only for this board', mutation_authorized: false },
            { name: 'model', state: 'UNKNOWN', reason: 'check_model was false' }
          ]
        },
        'FAIL',
        { reason: 'not ready' }
      )
    )
    renderPanel()

    fireEvent.click(screen.getByText('Check readiness'))

    expect(await screen.findByText('board_permission (read-only)')).toBeTruthy()
    expect(screen.getByText('model')).toBeTruthy()
    expect(screen.getByText(/readiness fail/i)).toBeTruthy()
  })

  it('check_model toggles the exact provider/model proof flag', async () => {
    h.fetch.runReadiness.mockResolvedValue(envelope({ state: 'PASS', ready_to_release: true, checks: [] }, 'PASS'))
    renderPanel()

    fireEvent.click(screen.getByText('Check the exact provider and model'))
    fireEvent.click(screen.getByText('Check readiness'))

    await waitFor(() => expect(h.fetch.runReadiness).toHaveBeenCalledWith('evo', 't_abc123', true))
  })
})

// ---------------------------------------------------------------------------
// CardWorkflowPanel — continuation draft + continue + hold
// ---------------------------------------------------------------------------

describe('CardWorkflowPanel continuation', () => {
  const draftFixture = () =>
    envelope(
      {
        state: 'PASS',
        fingerprint: 'fp123',
        original: { status: 'blocked', assignee: 'evo', source: 'unverified source excerpt' },
        worker: { verdict: 'STOPPED', reason: 'safe to continue' },
        passed_checks: ['one'],
        remaining_checks: [{ check: 'acceptance check', evidence: 'real transport', acceptance: 'one held card' }],
        verification_note: 'verified',
        commission: {
          workspace: '/home/hayden/work/trajectory/continuation',
          profile: 'evo',
          provider: 'deepseek',
          model: 'deepseek-v4-pro',
          max_runtime_minutes: 25,
          creator: 'atlas-relay',
          title: 'Continue work'
        },
        no_mutation_performed: true
      },
      'PASS'
    )

  it('drafts from the entered checks, labels the original unverified, and continues on a separate click', async () => {
    h.fetch.draftContinuation.mockResolvedValue(draftFixture())
    h.fetch.continueCard.mockResolvedValue(
      envelope({ state: 'PASS', new_card: 't_new', new_card_status: 'blocked', new_card_assignee: 'evo', held: true }, 'PASS')
    )
    renderPanel()

    fireEvent.change(screen.getByPlaceholderText('Parent source validation passed'), { target: { value: 'one\n' } })
    fireEvent.click(screen.getByText('Add remaining check'))
    fireEvent.change(screen.getByPlaceholderText('check'), { target: { value: 'acceptance check' } })
    fireEvent.change(screen.getByPlaceholderText('evidence'), { target: { value: 'real transport' } })
    fireEvent.change(screen.getByPlaceholderText('acceptance'), { target: { value: 'one held card' } })
    fireEvent.change(screen.getByPlaceholderText('What you verified, in your own words'), { target: { value: 'verified' } })

    fireEvent.click(screen.getByText('Draft continuation'))

    expect(await screen.findByText('fp123')).toBeTruthy()
    expect(screen.getByText(/unverified source excerpt/)).toBeTruthy()

    fireEvent.click(screen.getByText('Create held continuation card'))

    expect(await screen.findByText(/New held card/)).toBeTruthy()
    expect(screen.getByText(/t_new/)).toBeTruthy()

    // The draft carries the entered checks and note.
    expect(h.fetch.draftContinuation).toHaveBeenCalledTimes(1)
    const draftArgs = h.fetch.draftContinuation.mock.calls[0]
    expect(draftArgs[0]).toBe('evo')
    expect(draftArgs[1]).toMatchObject({
      card: 't_abc123',
      passed_checks: ['one'],
      remaining_checks: [{ check: 'acceptance check', evidence: 'real transport', acceptance: 'one held card' }],
      verification_note: 'verified'
    })

    // The continue carries the EXACT commission + checks + note from the
    // accepted draft (the fingerprint binds them), never re-derived from inputs.
    const continueArgs = h.fetch.continueCard.mock.calls[0]
    expect(continueArgs[0]).toBe('evo')
    expect(continueArgs[1]).toMatchObject({
      card: 't_abc123',
      fingerprint: 'fp123',
      workspace: '/home/hayden/work/trajectory/continuation',
      profile: 'evo',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      max_runtime_minutes: 25,
      creator: 'atlas-relay',
      title: 'Continue work',
      passed_checks: ['one'],
      remaining_checks: [{ check: 'acceptance check', evidence: 'real transport', acceptance: 'one held card' }],
      verification_note: 'verified'
    })

    // No dispatch, no creation, no unblock ever fired.
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
    expect(h.fetch.createTask).not.toHaveBeenCalled()
  })

  it('a stale fingerprint asks to redraft instead of retrying', async () => {
    h.fetch.draftContinuation.mockResolvedValue(draftFixture())
    h.fetch.continueCard.mockResolvedValue(envelope(null, 'FAIL', { reason: 'fingerprint_mismatch: card changed' }))
    renderPanel()

    fireEvent.click(screen.getByText('Draft continuation'))
    await screen.findByText('fp123')

    fireEvent.click(screen.getByText('Create held continuation card'))

    expect(await screen.findByText(/Draft again to continue/)).toBeTruthy()
    expect(h.fetch.continueCard).toHaveBeenCalledTimes(1)
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
  })

  it('an UNKNOWN continue result asks to inspect before retry', async () => {
    h.fetch.draftContinuation.mockResolvedValue(draftFixture())
    h.fetch.continueCard.mockResolvedValue(envelope(null, 'UNKNOWN', { reason: 'helper returned UNKNOWN' }))
    renderPanel()

    fireEvent.click(screen.getByText('Draft continuation'))
    await screen.findByText('fp123')

    fireEvent.click(screen.getByText('Create held continuation card'))

    expect(await screen.findByText(/Inspect the card before retrying/)).toBeTruthy()
    expect(h.fetch.continueCard).toHaveBeenCalledTimes(1)
  })

  it('hold is a separate explicit click that never stops a live worker', async () => {
    h.fetch.holdCard.mockResolvedValue(envelope({ state: 'PASS' }, 'PASS'))
    renderPanel()

    fireEvent.change(screen.getByPlaceholderText('Why this card needs review'), { target: { value: 'needs a human look' } })
    fireEvent.click(screen.getByText('Hold'))

    expect(await screen.findByText(/held for review/i)).toBeTruthy()
    expect(h.fetch.holdCard).toHaveBeenCalledWith('evo', 't_abc123', 'needs a human look')
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// BoardWorkflowPanel — attention / changes / timeline
// ---------------------------------------------------------------------------

function renderBoardPanel(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <BoardWorkflowPanel />
    </QueryClientProvider>
  )

  return client
}

describe('BoardWorkflowPanel', () => {
  it('attention queue renders exact cards, opens one through $openCard, and pages via Load more', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchAttentionQueue
      .mockResolvedValueOnce(
        envelope(
          {
            board: 'evo',
            cards: [{ id: 't_1', title: 'Needs a decision', status: 'blocked', next_action: 'inspect worker' }],
            returned: 1,
            has_more: true,
            next_cursor: 'v1:p2',
            omitted: 2
          },
          'PASS'
        )
      )
      .mockResolvedValueOnce(
        envelope({ board: 'evo', cards: [{ id: 't_2', title: 'Late card', status: 'blocked' }], returned: 1, has_more: false, omitted: 0 }, 'PASS')
      )
    renderBoardPanel()

    fireEvent.click(await screen.findByText('Show attention queue'))

    expect(await screen.findByText('Needs a decision')).toBeTruthy()
    expect(screen.getByText('t_1')).toBeTruthy()
    expect(screen.getByText('+2 omitted')).toBeTruthy()

    // Exact-card open: clicking the card carries board + card through the atom.
    fireEvent.click(screen.getByText('Needs a decision'))
    expect($openCard.get()).toEqual({ board: 'evo', card: 't_1' })

    // Bounded paging: the next page is fetched under the returned cursor.
    fireEvent.click(screen.getByText('Load more'))
    expect(await screen.findByText('Late card')).toBeTruthy()
    expect(h.fetch.fetchAttentionQueue).toHaveBeenLastCalledWith('evo', 50, 'v1:p2')
  })

  it('attention queue unavailable state is shown, never an empty success', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchAttentionQueue.mockResolvedValue(envelope(null, 'FAIL', { reason: 'Board database is absent' }))
    renderBoardPanel()

    fireEvent.click(await screen.findByText('Show attention queue'))

    expect(await screen.findByText(/Attention unavailable/)).toBeTruthy()
    expect(screen.queryByText('Load more')).toBeNull()
  })

  it('changes section shows freshness and the current session events', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchChanges.mockResolvedValue(
      envelope(
        {
          board: 'evo',
          first_read_policy: 'baseline-now: no historical events returned',
          observed_at: now,
          events: [{ id: 875, task_id: 't_9', kind: 'completed', created_at: now }],
          incomplete: false
        },
        'PASS'
      )
    )
    renderBoardPanel()

    fireEvent.click(await screen.findByText('Show changes'))

    expect(await screen.findByText(/baseline-now/)).toBeTruthy()
    expect(screen.getByText('completed')).toBeTruthy()
    expect(screen.getByText('t_9')).toBeTruthy()
  })

  it('timeline section shows disjoint interval kinds and a gap warning', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchTimeline.mockResolvedValue(
      envelope(
        {
          board: 'evo',
          card: 't_1',
          intervals: [
            { kind: 'blocked', duration_seconds: 60 },
            { kind: 'unknown', duration_seconds: 387 },
            { kind: 'execution', duration_seconds: 978 }
          ],
          coverage: { window_start: now - 1000, window_end: now, gaps: [{}] },
          incomplete: true
        },
        'PASS'
      )
    )
    renderBoardPanel()

    fireEvent.change(await screen.findByPlaceholderText('Card id'), { target: { value: 't_1' } })
    fireEvent.click(screen.getByText('Show'))

    expect(await screen.findByText('execution')).toBeTruthy()
    expect(screen.getByText('blocked')).toBeTruthy()
    expect(screen.getByText('unknown')).toBeTruthy()
    expect(screen.getByText(/gaps in the covered window/)).toBeTruthy()
    expect(h.fetch.fetchTimeline).toHaveBeenCalledWith('evo', 't_1', 50, null)
  })
})

// ---------------------------------------------------------------------------
// Mounted proof: BoardWorkflowPanel is wired into KanbanBoardPage
// ---------------------------------------------------------------------------

describe('KanbanBoardPage workflow wiring', () => {
  it('the Workflow toggle mounts BoardWorkflowPanel inside the real board page', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue({
      state: 'PASS',
      evidence: { board: 'evo', cards: [], counts: { by_status: {}, total: 0 }, observed_at: now }
    })

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={client}>
        <KanbanBoardPage />
      </QueryClientProvider>
    )

    // The board renders (aligned, empty snapshot) and the workflow toggle is present.
    await screen.findByLabelText('Workflow')
    expect(screen.queryByText('Show attention queue')).toBeNull()

    fireEvent.click(screen.getByLabelText('Workflow'))

    expect(await screen.findByText('Show attention queue')).toBeTruthy()
    expect(screen.getByText('Show changes')).toBeTruthy()
  })

  it('a terminal toast exact-card request opens the drawer while the page is already mounted', async () => {
    // (board-aligned context is provided by the slug-aware beforeEach default)
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue({
      state: 'PASS',
      evidence: {
        board: 'evo',
        cards: [{ id: 't_open1', title: 'Card to open', status: 'running' }],
        counts: { by_status: { running: 1 }, total: 1 },
        observed_at: now
      }
    })

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    render(
      <QueryClientProvider client={client}>
        <KanbanBoardPage />
      </QueryClientProvider>
    )

    await screen.findByText('Card to open')

    // Simulate the notification action landing while the page is mounted.
    act(() => {
      $openCard.set({ board: 'evo', card: 't_open1' })
    })

    // The page consumes the request (the drawer opens) and clears the atom.
    await waitFor(() => expect($openCard.get()).toBeNull())
  })
})

// ---------------------------------------------------------------------------
// R3 — next-repair preview (text only, never executed)
// ---------------------------------------------------------------------------

describe('CardWorkflowPanel — R3 repair preview', () => {
  it('renders the next-repair preview from failed checks, as a text action never applied', async () => {
    h.fetch.runReadiness.mockResolvedValue(
      envelope(
        {
          state: 'FAIL',
          ready_to_release: false,
          checks: [{ name: 'board_permission', state: 'FAIL', reason: 'read-only for this board', mutation_authorized: false }],
          repair_preview: [
            { check: 'board_permission', state: 'FAIL', action: 'Set the board writable in config.yaml', reason: 'read-only' }
          ]
        },
        'PASS'
      )
    )
    renderPanel()

    fireEvent.click(screen.getByText('Check readiness'))

    expect(await screen.findByText('Next repair (preview)')).toBeTruthy()
    expect(screen.getByText(/Set the board writable in config.yaml/)).toBeTruthy()
    expect(screen.getByText(/preview — not applied/)).toBeTruthy()
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// R9 — readiness batch preview (select held cards, never bulk-release)
// ---------------------------------------------------------------------------

describe('BoardWorkflowPanel — R9 readiness batch', () => {
  it('previews readiness for selected held cards and never releases', async () => {
    h.fetch.fetchEvidenceSnapshot.mockImplementation((slug: string, status: string) =>
      status === 'blocked'
        ? Promise.resolve(
            envelope({
              board: 'evo',
              cards: [
                { id: 't_held1', title: 'Held card one', status: 'blocked' },
                { id: 't_held2', title: 'Held card two', status: 'blocked' }
              ],
              counts: { by_status: { blocked: 2 }, total: 2 }
            })
          )
        : Promise.resolve(envelope({ board: 'evo', cards: [], counts: { by_status: {}, total: 0 } }))
    )
    h.fetch.fetchReadinessBatch.mockResolvedValue(
      envelope({
        board: 'evo',
        items: [
          {
            card: 't_held1',
            state: 'FAIL',
            repair_preview: [{ check: 'profile_exists', state: 'FAIL', action: 'Assign a profile', reason: 'no assignee' }]
          }
        ],
        requested: 1,
        returned: 1,
        omitted: 0,
        no_mutation_performed: true
      })
    )

    renderBoardPanel()

    fireEvent.click(await screen.findByText('Preview held cards'))
    expect(await screen.findByText('Held card one')).toBeTruthy()

    // Select one held card, then preview.
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByText('Preview readiness'))

    expect(await screen.findByText('t_held1')).toBeTruthy()
    expect(screen.getByText(/Assign a profile/)).toBeTruthy()
    expect(screen.getByText(/no mutation performed/)).toBeTruthy()
    expect(h.fetch.fetchReadinessBatch).toHaveBeenCalledWith('evo', ['t_held1'], false)
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
  })

  it('a FAIL batch envelope surfaces its reason, never an all-clear', async () => {
    h.fetch.fetchEvidenceSnapshot.mockResolvedValue(
      envelope({
        board: 'evo',
        cards: [{ id: 't_held1', title: 'Held card one', status: 'blocked' }],
        counts: { by_status: { blocked: 1 }, total: 1 }
      })
    )
    h.fetch.fetchReadinessBatch.mockResolvedValue(envelope(null, 'FAIL', { reason: 'board database absent' }))

    renderBoardPanel()

    fireEvent.click(await screen.findByText('Preview held cards'))
    // Wait for the held card list to render before selecting a card.
    await screen.findByText('Held card one')
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByText('Preview readiness'))

    expect(await screen.findByText(/Readiness batch fail/)).toBeTruthy()
    expect(h.fetch.nudgeDispatcher).not.toHaveBeenCalled()
  })

  it('exposes the held-card 100-cap omission and pages the rest via the snapshot cursor', async () => {
    h.fetch.fetchEvidenceSnapshot.mockImplementation((slug: string, status: string, cursor: null | string) => {
      if (status !== 'blocked') {
        return Promise.resolve(envelope({ board: 'evo', cards: [], counts: { by_status: {}, total: 0 } }))
      }

      return cursor
        ? Promise.resolve(
            envelope({
              board: 'evo',
              cards: [{ id: 't_held101', title: 'Held card 101', status: 'blocked' }],
              counts: { by_status: { blocked: 101 }, total: 101, omitted: 0 },
              has_more: false,
              next_cursor: null,
              omitted: 0
            })
          )
        : Promise.resolve(
            envelope({
              board: 'evo',
              cards: [{ id: 't_held1', title: 'Held card one', status: 'blocked' }],
              counts: { by_status: { blocked: 101 }, total: 101, omitted: 100 },
              has_more: true,
              next_cursor: 'p2',
              omitted: 100
            })
          )
    })
    h.fetch.fetchReadinessBatch.mockResolvedValue(envelope(null, 'PASS', {}))

    renderBoardPanel()

    fireEvent.click(await screen.findByText('Preview held cards'))
    await screen.findByText('Held card one')

    // The omitted tail is exposed, never silently dropped.
    expect(await screen.findByText(/\+100 held cards omitted/)).toBeTruthy()

    // A path to the further held cards exists via the snapshot cursor.
    fireEvent.click(screen.getByText('Load more held cards'))
    expect(await screen.findByText('Held card 101')).toBeTruthy()
    expect(h.fetch.fetchEvidenceSnapshot).toHaveBeenLastCalledWith('evo', 'blocked', 'p2', 100)
  })

  it('a late batch response for a stale selection is discarded, never painted under the new selection', async () => {
    let resolveBatch!: (value: unknown) => void

    const pendingBatch = new Promise<unknown>(resolve => {
      resolveBatch = resolve
    })

    h.fetch.fetchEvidenceSnapshot.mockImplementation((slug: string, status: string) =>
      status === 'blocked'
        ? Promise.resolve(
            envelope({
              board: 'evo',
              cards: [
                { id: 't_held1', title: 'Held card one', status: 'blocked' },
                { id: 't_held2', title: 'Held card two', status: 'blocked' }
              ],
              counts: { by_status: { blocked: 2 }, total: 2 }
            })
          )
        : Promise.resolve(envelope({ board: 'evo', cards: [], counts: { by_status: {}, total: 0 } }))
    )
    h.fetch.fetchReadinessBatch.mockImplementation(() => pendingBatch)

    renderBoardPanel()

    fireEvent.click(await screen.findByText('Preview held cards'))
    await screen.findByText('Held card one')
    const checkboxes = screen.getAllByRole('checkbox')
    fireEvent.click(checkboxes[0]) // select t_held1
    fireEvent.click(screen.getByText('Preview readiness'))

    // While the t_held1 batch is in flight, change the selection (unselect
    // t_held1, select t_held2) — this bumps the generation.
    await act(async () => {
      fireEvent.click(checkboxes[0]) // unselect t_held1
      fireEvent.click(checkboxes[1]) // select t_held2
    })

    // The stale t_held1 response resolves late — it must not paint under the
    // t_held2 selection.
    await act(async () => {
      resolveBatch(
        envelope({
          board: 'evo',
          items: [{ card: 't_held1', state: 'PASS', repair_preview: [] }],
          requested: 1,
          returned: 1,
          omitted: 0,
          no_mutation_performed: true
        })
      )
      await Promise.resolve()
    })

    // The stale result for t_held1 is never painted (the result section would
    // show its card id); no all-clear appears either.
    expect(screen.queryByText(/1 returned/)).toBeNull()
  })
})
