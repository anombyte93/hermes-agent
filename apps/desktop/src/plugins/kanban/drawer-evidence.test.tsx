/**
 * Drawer evidence — the ACTUAL TaskDrawer, actual SDK and actual React Query,
 * with ONLY the network boundary (`./api`) mocked and the OS download boundary
 * faked. No SDK/components/hooks/React Query are mocked: the real drawer mounts,
 * resolves the real detail + evidence queries, and paints real rows. The proof
 * of record is rendered output, not a helper predicate.
 *
 * The `@/hermes` mock is the same seam model-override.test.tsx uses (the
 * electron bridge is not a component/hook/query); the SDK itself loads for real.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, evidenceContextKey } from './api'
import { TaskDrawer } from './drawer'

// ── electron bridge (reuse model-override.test.tsx setup) ────────────────────
const getGlobalModelOptions = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  setApiRequestProfile: vi.fn()
}))

// ── network boundary only; the real ./api keeps its atoms + query keys ───────
const apiMock = vi.hoisted(() => ({
  fetchBoards: vi.fn(),
  fetchEvidenceContext: vi.fn(),
  fetchEvidenceWorker: vi.fn(),
  fetchTask: vi.fn(),
  fetchTaskWithoutHistory: vi.fn(),
  fetchLog: vi.fn(),
  fetchProfiles: vi.fn(),
  fetchOrchestration: vi.fn(),
  fetchEvidencePage: vi.fn(),
  fetchAttachmentDownload: vi.fn()
}))

vi.mock('./api', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()

  return {
    ...actual,
    fetchBoards: apiMock.fetchBoards,
    fetchEvidenceContext: apiMock.fetchEvidenceContext,
    fetchEvidenceWorker: apiMock.fetchEvidenceWorker,
    fetchTask: apiMock.fetchTask,
    fetchTaskWithoutHistory: apiMock.fetchTaskWithoutHistory,
    fetchLog: apiMock.fetchLog,
    fetchProfiles: apiMock.fetchProfiles,
    fetchOrchestration: apiMock.fetchOrchestration,
    fetchEvidencePage: apiMock.fetchEvidencePage,
    fetchAttachmentDownload: apiMock.fetchAttachmentDownload
  }
})

// ── fixtures ──────────────────────────────────────────────────────────────────
const alignedContext = { aligned: true, board: 'evo', hostname: 'evo', reason: 'aligned', observed_at: 1 }

const detailFor = (id: string, title: string) => ({
  task: { id, title, status: 'running', assignee: 'evo', body: null, created_at: 1000 },
  comments: [],
  events: [],
  attachments: [],
  links: { parents: [], children: [] },
  runs: []
})

const emptyPage = () => ({
  state: 'PASS',
  evidence: { items: [], returned: 0, has_more: false, next_cursor: null, total: 0, omitted: 0 },
  reason: null
})

const page = (items: Array<Record<string, unknown>>, hasMore: boolean, nextCursor: null | string) => ({
  state: 'PASS',
  evidence: { items, returned: items.length, has_more: hasMore, next_cursor: nextCursor, total: items.length, omitted: 0 },
  reason: null
})

const run = (id: number, summary: string) => ({
  id,
  task_id: 't_1',
  profile: 'evo',
  status: 'completed',
  outcome: 'completed',
  summary,
  error: null,
  started_at: 100,
  ended_at: 200
})

const event = (id: number, kind: string) => ({ id, task_id: 't_1', kind, payload: {}, created_at: 100, run_id: null })

const attachment = (id: number, filename: string) => ({
  id,
  task_id: 't_1',
  filename,
  content_type: 'text/plain',
  size: 12,
  uploaded_by: 'evo',
  stored_path: null,
  created_at: 100
})

const deferred = <T,>() => {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, resolve, reject }
}

// ── OS download boundary fakes ────────────────────────────────────────────────
let capturedBlob: Blob | null = null
let clickedAnchor: HTMLAnchorElement | null = null

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()

  URL.createObjectURL = vi.fn((blob: Blob) => {
    capturedBlob = blob

    return 'blob:mock-url'
  }) as unknown as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL

  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clickedAnchor = this
  })
})

beforeEach(() => {
  capturedBlob = null
  clickedAnchor = null
  $boardSlug.set('evo')

  apiMock.fetchBoards.mockResolvedValue({ boards: [], current: 'evo' })
  apiMock.fetchEvidenceContext.mockResolvedValue(alignedContext)
  apiMock.fetchEvidenceWorker.mockResolvedValue({
    state: 'PASS',
    evidence: { task_id: 't_1', observations: [], aggregate: null }
  })
  apiMock.fetchTask.mockImplementation((id: string) => Promise.resolve(detailFor(id, 'Legacy')))
  apiMock.fetchTaskWithoutHistory.mockImplementation((_slug: string, id: string) => Promise.resolve(detailFor(id, 'Aligned')))
  apiMock.fetchLog.mockResolvedValue({ exists: false, size_bytes: 0, content: '', truncated: false })
  apiMock.fetchProfiles.mockResolvedValue({ profiles: [] })
  apiMock.fetchOrchestration.mockResolvedValue({ default_assignee: '' })
  apiMock.fetchEvidencePage.mockResolvedValue(emptyPage())
  apiMock.fetchAttachmentDownload.mockResolvedValue({
    id: 1,
    filename: 'report.txt',
    content_type: 'text/plain',
    size: 6,
    content_base64: btoa('hello!')
  })
  getGlobalModelOptions.mockResolvedValue({ providers: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// ── helpers ───────────────────────────────────────────────────────────────────
function renderDrawer(id = 't_1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } })

  const utils = render(
    <QueryClientProvider client={client}>
      <TaskDrawer columns={['ready', 'running', 'done']} id={id} onClose={vi.fn()} onOpen={vi.fn()} />
    </QueryClientProvider>
  )

  return { ...utils, client }
}

// ── the real, aligned drawer ──────────────────────────────────────────────────
describe('TaskDrawer — aligned EVO history', () => {
  it('does not materialise legacy history while aligned (no fetchTask)', async () => {
    renderDrawer()

    await waitFor(() => expect(apiMock.fetchTaskWithoutHistory).toHaveBeenCalledWith('evo', 't_1'))
    await waitFor(() => expect(apiMock.fetchEvidenceContext).toHaveBeenCalled())

    expect(apiMock.fetchTask).not.toHaveBeenCalled()
  })

  it('renders real run/event/attachment rows across page 2', async () => {
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource, _card, cursor) => {
      if (resource === 'runs') {
        return cursor
          ? Promise.resolve(page([run(3, 'third run summary')], false, null))
          : Promise.resolve(page([run(1, 'first run summary'), run(2, 'second run summary')], true, 'runs:2'))
      }

      if (resource === 'events') {
        return cursor
          ? Promise.resolve(page([event(13, 'spawned')], false, null))
          : Promise.resolve(page([event(11, 'created'), event(12, 'claimed')], true, 'ev:2'))
      }

      if (resource === 'attachments') {
        return cursor
          ? Promise.resolve(page([attachment(8, 'report2.txt')], false, null))
          : Promise.resolve(page([attachment(7, 'report.txt')], true, 'att:2'))
      }

      return Promise.resolve(emptyPage())
    })

    renderDrawer()

    // page 1
    expect(await screen.findByText('first run summary')).toBeTruthy()
    expect(screen.getByText('second run summary')).toBeTruthy()
    expect(screen.getByText('created')).toBeTruthy()
    expect(screen.getByText('report.txt')).toBeTruthy()

    // page 2 via load-more on each section
    fireEvent.click(screen.getByRole('button', { name: 'Load more runs' }))
    fireEvent.click(screen.getByRole('button', { name: 'Load more events' }))
    fireEvent.click(screen.getByRole('button', { name: 'Load more attachments' }))

    expect(await screen.findByText('third run summary')).toBeTruthy()
    expect(await screen.findByText('spawned')).toBeTruthy()
    expect(await screen.findByText('report2.txt')).toBeTruthy()
  })
})

// ── attachment download through the authenticated JSON transport ─────────────
describe('TaskDrawer — attachment download', () => {
  it('decodes the JSON payload to a Blob and downloads under the named file', async () => {
    const fixture = 'hello!'
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource) =>
      resource === 'attachments'
        ? Promise.resolve(page([attachment(7, 'report.txt')], false, null))
        : Promise.resolve(emptyPage())
    )
    apiMock.fetchAttachmentDownload.mockResolvedValue({
      id: 7,
      filename: 'report.txt',
      content_type: 'text/plain',
      size: 6,
      content_base64: btoa(fixture)
    })

    renderDrawer()

    fireEvent.click(await screen.findByRole('button', { name: /download/i }))

    await waitFor(() => expect(capturedBlob).not.toBeNull())
    expect(await capturedBlob!.text()).toBe(fixture)
    expect(clickedAnchor?.download).toBe('report.txt')
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })

  it('shows a visible failure remedy when the download 404s', async () => {
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource) =>
      resource === 'attachments'
        ? Promise.resolve(page([attachment(7, 'report.txt')], false, null))
        : Promise.resolve(emptyPage())
    )
    apiMock.fetchAttachmentDownload.mockRejectedValue(new Error('404: {"detail":"attachment not found"}'))

    renderDrawer()

    fireEvent.click(await screen.findByRole('button', { name: /download/i }))

    expect(await screen.findByText(/attachment not found/i)).toBeTruthy()
  })
})

// ── missing / error remedy, no hang ───────────────────────────────────────────
describe('TaskDrawer — evidence page errors', () => {
  it('shows a visible error remedy when a page fails (not an infinite spinner)', async () => {
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource) =>
      resource === 'runs'
        ? Promise.reject(new Error('helper unavailable'))
        : Promise.resolve(emptyPage())
    )

    renderDrawer()

    expect(await screen.findByText(/Could not load runs for this card/i)).toBeTruthy()
  })
})

// ── late-card race: a stale page must never land under a new card ────────────
describe('TaskDrawer — late response after card switch', () => {
  it('ignores a late page from the previous card', async () => {
    const slowA = deferred<ReturnType<typeof page>>()

    apiMock.fetchEvidencePage.mockImplementation((_slug, resource, card) => {
      if (resource === 'runs' && card === 't_a') {
        return slowA.promise
      }

      if (resource === 'runs' && card === 't_b') {
        return Promise.resolve(page([run(20, 'B run')], false, null))
      }

      return Promise.resolve(emptyPage())
    })

    const { client, rerender } = renderDrawer('t_a')

    // Switch to card B while card A's runs page is still in flight.
    rerender(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['ready', 'running', 'done']} id="t_b" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    expect(await screen.findByText('B run')).toBeTruthy()

    // Card A's page resolves late — it must not bleed into card B's view.
    slowA.resolve(page([run(1, 'A run')], false, null))

    await waitFor(() => expect(apiMock.fetchEvidencePage).toHaveBeenCalled())
    expect(screen.queryByText('A run')).toBeNull()
  })
})


describe('parent acceptance controls', () => {
  it('does not use legacy detail when identity lookup fails', async () => {
    apiMock.fetchEvidenceContext.mockRejectedValue(new Error('identity unavailable'))
    const { client } = renderDrawer()
    await waitFor(() => expect(client.getQueryCache().getAll().some(q => q.state.status === 'error')).toBe(true))
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(apiMock.fetchTask).not.toHaveBeenCalled()
    expect(apiMock.fetchTaskWithoutHistory).not.toHaveBeenCalled()
  })

  it('does not call pending history an empty history', async () => {
    const pending = deferred<ReturnType<typeof page>>()
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource) => resource === 'runs' ? pending.promise : Promise.resolve(emptyPage()))
    renderDrawer()
    await waitFor(() => expect(apiMock.fetchEvidencePage).toHaveBeenCalled())
    expect(screen.queryByText('No runs.')).toBeNull()
    expect(screen.getByText(/loading runs/i)).toBeTruthy()
  })

  it('replaces changed first-page evidence on refresh instead of retaining old rows', async () => {
    let refreshed = false
    apiMock.fetchEvidencePage.mockImplementation((_slug, resource) => Promise.resolve(resource === 'runs' ? page([run(1, refreshed ? 'corrected run summary' : 'old run summary')], false, null) : emptyPage()))
    const { client } = renderDrawer()
    expect(await screen.findByText('old run summary')).toBeTruthy()
    refreshed = true
    await client.invalidateQueries({ queryKey: ['kanban', 'evidence'] })
    expect(await screen.findByText('corrected run summary')).toBeTruthy()
    expect(screen.queryByText('old run summary')).toBeNull()
  })
})

// ── evo repair controls: cached-detail transition + later-page refresh ────────
describe('TaskDrawer — evo repair controls', () => {
  it('withholds cached legacy detail when the identity check later fails', async () => {
    // Start positively unaligned so the legacy full-history detail (with runs)
    // is fetched and cached by the drawer.
    apiMock.fetchEvidenceContext.mockResolvedValue({ ...alignedContext, aligned: false })
    apiMock.fetchTask.mockImplementation((id: string) =>
      Promise.resolve({
        ...detailFor(id, 'Legacy'),
        runs: [
          {
            id: 1,
            task_id: id,
            profile: 'evo',
            status: 'completed',
            outcome: 'completed',
            summary: 'legacy run summary',
            error: null,
            started_at: 100,
            ended_at: 200
          }
        ]
      })
    )

    const { client } = renderDrawer()
    expect(await screen.findByText('legacy run summary')).toBeTruthy()

    // The identity check now fails on a refetch; the cached legacy detail must
    // be visibly withheld rather than retained or re-fetched.
    apiMock.fetchEvidenceContext.mockRejectedValue(new Error('identity unavailable'))
    await client.invalidateQueries({ queryKey: evidenceContextKey('evo') })

    expect(await screen.findByText('Could not verify this board')).toBeTruthy()
    expect(screen.queryByText('legacy run summary')).toBeNull()
  })

  it('resets accumulated pages and replaces changed rows on a later-page refresh', async () => {
    let page1Summary = 'first run summary'

    apiMock.fetchEvidencePage.mockImplementation((_slug, resource, _card, cursor) => {
      if (resource !== 'runs') {
        return Promise.resolve(emptyPage())
      }

      if (!cursor) {
        return Promise.resolve(page([run(1, page1Summary), run(2, 'second run summary')], true, 'runs:2'))
      }

      return Promise.resolve(page([run(3, 'stale third run')], false, null))
    })

    const { client } = renderDrawer()
    expect(await screen.findByText('first run summary')).toBeTruthy()
    expect(screen.getByText('second run summary')).toBeTruthy()

    // Load page 2 (a third accumulated row).
    fireEvent.click(screen.getByRole('button', { name: 'Load more runs' }))
    expect(await screen.findByText('stale third run')).toBeTruthy()

    // Refresh: page 1 changed; the accumulated page 2 must be discarded and the
    // changed page 1 replaced.
    page1Summary = 'corrected first run'
    await client.invalidateQueries({ queryKey: ['kanban', 'evidence'] })

    expect(await screen.findByText('corrected first run')).toBeTruthy()
    expect(screen.getByText('second run summary')).toBeTruthy()
    expect(screen.queryByText('first run summary')).toBeNull()
    expect(screen.queryByText('stale third run')).toBeNull()
  })
})
