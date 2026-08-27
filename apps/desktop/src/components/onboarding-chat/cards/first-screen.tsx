/**
 * The first-screen card — the dashboard built from what the user just said.
 *
 * It mounts on the context answer, while module generation is usually still in
 * flight, so it has four faces: designing, keep/drop rows once candidates land,
 * a template confirm if generation never does, and a one-line receipt after the
 * build. Continue compiles and materializes the screen, and the pane opens
 * beside the chat — the app assembles itself around the user rather than
 * leaving a button to hunt for.
 */

import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'
import { useEffect, useState } from 'react'

import { CardFrame, type CardProps, report } from '@/components/onboarding-chat/cards/frame'
import { cn } from '@/lib/utils'
import {
  $droppedModuleIds,
  $livePaneOpen,
  $moduleCandidates,
  $speculativeFill,
  advanceSketch,
  compileLiveScreen,
  stopSpeculativeWrites
} from '@/store/first-screen-live'
import { compileFirstScreen, materializeFirstScreen } from '@/store/onboarding-first-screen'
import { $wizardAnswers } from '@/store/onboarding-wizard'

/** How long to show the honest designing state before falling back to the
 *  template confirm. */
const GENERATION_GRACE_MS = 45_000

/** Built receipt, shared across every mount of the card: transcript
 *  virtualization remounts directives with fresh local state, which would
 *  resurrect the keep/drop picker after the build. An atom survives that. */
const $builtConfig = atom<null | ReturnType<typeof compileFirstScreen>>(null)

export function FirstScreenCard({ locked }: CardProps) {
  const answers = useStore($wizardAnswers)
  const [building, setBuilding] = useState(false)
  const built = useStore($builtConfig)
  // The generated modules — THEIR screen's parts, from their own words. When
  // generation produced candidates the card is keep/drop rows; when it failed
  // (or hasn't landed) the kind tiles carry the fallback.
  const candidates = useStore($moduleCandidates)
  const dropped = useStore($droppedModuleIds)
  const keptCount = candidates ? candidates.length - dropped.length : 0
  const [waitedOut, setWaitedOut] = useState(false)

  useEffect(() => {
    if (candidates) {
      return
    }

    const grace = window.setTimeout(() => setWaitedOut(true), GENERATION_GRACE_MS)

    return () => window.clearTimeout(grace)
  }, [candidates])

  // Continue = build. The config compiles synchronously, then materializes
  // (screen.json lands on disk) before the model is told — so when the chat
  // says "it's built", the pane IS ALREADY OPEN beside the conversation.
  const build = () => {
    if (building || (candidates !== null && keptCount === 0)) {
      return
    }

    setBuilding(true)
    // Build owns the file from here — the speculative writer stands down, and
    // whatever it already wrote rides into the final file so the kept modules
    // are usually ALREADY filled (the selectors were the fill's working time).
    stopSpeculativeWrites()

    const config = candidates
      ? compileLiveScreen('dashboard')
      : compileFirstScreen({ context: answers.context, focus: answers.focus, name: answers.name }, 'dashboard')

    void materializeFirstScreen(config).then(result => {
      // Population runs behind the reveal: a hidden fast-lane session fills
      // every block with real content and rewrites screen.json — the pane's
      // file watcher repaints it as the content lands, seconds after it opens.
      // Fire-and-forget: any failure leaves the deterministic screen exactly
      // as materialized.
      if (result.ok) {
        void import('@/store/first-screen-populate').then(({ populateFirstScreenArtifact }) =>
          populateFirstScreenArtifact(config, $speculativeFill.get())
        )
      }

      const picked = candidates ? ' they hand-picked' : ''
      const saved = result.ok ? `, saved to ${result.path}` : ''

      if (
        !report(
          `built their dashboard "${config.title}" with ${config.blocks.length} modules${picked}${saved}. It is open beside this chat and writes itself while you finish the remaining setup steps together — acknowledge briefly and move to the next step.`
        )
      ) {
        setBuilding(false)

        return
      }

      void revealBuiltPane()
      $builtConfig.set(config)
    })
  }

  if (built) {
    // The real artifact is the PANE that just opened beside this chat — the
    // transcript keeps only a quiet one-line receipt. Less in the session,
    // more in the GUI: the app visibly assembled around the user.
    return (
      <div className="my-3 flex items-center gap-1.5 text-muted-foreground text-xs" data-onboarding-card>
        <span aria-hidden>✓</span>
        <span>
          <strong className="font-medium text-foreground">{built.title}</strong> is open beside this chat and sits in
          your sidebar as <strong className="font-medium">Onboarding Dashboard</strong>.
        </span>
      </div>
    )
  }

  if (!candidates && !waitedOut) {
    // Generation in flight — honest designing state with a live spinner.
    // Continue stays away entirely; the card swaps to keep/drop rows the
    // moment candidates land.
    return (
      <div className="my-3 flex items-center gap-2.5 text-[12px] text-muted-foreground" data-onboarding-card>
        <span className="size-3.5 flex-none animate-spin rounded-full border-2 border-muted-foreground/30 border-t-primary" />
        Designing your dashboard from what you told me…
      </div>
    )
  }

  if (candidates) {
    // THEIR modules, generated from their own answers mid-conversation:
    // keep/drop rows (the choosing IS the interaction).
    return (
      <CardFrame disabled={keptCount === 0} done={false} locked={locked || building} onContinue={build}>
        <div className="flex min-w-0 flex-col gap-1 overflow-hidden">
          {candidates.map(module => {
            const off = dropped.includes(module.id)

            return (
              <button
                aria-pressed={!off}
                className={cn(
                  'flex w-full min-w-0 items-center gap-2.5 overflow-hidden rounded-[8px] border px-3 py-2 text-left transition-colors',
                  off ? 'border-transparent opacity-45 hover:opacity-70' : 'border-border bg-card hover:border-primary/40'
                )}
                key={module.id}
                onClick={() => {
                  $droppedModuleIds.set(off ? dropped.filter(id => id !== module.id) : [...dropped, module.id])
                  // Mirror the pick into the pane immediately: the dropped
                  // module grays out beside the chat as the box unchecks.
                  advanceSketch()
                }}
                type="button"
              >
                <Tick off={off} />
                <span className="min-w-0">
                  <span className={cn('block truncate text-[13px] font-medium', off && 'line-through')}>
                    {module.label}
                  </span>
                  <span className="block truncate text-[11px] text-muted-foreground">{module.prompt}</span>
                </span>
                <span className="ml-auto flex-none font-mono text-[9px] uppercase tracking-wider text-muted-foreground">
                  {module.kind}
                </span>
              </button>
            )
          })}
        </div>
      </CardFrame>
    )
  }

  return (
    <CardFrame done={false} locked={locked || building} onContinue={build}>
      <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
        <Tick off={false} />
        Your dashboard is drafted from what you told me. Press Continue and it opens beside this chat.
      </div>
    </CardFrame>
  )
}

