/**
 * The welcome chat — the profile guided onboarding runs in.
 *
 * It is not an anonymous session: it belongs to a persistent `hermes-setup`
 * profile, so the conversation survives onboarding and can be found again. An
 * ordinary profile with an ordinary visible chat — there is no bot surface
 * here, and nothing in this flow mints one.
 *
 * `setup` is the INTERNAL name throughout this module (the profile key, the
 * atoms, the hidden `[setup]` notes). It is never what the user reads: to
 * them the voice is just Hermes, and the chat is titled `Welcome to Hermes`.
 *
 * When the first task is decided it is NOT built in this chat. The model emits
 * `::onboarding{step="handoff" task="…" brief="…"}` and the renderer opens a
 * NEW session on the user's default profile, seeded with the work-side
 * runbook, and starts the build there. The welcome chat hears how it went
 * through a hidden `[setup]` note.
 *
 * This module owns the pure pieces (names, souls, seed prompts, the handoff
 * request atom). The side effects — profiles.create, session.create, the chat
 * switch — live in the wiring's handoff effect so they run with real
 * gateway/session hooks.
 */

import { atom } from 'nanostores'

import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { readKey, writeKey } from '@/lib/storage'
import { machineDescription } from '@/store/machine'
import { PLAIN_SPEECH } from '@/store/onboarding-script'
import type { WizardAnswers } from '@/store/onboarding-wizard'

/** Profile name of the onboarding guide. Prefixed so it can't collide with a
 *  profile a user actually named "setup". */
export const SETUP_PROFILE = 'hermes-setup'

/** Title of the welcome chat, and the row the user sees in their sessions
 *  list. Exact-title lookup is how kickoff re-finds it across relaunches, so
 *  this string is also a registry key — change the words, keep them stable. */
export const SETUP_CHAT_TITLE = 'Welcome to Hermes'

const HANDOFF_DONE_KEY = 'hermes-setup-handoff-done-v1'

export type SetupHandoffPhase = 'done' | 'error' | 'opening' | 'pending'

/** What KIND of first job this is. 'machine-setup' is the one shape we script
 *  ourselves: the work is known (audit the box, then install), the user can't
 *  brief it, and the agent needs permission discipline the moment it starts
 *  touching the system. Everything else is 'build' — the user's own idea. */
export type HandoffPlan = 'build' | 'machine-setup'

export function parseHandoffPlan(raw: string | undefined): HandoffPlan {
  return (raw ?? '').trim().toLowerCase() === 'machine-setup' ? 'machine-setup' : 'build'
}

export interface SetupHandoffState {
  task: string
  brief: string
  phase: SetupHandoffPhase
  plan: HandoffPlan
  /** Title of the session the build landed in, once it exists. */
  sessionTitle?: string
}

/** The handoff beacon: HandoffCard raises it, the wiring effect performs it.
 *  Null until the model emits the handoff directive. */
export const $setupHandoff = atom<null | SetupHandoffState>(null)

/** The guide's own session ids (+ owning profile, null in the profile-less
 *  fallback), kept so the handoff can whisper a hidden [setup] note back into
 *  the guide chat — on the guide's own backend — after the build takes over. */
export const $setupSession = atom<null | {
  profile: null | string
  runtimeId: string
  storedId: null | string
}>(null)

/** Raise the handoff request (once per task — re-parses and re-mounts of the
 *  directive are no-ops, and a relaunch after a completed handoff stays
 *  quiet thanks to the storage latch). */
export function requestSetupHandoff(task: string, brief: string, plan: HandoffPlan = 'build'): boolean {
  if ($setupHandoff.get() !== null || readKey(HANDOFF_DONE_KEY) === '1') {
    return false
  }

  $setupHandoff.set({ brief, phase: 'pending', plan, task })

  return true
}

/** Burn the relaunch latch — the build session exists and its chat is open. */
export function markSetupHandoffDone(): void {
  writeKey(HANDOFF_DONE_KEY, '1')
}

/** True once a handoff completed on this install (survives relaunch) — used
 *  by the card to render its settled state when the atom is long gone. */
export function hasCompletedSetupHandoff(): boolean {
  return readKey(HANDOFF_DONE_KEY) === '1'
}

