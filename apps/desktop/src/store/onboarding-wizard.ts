/**
 * Onboarding wizard — the Dia-style first-run setup that follows the intro
 * cinematic. A small curated modal (not an app takeover): welcome → personalize
 * → connectors → appearance → system, an optional provider step when no
 * inference path exists, then a cinematic "Welcome to your agent" finale that
 * dissolves straight into the first chat.
 *
 * Trigger contract:
 * - First run: `finishIntroReveal()` calls `startOnboardingWizard()` after the
 *   cinematic (skips included). If the app restarts mid-wizard, the gate
 *   restarts it — the intro's seen-key is set but the wizard's done-key isn't.
 * - The wizard is gated by the same build flag as the intro
 *   (`VITE_INTRO_REVEAL=1`); neither exists in unflagged builds.
 *
 * Answers persist live (localStorage) so a mid-flow restart keeps them, and so
 * the first-chat kickoff can read them after the wizard unmounts.
 */

import { atom } from 'nanostores'

import { readJson, readKey, writeJson, writeKey } from '@/lib/storage'

import { $instantAccount, instantSuppressesOnboarding } from './instant-account'
import { clearIntroRevealSeen, hasSeenIntroReveal, isIntroRevealEnabled } from './intro-reveal'
import { $desktopOnboarding } from './onboarding'
import { setOnboardingSurfaceActive } from './onboarding-presence'

const DONE_KEY = 'hermes-onboarding-wizard-done-v1'
const ANSWERS_KEY = 'hermes-onboarding-wizard-answers-v1'
const GUIDE_KICKED_KEY = 'hermes-onboarding-guide-kicked-v1'

/** Guide handoff beacon — set by the no-window guide run, consumed by the
 *  gate. An ATOM, not just the localStorage keys, because the done-key is
 *  written from finishIntroReveal's dynamic-import callback AFTER the intro
 *  store has already settled to 'hidden': by then every dependency of the
 *  gate's kickoff effect (enabled, intro.phase, wizard.phase) has gone quiet,
 *  so a poll-on-render check misses the handoff entirely — the cinematic
 *  dissolves into a vanilla shell and the guided chat never starts. The
 *  beacon is reactive state the gate subscribes to, so the write itself
 *  re-fires the effect. */
export const $guideKickoffPending = atom(false)

export type WizardStepId =
  /** Pick what your first screen should be — dashboard, document, or app. */
  | 'first-screen'
  | 'welcome'
  | 'personalize'
  | 'connectors'
  | 'appearance'
  | 'system'
  /** Only present when no inference path exists (instant mint failed/off). */
  | 'providers'
  /** The login-mode run's single step: sign in to Nous Portal (or any
   *  provider behind the disclosure), skippable. */
  | 'login'
  /** Cinematic full-bleed "Welcome to your agent" before the app appears. */
  | 'finale'

/** Which run the wizard window hosts:
 *  - 'full'  — the classic multi-step setup (dev:onboarding, screenshots).
 *  - 'login' — one card: portal sign-in, then the guided IN-CHAT setup takes
 *    over. No longer on the first-run path; kept for the window machinery.
 *  - 'guide' — NO window: the first-run default. The wizard settles instantly
 *    and the gate hands straight off to the in-chat guided setup. */
export type WizardRunMode = 'full' | 'guide' | 'login'

export interface WizardAnswers {
  /** What the user wants to be called. Optional — empty is fine. */
  name: string
  /** What they're actually working on right now, in their own words —
   *  the free-text answer that makes the first screen THEIRS instead of a
   *  template. Captured conversationally in the guided chat. */
  context: string
  /** Focus areas picked on the personalize step. */
  focus: string[]
  /** Connector ids toggled on (fake for now — stored, not wired). */
  connectors: string[]
  /** Theme skin committed on the appearance step. */
  theme: string
  /** Accent seed picked on the appearance step; null = the theme's own. */
  accent: null | string
  /** Layout preset id committed on the appearance step. */
  layout: string
  /** Keep Hermes in the dock (macOS nicety — stored, best-effort). */
  keepInDock: boolean
  /** Launch Hermes at login. */
  openAtLogin: boolean
}