function Tick({ off }: { off: boolean }) {
  return (
    <span
      className={cn(
        'grid size-4 flex-none place-items-center rounded-[4px] border text-[10px] leading-none',
        off ? 'border-muted-foreground/40 text-transparent' : 'border-primary bg-primary text-primary-foreground'
      )}
    >
      ✓
    </span>
  )
}

/** Dock the built pane beside the chat. It is usually ALREADY open (the sketch
 *  arrived at the focus step), so only a run where that never happened needs
 *  the grow — growing again would widen the window twice. */
async function revealBuiltPane(): Promise<void> {
  const [{ registry }, { dockPaneBeside, revealTreePane }, loader] = await Promise.all([
    import('@/contrib/registry'),
    import('@/components/pane-shell/tree/store'),
    import('@/contrib/runtime-loader')
  ])

  // Skip the disk watcher's tick — rescan now so the pane docks the moment the
  // build lands.
  await loader.discoverRuntimePlugins().catch(() => undefined)

  const deadline = Date.now() + 15_000

  while (Date.now() < deadline) {
    if (registry.getArea('panes').some(c => c.id === 'first-screen:pane')) {
      if (!$livePaneOpen.get()) {
        window.hermesDesktop?.chatOnboarding?.grow({ bottom: 0, left: 0, right: 380, top: 0 })
      }

      dockPaneBeside('first-screen:pane', 'workspace')
      revealTreePane('first-screen:pane')

      return
    }

    await new Promise(resolve => setTimeout(resolve, 500))
  }
}