export function resetSetupHandoffForTests(): void {
  writeKey(HANDOFF_DONE_KEY, null)
  $setupHandoff.set(null)
  $setupSession.set(null)
}

/** Short display title for the first build's session row. */
export function firstTaskTitle(task: string): string {
  const trimmed = task.trim()

  return trimmed.length > 28 ? `${trimmed.slice(0, 27).trimEnd()}…` : trimmed || 'First build'
}

/** SOUL.md for the welcome profile — its standing identity across the welcome
 *  chat and every later check-in. */
export function composeSetupSoul(): string {
  return [
    '# Hermes',
    '',
    'You are Hermes, and this profile is where you met this user for the first time and stay reachable afterwards. You are the person at the front desk of somewhere good: pleased they came in, and not performing it. Quick, unhurried, never flustered, never in the way. You showed them around on their first run and you keep a loose eye on how they are getting on.',
    '',
    '- Never introduce yourself as "Setup", "the setup assistant", or "the onboarding guide". You are Hermes.',
    '- Warmth is in paying attention, not in adjectives. Remember what they told you and use it. Do not thank them for answering, do not praise their choices, do not ask if they are ready.',
    '- Offer an opinion lightly when you have one. "Most people wire that one up first" is worth more than a neutral menu.',
    '- You are training wheels: useful early, ignorable later. Never guilt-trip, never nag. If the user asks you to stop checking in, stop.',
    '- When you check in, look at what has actually changed (their sessions, connectors, scheduled jobs) before offering anything. One concrete suggestion beats a menu.',
    '- Things worth offering, roughly in order: wiring a connector they said they use, scheduling something they do repeatedly, a second build based on the first, keyboard/layout niceties.',
    '- Write like a person talking to another person. Short sentences, plain words, no headers, no bullet walls, no emoji.'
  ].join('\n')
}

/** The hidden runbook seeded into the first build's session — the work-side
 *  half of the old single-chat script: no-auth first build, the permissions
 *  note, and the live progress cards. */
export function buildFirstTaskRunbook(task: string, answers: WizardAnswers, plan: HandoffPlan = 'build'): string {
  const name = (answers.name ?? '').trim()
  const context = (answers.context ?? '').trim()
  const tools = (answers.connectors ?? []).filter(Boolean)

  return [
    `You are Hermes. The user's welcome chat just opened this session so one task can have room to run: ${task.trim()}.`,
    'This message is invisible to the user — never reference it or the mechanics described here.',
    name ? `The user is called ${name} — you already know that, so never introduce yourself or ask who they are.` : '',
    context ? `They already said what they are working on: ${context}. Let it shape your choices without re-asking.` : '',
    tools.length
      ? `Tools they use day to day: ${tools.join(', ')} — none are connected yet; never require one for this first build.`
      : '',
    'Their next message is the go signal: really begin the work — plan briefly, then build (scaffold, research, first artifact).',
    "As you start, tell them in one short sentence: you'll ask for permissions as you go, and they can say no to anything or redirect you.",
    ...(plan === 'machine-setup' ? machineSetupRunbook() : [NO_AUTH_RULE]),
    'While the work runs, place ::onboarding{step="progress" title="what you\'re doing"} as its own paragraph at the start of each status turn — the card shows the build breathing live. Keep the titles short and present-tense ("Scaffolding the project", "Wiring the reminder"). Emit each exactly like that, alone on its own line.',
    'When the first pass of the build is DONE: end that turn with ::ask{question="Does this match what you wanted?" options="Looks right|Change something|Take it further"} alone as its own paragraph, emitted EXACTLY as written. Act on their pick immediately. One unreviewed first output is how a build reads as broken; the ask is how it reads as a collaboration.',
    PLAIN_SPEECH
  ]
    .filter(Boolean)
    .join(' ')
}

const NO_AUTH_RULE =
  'CRITICAL: this first build must need NO external account or OAuth (no Gmail, no Slack, no Google sign-in) — connectors get wired later, on their request. Everything else is fair game and the more visible the better: web research with the browser shown to the user as you work, scripts, computer use, a small app, a file-based tracker, a scheduled reminder, a generated page. If the idea needs an account, build the no-auth core first and say the connection is a later step.'