export const DEFAULT_ANSWERS: WizardAnswers = {
  accent: null,
  connectors: [],
  context: '',
  focus: [],
  keepInDock: true,
  layout: 'basic',
  name: '',
  openAtLogin: false,
  theme: 'nous'
}

export interface OnboardingWizardState {
  phase: 'hidden' | 'active'
  step: WizardStepId
  /** Step list for this run (provider step is conditional). */
  steps: WizardStepId[]
  /** Which run this is — the gate forwards it to the dedicated window. */
  mode: WizardRunMode
}

/** Outcome the wizard window reports back over IPC (see preload/global.d.ts). */
export interface OnboardingWizardOutcome {
  /** False when the user skipped setup. */
  completed: boolean
  /** Full-run only: the first-screen artifact the finale built. The main
   *  window uses it to seed the first chat ("press a button, it does
   *  something") after the take-over. Absent on skip and in login mode. */
  firstScreen?: { configJson: string; filePath?: string; kind: string }
  /** False when the run needed a provider and none was configured (the step
   *  was skipped past) — the first-chat kickoff has nothing to greet with. */
  providerReady?: boolean
  /** Which run produced this outcome. Login-mode outcomes hand off to the
   *  in-chat guided setup instead of the greet kickoff. */
  mode?: WizardRunMode
  /** Login mode only: the app should come back as the small solo-chat window
   *  and run the guided in-chat setup (electron pre-sizes before showing). */
  soloChat?: boolean
}

const INITIAL: OnboardingWizardState = {
  mode: 'full',
  phase: 'hidden',
  step: 'welcome',
  steps: []
}

export const $onboardingWizard = atom<OnboardingWizardState>(INITIAL)

// Presence mirror — see onboarding-presence.ts (update toast stands down).
$onboardingWizard.subscribe(state => setOnboardingSurfaceActive('wizard', state.phase !== 'hidden'))

function loadAnswers(): WizardAnswers {
  const raw = readJson<Partial<WizardAnswers>>(ANSWERS_KEY)

  return { ...DEFAULT_ANSWERS, ...raw }
}

/** Dev reruns the wizard every launch (see hasCompletedOnboardingWizard) — it
 *  must also START clean every launch, not preloaded with the last run's
 *  picks. The stored blob is dropped too, so a run that touches nothing can't
 *  hand stale picks to the main window's commit. Prod resumes from storage. */
function initialAnswers(): WizardAnswers {
  if (import.meta.env.DEV) {
    writeJson(ANSWERS_KEY, null)

    return { ...DEFAULT_ANSWERS }
  }

  return loadAnswers()
}

export const $wizardAnswers = atom<WizardAnswers>(initialAnswers())

export function setWizardAnswers(patch: Partial<WizardAnswers>): void {
  const next = { ...$wizardAnswers.get(), ...patch }

  $wizardAnswers.set(next)
  writeJson(ANSWERS_KEY, next)
}

export function hasCompletedOnboardingWizard(): boolean {
  // Dev builds never persist "onboarded": every dev launch (with the intro
  // flag on) boots straight into the wizard for QA. Completing or skipping
  // still settles it for the running session — see `settledThisSession`.
  if (import.meta.env.DEV) {
    return false
  }

  return readKey(DONE_KEY) === '1'
}

function markDone(): void {
  writeKey(DONE_KEY, '1')
}

/** True when the wizard needs a provider step: no guest account is carrying
 *  inference AND the classic onboarding never completed. */
export function wizardNeedsProviderStep(): boolean {
  if (instantSuppressesOnboarding($instantAccount.get().status)) {
    return false
  }

  return $desktopOnboarding.get().configured !== true
}

function buildSteps(includeProviders = wizardNeedsProviderStep()): WizardStepId[] {
  const steps: WizardStepId[] = ['welcome', 'personalize', 'connectors', 'appearance']

  // The (conditional) provider step sits right before "Make Hermes at home" —
  // intelligence gets connected before the domestic niceties close the run.
  // TEMP dev: always in, every path (Electron included), so the step is
  // testable regardless of the accountless gate. Re-gate before ship.
  if (includeProviders || import.meta.env.DEV) {
    steps.push('providers')
  }

  steps.push('system', 'first-screen', 'finale')

  return steps
}

