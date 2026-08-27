/**
 * Guide-mode first run — the animation → guided in-chat onboarding chain (no
 * wizard window, no sign-in card). Pins:
 *
 * - startOnboardingWizard() (the finishIntroReveal handoff) runs GUIDE mode:
 *   no window, the wizard settles instantly, the guided chat is the setup.
 * - startOnboardingWizardWindow still honors an explicit login-mode boot
 *   (the window machinery is kept), and full mode for the classic dev run.
 * - The classic dev run (devStartOnboardingWizard) stays full-mode.
 * - shouldStartGuideKickoff: true only while a first-run chain is mid-handoff
 *   (intro seen this launch + wizard settled).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $machine } from './machine'
import {
  $onboardingWizard,
  devStartOnboardingWizard,
  resetOnboardingWizardForTests,
  shouldResumeOnboardingWizard,
  shouldStartGuideKickoff,
  startOnboardingWizard,
  startOnboardingWizardWindow
} from './onboarding-wizard'

describe('onboarding wizard guide mode', () => {
  beforeEach(() => {
    resetOnboardingWizardForTests()
    localStorage.clear()
    vi.stubEnv('VITE_INTRO_REVEAL', '1')
  })

  it('first-run handoff (startOnboardingWizard) opens NO window and settles', () => {
    startOnboardingWizard()

    const s = $onboardingWizard.get()

    expect(s.phase).toBe('hidden')
    // The done-key is written (prod semantics); hasCompletedOnboardingWizard
    // itself always reads false under import.meta.env.DEV by design.
    expect(localStorage.getItem('hermes-onboarding-wizard-done-v1')).toBe('1')
    // The gate's guide-kickoff check now holds: intro seen (the handoff marks
    // it) + wizard settled = mid-handoff.
    expect(shouldStartGuideKickoff()).toBe(true)
    // And the resume path must NOT fire — the wizard is done, not unfinished.
    expect(shouldResumeOnboardingWizard()).toBe(false)
  })

  it('window boot still honors login mode and full mode', () => {
    startOnboardingWizardWindow(true, 'login')
    expect($onboardingWizard.get().steps).toEqual(['login'])
    expect($onboardingWizard.get().mode).toBe('login')

    resetOnboardingWizardForTests()
    startOnboardingWizardWindow(true, 'full')

    const full = $onboardingWizard.get()

    expect(full.mode).toBe('full')
    expect(full.steps.length).toBeGreaterThan(2)
    expect(full.steps).toContain('providers')
    expect(full.steps).toContain('finale')
    expect(full.steps).not.toContain('login')
  })

  it('classic dev run stays full-mode', () => {
    devStartOnboardingWizard()

    const s = $onboardingWizard.get()

    expect(s.mode).toBe('full')
    expect(s.steps).not.toContain('login')
  })

  it('guide kickoff requires the intro seen-key (no stale done-key handoff)', () => {
    // A completed wizard from a PREVIOUS run (done-key set, intro never seen
    // this launch) must not read as a mid-handoff guide run.
    localStorage.setItem('hermes-onboarding-wizard-done-v1', '1')

    expect(shouldStartGuideKickoff()).toBe(false)
  })
})

describe('pre-banked greeting', () => {
  it('the seed rows carry the exact on-screen greeting and the runbook forbids re-greeting', async () => {
    const { buildChatOnboardingSeedMessages } = await import('./onboarding-script')
    const greeting = 'Hey, welcome to Hermes. What should I call you?'
    const seeds = buildChatOnboardingSeedMessages(greeting)

    // Runbook (hidden) first, then the greeting as a REAL assistant row —
    // the canonical Bot Chat must rehydrate with the same words the banked
    // reveal typed in.
    expect(seeds[0].display_kind).toBe('hidden')
    expect(seeds[0].content).toContain('Do not greet again')
    expect(seeds[1]).toEqual({ content: greeting, role: 'assistant' })
  })

  it('pickOnboardingGreeting is stable within a run and never empty', async () => {
    const { $onboardingGreeting, pickOnboardingGreeting } = await import('@/components/onboarding-chat/assembly')

    $onboardingGreeting.set('')
    const first = pickOnboardingGreeting()

    expect(first.length).toBeGreaterThan(20)
    expect(first).toContain('call you')
    expect(pickOnboardingGreeting()).toBe(first)
  })
})

describe('the OS-name suggestion', () => {
  afterEach(() => {
    $machine.set(null)
  })

  it('offers the account name in the banked greeting when it looks like a name', async () => {
    const { $onboardingGreeting, pickOnboardingGreeting } = await import('@/components/onboarding-chat/assembly')

    $machine.set({ ageDays: 400, arch: 'x64', model: '', nvidia: false, platform: 'darwin', release: '24.6.0', username: 'alex' })
    $onboardingGreeting.set('')

    const greeting = pickOnboardingGreeting()

    expect(greeting).toContain('alex')
    expect(greeting).toContain('if you prefer')
    expect(pickOnboardingGreeting()).toBe(greeting)
  })

  it('seeds the same suggested name into the hidden runbook so a one-word yes resolves', async () => {
    const { buildChatOnboardingSeedMessages } = await import('./onboarding-script')

    $machine.set({ ageDays: 400, arch: 'x64', model: '', nvidia: false, platform: 'darwin', release: '24.6.0', username: 'alex' })

    const seeds = buildChatOnboardingSeedMessages('Hi there')

    expect(seeds[0].display_kind).toBe('hidden')
    expect(seeds[0].content).toContain('"alex"')
    expect(seeds[0].content).toMatch(/save exactly "alex"/)
  })

  it('leaves both clean when the account name is not suggestable', async () => {
    const { buildChatOnboardingSeedMessages } = await import('./onboarding-script')
    const { $onboardingGreeting, pickOnboardingGreeting } = await import('@/components/onboarding-chat/assembly')

    // Blocklisted handle — a login name nobody should be called.
    $machine.set({ ageDays: 400, arch: 'x64', model: '', nvidia: false, platform: 'darwin', release: '24.6.0', username: 'user' })

    const seeds = buildChatOnboardingSeedMessages('Hi there')

    expect(seeds[0].content).not.toContain('OS account name')

    $onboardingGreeting.set('')
    expect(pickOnboardingGreeting()).not.toContain('if you prefer')
  })
})