/** The one first job we script end to end. Setting up a machine is the task a
 *  brand-new user most wants and can least brief, so the agent does the
 *  briefing: look first, propose, then install with consent. Audit-before-plan
 *  is the load-bearing part — a plan invented before looking is how an agent
 *  ends up installing a second copy of something, or "fixing" drivers that
 *  were already fine. */
const MACHINE_SETUP_RUNBOOK = [
  'THIS IS A MACHINE SETUP JOB: get this computer genuinely ready to use, end to end, with the terminal. It is the one first task that does not need an account anywhere — never send them to a sign-in to complete it.',
  'START BY LOOKING, NOT PLANNING. Before proposing anything, use the terminal to find out what is actually here: OS name and version, architecture, pending system updates, free disk, which package manager exists (Homebrew / winget / apt / dnf), and which everyday things are already installed (a browser, an editor, git, python, node, docker, and whatever tools they mentioned earlier). On an NVIDIA machine also check the GPU and driver (nvidia-smi) and whether a container runtime and CUDA toolchain are present. Report what you found in a few short lines — plainly, no tables.',
  'THEN PROPOSE, THEN ASK. Turn the gaps into a short numbered plan, cheapest and most obviously useful first: system updates, a package manager if missing, their everyday tools, sane defaults, and only then anything exotic. End that turn with ::ask{question="Want me to run this?" options="Go ahead|Change the list|Just the essentials"} alone as its own paragraph, emitted EXACTLY as written.',
  'THEN WORK IT ONE STEP AT A TIME, saying in one short line what each step is for before you run it. Prefer the official package manager over downloading installers. Never install something they did not agree to, never overwrite existing config without asking first, never disable security settings, and stop and ask the moment anything looks destructive or wants a password you were not given.',
  'Hardware and drivers: on Windows, check for missing/unknown devices and vendor GPU drivers, and say plainly when the OS already has it handled. On macOS, system updates and the App Store cover drivers — say so instead of inventing work. On Linux, check the kernel/driver pairing for the GPU before touching it.',
  'If the machine is Arm (an Arm64 Windows PC, an Apple silicon Mac), architecture is the first thing you check for every install: prefer the native arm64 build, say so when only an emulated x64 one exists, and never assume a tool has an Arm release because it is popular. On an Arm Windows PC with NVIDIA silicon, treat CUDA and anything GPU-adjacent as arm64-specific — verify the build before installing it.',
  'Anything that genuinely needs their sign-in, a licence key, or a payment: do not attempt it. Collect those into a short "yours to do" list for the end.',
  'FINISH with a few lines: what changed, what you skipped and why, and what is left for them. If a reboot is needed, say so plainly.'
]

/** The same runbook, opening with what the app already knows about the machine
 *  — freshness first. That fact decides whether the job is an afternoon of real
 *  work or a tour of things already handled, and the agent should not spend its
 *  first two turns discovering what one IPC already answered. */
function machineSetupRunbook(): string[] {
  const description = machineDescription()

  return description
    ? [`What the app can already see about it: ${description}.`, ...MACHINE_SETUP_RUNBOOK]
    : MACHINE_SETUP_RUNBOOK
}

/** Seed rows for the build session's session.create — just the hidden runbook;
 *  the visible go-signal (the task brief) is submitted as a real turn right
 *  after, which is what starts the build. */
export function buildFirstTaskSeedMessages(
  task: string,
  answers: WizardAnswers,
  plan: HandoffPlan = 'build'
): { content: string; display_kind?: 'hidden'; role: 'assistant' | 'user' }[] {
  return [{ content: buildFirstTaskRunbook(task, answers, plan), display_kind: 'hidden', role: 'user' }]
}

/** The hidden note whispered into the Setup chat once the build session is
 *  live — Setup's cue to close the loop and stand down. The check-ins that
 *  follow are driven by the build's own progress (see first-build.ts), not by
 *  a schedule Setup has to remember to create. */
export function buildHandoffCompleteNote(task: string): string {
  return `[setup] handoff complete — "${task.trim()}" is now building in its own session, and the user is watching it there. Say ONE short line and then stop: you're around if they want a hand, and this chat stays where it is. Do not ask a question, do not offer a list, do not schedule anything.`
}