// Set once the wizard has run its course this session — completed, skipped,
// or its window closed with no outcome (⌘W). Stops the resume path from
// re-opening it mid-session; in dev (where the done-key is ignored) this is
// the only thing that ends it.
let settledThisSession = false

/** The gate's restart check: intro seen, wizard unfinished, flag on. */
export function shouldResumeOnboardingWizard(): boolean {
  return (
    !settledThisSession && isIntroRevealEnabled() && hasSeenIntroReveal() && !hasCompletedOnboardingWizard()
  )
}

/** The gate's guide-kickoff check: a first-run chain mid-handoff — intro seen
 *  THIS launch (devResetOnboardingFlow clears it), wizard settled by the
 *  no-window guide run, guided chat not yet kicked off. Reads the done-key
 *  directly: hasCompletedOnboardingWizard() is always false in dev builds,
 *  but the handoff must still hold there (dev:full runs the real chain).
 *
 *  The kicked-key is the persistent half of the latch: seen + done survive
 *  relaunch, so without it every launch of an onboarded install would re-run
 *  the guided chat (the gate's module flag resets per process). */
export function shouldStartGuideKickoff(): boolean {
  return (
    isIntroRevealEnabled() &&
    hasSeenIntroReveal() &&
    readKey(DONE_KEY) === '1' &&
    readKey(GUIDE_KICKED_KEY) !== '1'
  )
}

/** Mid-handoff relaunch: the persistent keys say the guide was settled but
 *  never launched (app quit between the cinematic and the first chat). Seed
 *  the beacon at module load so the gate picks the handoff back up. Guarded
 *  so a plain onboarded install (kicked-key set) boots quiet. */
export function seedGuideKickoffFromStorage(): void {
  if (shouldStartGuideKickoff()) {
    $guideKickoffPending.set(true)
  }
}

/** Stamp the guided chat as launched — called by the gate the moment it hands
 *  off, so a relaunch resumes the normal app instead of re-onboarding. */
export function markGuideKickoffStarted(): void {
  writeKey(GUIDE_KICKED_KEY, '1')
  $guideKickoffPending.set(false)
}

/** The wizard window closed with no outcome — stand down for this session. */
export function dismissOnboardingWizardSession(): void {
  settledThisSession = true
  $onboardingWizard.set(INITIAL)
}

/** Begin (or resume) the wizard. No-ops once done.
 *
 *  The first-run chain runs GUIDE mode: animation → the guided in-chat setup
 *  directly — accountless, no wizard window, no sign-in card (login comes
 *  later, once the first task is under way). LOGIN mode (animation → one
 *  portal sign-in card → the guided chat) and the classic multi-step run
 *  stay reachable through the dev entries (`dev:onboarding`,
 *  `__onboarding.start`). */
export function startOnboardingWizard(mode: WizardRunMode = 'guide'): void {
  if (!isIntroRevealEnabled() || hasCompletedOnboardingWizard()) {
    return
  }

  if (mode === 'guide') {
    // No window at all: mark the wizard settled (the guided chat IS the
    // setup) and let the gate's effect hand off to the guide kickoff. The
    // intro's seen-key is the handoff's other half — finishIntroReveal set
    // it on the real chain, but a direct startOnboardingWizard() (the gate's
    // resume path, tests) arrives without it, so stamp it here too. The
    // beacon is what actually wakes the gate (see $guideKickoffPending).
    settledThisSession = true
    markDone()
    writeKey('hermes-intro-reveal-seen-v1', '1')
    $onboardingWizard.set(INITIAL)
    $guideKickoffPending.set(true)

    return
  }

  const steps: WizardStepId[] = ['login']

  $onboardingWizard.set({ mode, phase: 'active', step: steps[0], steps })
}

