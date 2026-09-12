import { act, cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { registry } from '@/contrib/registry'

import { ChatRoutesSurface } from './surfaces'
import type { WiringActions } from './types'

// Real registry, real subscription, real route derivation — the ONLY things this
// suite may not mock are the contribution registry, `useContributions`, and the
// route table under test (issue55: a plugin route registered after mount must
// render without a reload). Everything else here is presentation noise that the
// route surface delegates to, not part of the route-resolution path.

vi.mock('@/components/pane-shell/tree/store', () => ({
  noteActiveTreeGroup: vi.fn(),
  revealTreePane: vi.fn()
}))
vi.mock('@/store/connections', () => ({ $activeConnectionId: atom('local') }))
vi.mock('@/store/gateway', () => ({ $gateway: atom<unknown>(null) }))
vi.mock('@/store/profile', () => ({ $activeGatewayProfile: atom('default') }))
vi.mock('@/store/session', () => ({
  $freshDraftReady: atom(false),
  $gatewayState: atom('open')
}))
vi.mock('../chat', () => ({
  ChatView: () => <div data-testid="chat-view">chat</div>
}))
vi.mock('../chat/sidebar', () => ({ ChatSidebar: () => null }))
vi.mock('../right-sidebar/terminal/chrome', () => ({ TerminalPaneChrome: () => null }))
vi.mock('../shell/hooks/use-status-snapshot', () => ({ useStatusSnapshot: () => ({}) }))
vi.mock('../shell/hooks/use-statusbar-items', () => ({
  useStatusbarItems: () => ({ leftStatusbarItems: [], statusbarItems: [] })
}))
vi.mock('../shell/statusbar-controls', () => ({ StatusbarControls: () => null }))
vi.mock('../shell/model-menu-panel', () => ({ ModelMenuPanel: () => null }))
vi.mock('./latest-actions', () => ({ latestChatActions: () => ({}), latestSidebarActions: () => ({}) }))
vi.mock('./panes', () => ({ setStatusbarItemGroup: vi.fn(), useStatusbarContributions: () => [] }))

const ACTIONS = {} as unknown as WiringActions

const BOARD = 'kanban-board-live'

/** Register a real `routes` contribution that renders a sentinel board node. */
function contributeRoute(path: string): () => void {
  return registry.register({
    area: 'routes',
    data: { path },
    id: path.replace('/', 'route-'),
    render: () => <div data-testid={BOARD}>{path}</div>,
    title: 'Test board'
  })
}

const renderSurfaceAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ChatRoutesSurface actions={ACTIONS} />
    </MemoryRouter>
  )

const disposers: Array<() => void> = []

afterEach(() => {
  cleanup()
  disposers.splice(0).forEach(dispose => dispose())
})

describe('ChatRoutesSurface contributed routes (issue55)', () => {
  it('renders a route registered before mount — initial enabled positive', () => {
    disposers.push(contributeRoute('/kanban'))

    renderSurfaceAt('/kanban')

    expect(screen.getByTestId(BOARD).textContent).toBe('/kanban')
  })

  it('renders a route registered AFTER mount without a reload', () => {
    // Mount first, at the contributed path, with NO route registered yet.
    renderSurfaceAt('/kanban')

    // The path does not exist yet — the chat view (or the fallback) shows.
    expect(screen.queryByTestId(BOARD)).toBeNull()

    act(() => {
      disposers.push(contributeRoute('/kanban'))
    })

    expect(screen.getByTestId(BOARD).textContent).toBe('/kanban')
  })

  it('stops rendering a route once it is unregistered', () => {
    disposers.push(contributeRoute('/kanban'))
    renderSurfaceAt('/kanban')
    expect(screen.getByTestId(BOARD)).toBeTruthy()

    act(() => {
      const dispose = disposers.pop()
      dispose?.()
    })

    expect(screen.queryByTestId(BOARD)).toBeNull()
  })

  it('leaves an unrelated route path alone while another is mounted', () => {
    disposers.push(contributeRoute('/kanban'))
    renderSurfaceAt('/kanban')

    // A DIFFERENT contributed path is not this route — no board for it.
    expect(screen.queryByTestId(BOARD)?.textContent).toBe('/kanban')

    // The same surface at the chat index route shows the chat view, not the board.
    cleanup()
    renderSurfaceAt('/')
    expect(screen.queryByTestId(BOARD)).toBeNull()
    expect(screen.getByTestId('chat-view')).toBeTruthy()
  })
})