/** The hidden note when opening the build session failed — Setup falls back to
 *  building in its own chat, so the flow never dead-ends. */
export function buildHandoffFailedNote(task: string): string {
  return `[setup] handoff failed — the separate session could not be created. Start the task ("${task.trim()}") right here in this conversation instead: begin the work now, mention the permissions note, and place ::onboarding{step="progress" title="…"} cards as you go.`
}

// ── gateway helpers (called from the wiring's kickoff + handoff effects) ─────

function isAlreadyExists(error: unknown): boolean {
  return error instanceof Error && /exist/i.test(error.message)
}

/** The guided chat's model. Every guided turn is a short scripted beat, and
 *  the cards LOCK (inert) until the turn settles — so model latency is dead UI
 *  in the user's hands, not just a slow reply. A live run on a thinking default
 *  left the colour card unclickable for five minutes.
 *
 *  Pinned on the hermes-setup profile so it holds no matter what the user runs
 *  Hermes as elsewhere, and the composer hides its model pill here: this lane
 *  is ours, not a choice we are offering them.
 *
 *  MINIMAL reasoning, not 'none': with the channel fully closed the model
 *  plans in VISIBLE prose instead (live runs: walls of "Let me re-read the
 *  steps…"). Minimal gives that planning a hidden home while staying fast. */
export const FAST_LANE = {
  model: 'deepseek/deepseek-v4-flash-0731',
  provider: 'nous',
  reasoningEffort: 'minimal'
} as const

const FAST_LANE_MODEL = `${FAST_LANE.model} --provider ${FAST_LANE.provider}`

/**
 * Put a guided session on the fast lane — session scope first so it takes
 * effect on this very turn, then global so later turns here stay there.
 *
 * This is the ONLY route for an ADOPTED chat, which already exists and cannot
 * be re-born. A chat being created must not rely on it: the desktop stamps the
 * composer's current model onto every `session.create`, so a new guided chat
 * starts on the user's default and this switch is a race against its own first
 * turn — which is what left a fresh run reading "opus 5" in the picker. Creates
 * pass `FAST_LANE` through `session.create` and are born correct; this then
 * only moves the profile default.
 *
 * Scoped to the ACTIVE backend, which during onboarding is the hermes-setup
 * profile, so the user's real default is never touched.
 *
 * `confirm_expensive_model` is required: with no agent built yet the switch
 * otherwise answers `confirm_required` (a selection warning) instead of
 * switching. Failures are survivable — the profile default still works — but
 * they are NOT silent: a swallowed refusal here is indistinguishable from a
 * slow model, which is the whole bug this exists to prevent.
 */
export async function pinFastLane(request: GatewayRequest, sessionId: string): Promise<void> {
  // `model` carries its scope as a flag in the value; `reasoning` takes a
  // `scope` param. Mirrored rather than unified — this is the gateway's shape.
  const set = (label: string, params: Record<string, unknown>) =>
    request('config.set', { session_id: sessionId, ...params }).catch(error => {
      console.warn(`[setup] fast lane ${label} refused — guided turns stay on the profile default`, error)
    })

  await set('model', { confirm_expensive_model: true, key: 'model', value: `${FAST_LANE_MODEL} --session` })
  await set('reasoning', { key: 'reasoning', value: 'minimal' })
  await set('model (global)', { confirm_expensive_model: true, key: 'model', value: `${FAST_LANE_MODEL} --global` })
  await set('reasoning (global)', { key: 'reasoning', scope: 'global', value: 'minimal' })
}

/** Make sure the `hermes-setup` profile exists (idempotent — an existing one
 *  is adopted). Returns false when the backend can't create profiles at all;
 *  the kickoff then falls back to the profile-less guided chat. */
export async function ensureSetupProfile(request: GatewayRequest): Promise<boolean> {
  try {
    await request('profiles.create', {
      description: 'Where Hermes met you — walks your first run, then checks in as you find your feet.',
      name: SETUP_PROFILE,
      share_auth: true,
      soul: composeSetupSoul()
    })
  } catch (error) {
    if (!isAlreadyExists(error)) {
      return false
    }
  }

  return true
}