/** Boot the surface inside the dedicated `?win=onboarding` window. That window
 *  is gateway-less, so the provider decision arrives from the main renderer
 *  via the open IPC → query param instead of being computed here. Login mode
 *  is kept for an explicit portal-sign-in-only handoff. */
export function startOnboardingWizardWindow(includeProviders: boolean, mode: WizardRunMode = 'full'): void {
  const steps: WizardStepId[] = mode === 'login' ? ['login'] : buildSteps(includeProviders)

  $onboardingWizard.set({ mode, phase: 'active', step: steps[0], steps })
}

/** Re-read answers persisted by the wizard WINDOW (shared origin storage) into
 *  this renderer's atom — the main renderer commits from these after `done`. */
export function reloadWizardAnswers(): WizardAnswers {
  const answers = loadAnswers()

  $wizardAnswers.set(answers)

  return answers
}

export function wizardStepIndex(state: OnboardingWizardState): number {
  return Math.max(0, state.steps.indexOf(state.step))
}

export function nextWizardStep(): void {
  const s = $onboardingWizard.get()

  if (s.phase !== 'active') {
    return
  }

  const index = wizardStepIndex(s)

  if (index >= s.steps.length - 1) {
    completeOnboardingWizard()

    return
  }

  $onboardingWizard.set({ ...s, step: s.steps[index + 1] })
}

export function backWizardStep(): void {
  const s = $onboardingWizard.get()
  const index = wizardStepIndex(s)

  if (s.phase !== 'active' || index === 0) {
    return
  }

  $onboardingWizard.set({ ...s, step: s.steps[index - 1] })
}

/** Skip the remainder — marks done so it never auto-shows again. */
export function skipOnboardingWizard(): void {
  settledThisSession = true
  markDone()
  $onboardingWizard.set(INITIAL)
}

/** Terminal state — the finale finished; the app (and first chat) take over. */
export function completeOnboardingWizard(): void {
  settledThisSession = true
  markDone()
  $onboardingWizard.set(INITIAL)
}

/** Hard reset for tests. */
export function resetOnboardingWizardForTests(): void {
  settledThisSession = false
  $onboardingWizard.set(INITIAL)
  $guideKickoffPending.set(false)
  $wizardAnswers.set({ ...DEFAULT_ANSWERS })
}

// ── Dev hooks (installed by the gate in dev builds only) ─────────────────────

/** Stage baked by the `npm run dev:{movie,onboarding,kickoff,chat,full}`
 *  entry points (VITE_ONBOARDING_STAGE). The gate auto-launches it on boot;
 *  'wizard' also pauses the finale so its animation can be iterated on;
 *  'chat' is the in-chat guided setup experiment. */
export type OnboardingDevStage = 'chat' | 'full' | 'kickoff' | 'movie' | 'wizard'

const DEV_STAGES: readonly string[] = ['chat', 'full', 'kickoff', 'movie', 'wizard']

export function onboardingDevStage(): OnboardingDevStage | null {
  if (!import.meta.env.DEV) {
    return null
  }

  const stage: unknown = import.meta.env.VITE_ONBOARDING_STAGE

  return typeof stage === 'string' && DEV_STAGES.includes(stage) ? (stage as OnboardingDevStage) : null
}

/** Force-start at any step, bypassing the build flag and the done-key.
 *  Jumping to the provider step forces it into the run even when the
 *  accountless path would have dropped it — every stage stays testable. */
export function devStartOnboardingWizard(step?: WizardStepId): void {
  const steps = buildSteps(step === 'providers' ? true : undefined)
  const target = step && steps.includes(step) ? step : steps[0]

  $onboardingWizard.set({ mode: 'full', phase: 'active', step: target, steps })
}

/** Forget everything: intro seen-key, wizard done-key, kicked-key, answers. */
export function devResetOnboardingFlow(): void {
  settledThisSession = false
  writeKey(DONE_KEY, null)
  writeKey(GUIDE_KICKED_KEY, null)
  writeJson(ANSWERS_KEY, null)
  clearIntroRevealSeen()
  $onboardingWizard.set(INITIAL)
  $guideKickoffPending.set(false)
  $wizardAnswers.set({ ...DEFAULT_ANSWERS })
}
