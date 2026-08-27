/**
 * The guided first run's three moving parts, lifted out of `wiring.tsx`.
 *
 * They belong together and nowhere else: the kickoff opens the welcome chat,
 * the handoff hands its first task to a session of its own, and the check-in
 * whispers back into that session while it works. All three share the same
 * awkward constraint — the user sits on the `hermes-setup` profile while the
 * work lives on `default`, so every RPC here has to say which backend it
 * means instead of riding the active one.
 *
 * `wiring.tsx` is a 1600-line god-file; this is ~330 lines of it that has one
 * subject. Everything the hook needs from the enclosing component arrives as
 * options; everything else it imports directly.
 */

import { useStore } from '@nanostores/react'
import { useCallback, useEffect } from 'react'

import { PROMPT_SUBMIT_REQUEST_TIMEOUT_MS } from '@/api/client'
import type { SessionCreateOverrides } from '@/app/session/hooks/use-session-actions/create-overrides'
import { $chatOnboardingThreadIds, pickOnboardingGreeting, startChatOnboardingSolo } from '@/components/onboarding-chat/assembly'
import { $setupCheckIn, watchFirstBuild } from '@/components/onboarding-chat/first-build'
import {
  $setupHandoff,
  $setupSession,
  buildFirstTaskSeedMessages,
  buildHandoffCompleteNote,
  buildHandoffFailedNote,
  ensureSetupProfile,
  FAST_LANE,
  firstTaskTitle,
  markSetupHandoffDone,
  pinFastLane,
  SETUP_CHAT_TITLE,
  SETUP_PROFILE
} from '@/components/onboarding-chat/setup-profile'
import { declinedLookAround, showProfileSignpost } from '@/components/onboarding-chat/signpost'
import { setActiveTreePane } from '@/components/pane-shell/tree/store'
import { requestGatewayForProfile } from '@/store/gateway'
import { loadMachineProfile } from '@/store/machine'
import { buildChatOnboardingSeedMessages, buildKickoffPrompt } from '@/store/onboarding-script'
import { $wizardAnswers, markGuideKickoffStarted } from '@/store/onboarding-wizard'
import { $newChatProfile, ensureGatewayProfile, normalizeProfileKey } from '@/store/profile'
import {
  $activeSessionId,
  $messages,
  $selectedStoredSessionId,
  setAwaitingResponse,
  setBusy,
  setMessages
} from '@/store/session'

/** The profile the first build lands on: the user's real working context, not
 *  the guide's. Named because it has to be passed EXPLICITLY everywhere — the
 *  active gateway during the handoff is still `hermes-setup`. */
const BUILD_PROFILE = 'default'

/** Derived, not restated: the seed rows this hook passes through are whatever
 *  the builders produce, so the shape can never drift out of sync. */
type SeedMessage = ReturnType<typeof buildChatOnboardingSeedMessages>[number]

export interface OnboardingHandoffOptions {
  createBackendSessionForSend: (
    preview?: null | string,
    seedMessages?: SeedMessage[],
    overrides?: SessionCreateOverrides
  ) => Promise<null | string>
  requestGateway: <T>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>
  resumeSession: (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>
  /** Runs `create` with the session-create leg pinned to `profile` instead of
   *  the selected chat's owner. The caller owns the mechanism (its own
   *  `requestGateway` is what reads the pin); the hook only needs to say which
   *  backend the new session belongs on. */
  runCreatePinnedTo: <T>(profile: string, create: () => Promise<T>) => Promise<T>
}

/** Returns the first-chat kickoff; wires the handoff and check-in effects. */
export function useOnboardingHandoff({
  createBackendSessionForSend,
  requestGateway,
  resumeSession,
  runCreatePinnedTo
}: OnboardingHandoffOptions) {
  // Onboarding handoff: the first chat starts itself.
  // 'guide' seeds the WHOLE opening at session.create — the invisible runbook
  // (display_kind=hidden) plus a PRE-WRITTEN assistant greeting — so the
  // first thing the user sees paints instantly, zero generation wait. No
  // prompt.submit: the model's first real turn is its reply to the user's
  // name. 'greet' keeps the classic hidden-kickoff shape (Hermes generates
  // the opener from the wizard's answers).
  //
  // The guided chat belongs to a persistent `hermes-setup` profile, as an
  // ordinary visible session titled "Welcome to Hermes" — so it outlives
  // onboarding and the user can always come back to it. If the profile can't
  // be created (older backend), the chat falls back to the profile-less shape
  // and everything still works.
  const kickoffFirstChat = useCallback(
    (kind: 'greet' | 'guide' = 'greet') => {
      if (kind === 'guide') {
        // Solo mode: chat-only layout, no statusbar — the app assembles
        // around the conversation when the layout card is answered.
        startChatOnboardingSolo()
      }
      void (async () => {
        // Ask the host what it is BEFORE composing the greeting or the
        // runbook: a machine that is barely out of the box changes what the
        // flow leads with, and the greeting offers the account name as a
        // default. One local IPC — the pick below still beats everything
        // slow (the session create, the first model turn), so the first
        // paint stays instant.
        if (kind === 'guide') {
          await loadMachineProfile()
          // The opening line is PRE-BANKED and on screen before anything
          // else happens — picked here so it can know the machine (and the
          // name suggestion) while still painting long before any turn.
          pickOnboardingGreeting()
        }

        const seedMessages = kind === 'guide' ? buildChatOnboardingSeedMessages(pickOnboardingGreeting()) : undefined
        let onSetupProfile = kind === 'guide' ? await ensureSetupProfile(requestGateway) : false

        if (onSetupProfile) {
          // selectProfile-style: point new chats at the setup profile and make
          // its backend the active gateway BEFORE creating — the guided chat
          // then lives where every later ambient RPC (submit, hydration,
          // streaming) will look for it.
          try {
            $newChatProfile.set(SETUP_PROFILE)
            await ensureGatewayProfile(SETUP_PROFILE)
          } catch (error) {
            console.warn('[setup] profile swap failed', error)
            $newChatProfile.set(null)
            onSetupProfile = false
          }
        }

        if (onSetupProfile) {
          // Adopt before creating: the welcome chat may already exist — a
          // relaunch mid-onboarding, a dev re-kick. Creating a second one can
          // never win: UNIQUE(title) turns the new stamp into a silent no-op
          // and the auto-titler names the stray (a live tab read "BROOKLYN").
          // Exact-title lookup; a hit IS the chat — resume it and pick up
          // wherever the conversation left off.
          const registryHit = await requestGateway<{ sessions?: { id: string; resolved_id?: string }[] }>(
            'session.list',
            { include_hidden: true, title: SETUP_CHAT_TITLE }
          ).catch(() => null)

          const canonical = registryHit?.sessions?.[0]

          if (canonical?.id) {
            markGuideKickoffStarted()
            await resumeSession(canonical.resolved_id ?? canonical.id, true)

            const adoptedRuntimeId = $activeSessionId.get()

            $chatOnboardingThreadIds.set(adoptedRuntimeId ? [canonical.id, adoptedRuntimeId] : [canonical.id])
            $setupSession.set({
              profile: SETUP_PROFILE,
              runtimeId: adoptedRuntimeId ?? canonical.id,
              storedId: canonical.id
            })

            // An adopted chat needs the fast lane just as much as a minted one
            // — it IS the guided chat. Resuming without this put the guide
            // back on the user's thinking default, where a locked card reads
            // as a frozen app.
            await pinFastLane(requestGateway, adoptedRuntimeId ?? canonical.id)

            return
          }
        }

        const runtimeId = await createBackendSessionForSend(
          null,
          seedMessages,
          kind === 'guide' ? { model: FAST_LANE, ...(onSetupProfile ? { title: SETUP_CHAT_TITLE } : {}) } : undefined
        )

        if (!runtimeId) {
          return
        }

        if (kind === 'guide') {
          // The seeded session exists — NOW burn the persistent latch, so a
          // pre-create crash retries the handoff but a relaunch after this
          // resumes the normal app.
          markGuideKickoffStarted()

          // Mark this thread as the guided one (transcript treatment, no git
          // strip). Both ids: the thread list keys by the STORED session id
          // (that's what the route and sidebar select), the composer by the
          // runtime id.
          const storedId = $selectedStoredSessionId.get()

          $chatOnboardingThreadIds.set(storedId ? [storedId, runtimeId] : [runtimeId])

          // Remembered for the handoff's whisper back.
          $setupSession.set({ profile: onSetupProfile ? SETUP_PROFILE : null, runtimeId, storedId })

          // Name the thread NOW, explicitly: the runbook is persisted as a
          // (hidden) user message, and the backend's auto-titler happily
          // derives a title from it (or from the user's first words — a live
          // run's tab read "call me BK"). A manual title holds highest
          // authority (set_session_title), so the titler never reconsiders —
          // and it is the exact title kickoff re-finds this chat by.
          await requestGateway('session.title', { session_id: runtimeId, title: SETUP_CHAT_TITLE }).catch(
            () => undefined
          )

          await pinFastLane(requestGateway, runtimeId)

          // No kickoff prompt.submit: the runbook and the greeting are seeded
          // rows, and the greeting the user watches type itself in is the
          // banked line (thread list) — the model's first real turn is its
          // reply to the user's name.
          return
        }

        // 'greet' only from here: guide returned above (its opening is
        // seeded, not prompted).
        setAwaitingResponse(true)
        setBusy(true)

        await requestGateway(
          'prompt.submit',
          {
            display_kind: 'hidden',
            session_id: runtimeId,
            text: buildKickoffPrompt($wizardAnswers.get())
          },
          PROMPT_SUBMIT_REQUEST_TIMEOUT_MS
        ).catch(() => {
          setAwaitingResponse(false)
          setBusy(false)
        })
      })()
    },
    [createBackendSessionForSend, requestGateway, resumeSession]
  )

  // Handoff: the guide decided the first task (the HandoffCard raised
  // $setupHandoff) — open a session for it on the user's default profile,
  // seed it with the work-side runbook, move the user there, and start the
  // build from the brief as a real visible turn. The welcome chat stays
  // alive: it gets a hidden [setup] note so it can close the loop. On any
  // failure it is told to build in its own chat instead, so the flow never
  // dead-ends.
  const setupHandoff = useStore($setupHandoff)

  useEffect(() => {
    if (setupHandoff?.phase !== 'pending') {
      return
    }

    const { brief, plan, task } = setupHandoff

    $setupHandoff.set({ ...setupHandoff, phase: 'opening' })

    void (async () => {
      const setupSession = $setupSession.get()

      // Read the tour answer NOW: the guide transcript is what carries it, and
      // in a few lines the switch replaces it with the build session's.
      const signpost = !declinedLookAround($messages.get())

      // The guide chat lives on the setup profile's own backend; by whisper
      // time the ACTIVE gateway is the default profile's, so route explicitly.
      const whisperToSetup = (text: string) => {
        if (!setupSession) {
          return
        }

        const params = { display_kind: 'hidden', session_id: setupSession.runtimeId, text }

        void (
          setupSession.profile
            ? requestGatewayForProfile(setupSession.profile, 'prompt.submit', params, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
            : requestGateway('prompt.submit', params, PROMPT_SUBMIT_REQUEST_TIMEOUT_MS)
        ).catch(() => undefined)
      }

      const previousNewChatProfile = $newChatProfile.get()

      try {
        const chatTitle = firstTaskTitle(task)

        // The build belongs in the user's normal working context, so target
        // BUILD_PROFILE explicitly — the active gateway is the guide's own
        // profile right now, and a null target means "keep the current one".
        $newChatProfile.set(null)
        await ensureGatewayProfile(BUILD_PROFILE)

        // The create must ride the profile just ensured — NOT the
        // session-owner route, which resolves the owner from the SELECTED
        // chat (the guide's, profile hermes-setup, since the user is still
        // sitting in it) and pinned the session.create onto that socket
        // regardless of the swap above. Pinning dispatches the create leg over
        // the intended profile's own connection handle, the same explicit
        // routing the whisper already uses for hermes-setup.
        const runtimeId = await runCreatePinnedTo(BUILD_PROFILE, () =>
          createBackendSessionForSend(brief, buildFirstTaskSeedMessages(task, $wizardAnswers.get(), plan), {
            title: chatTitle
          })
        )

        if (!runtimeId) {
          throw new Error('task session create failed')
        }

        await verifyLandedOnBuildProfile(requestGateway, chatTitle, runtimeId)

        // The build lands in the normal app: the git strip and the user's own
        // panels belong here, so the thread stays out of the onboarding set,
        // and the sidebar goes back to the Sessions list the new row is in.
        setActiveTreePane('sessions')

        // Watch the build's own progress so the check-ins ride what is
        // actually happening rather than a clock (see first-build.ts).
        watchFirstBuild(runtimeId, BUILD_PROFILE)

        // The go signal — the brief lands as the user's first visible turn in
        // the new chat, and the build starts from it. Painted optimistically
        // (same trick as the guide greeting) so the new chat never opens empty
        // while the submit round-trips. Routed explicitly over the intended
        // profile — the session lives on THAT backend; the owner route would
        // re-derive the target from selection state that a create response
        // without a stored id leaves parked on the guide's chat.
        setMessages(current => [
          ...current,
          {
            id: `handoff-brief-${runtimeId}`,
            parts: [{ text: brief, type: 'text' }],
            role: 'user',
            timestamp: Date.now() / 1000
          }
        ])
        setAwaitingResponse(true)
        setBusy(true)
        await requestGatewayForProfile(
          BUILD_PROFILE,
          'prompt.submit',
          { session_id: runtimeId, text: brief },
          PROMPT_SUBMIT_REQUEST_TIMEOUT_MS
        ).catch(() => {
          setAwaitingResponse(false)
          setBusy(false)
        })

        markSetupHandoffDone()
        $setupHandoff.set({ brief, phase: 'done', plan, sessionTitle: chatTitle, task })
        whisperToSetup(buildHandoffCompleteNote(task))

        // Last: the rail lights up to say the guide chat is still there. Not
        // awaited — it waits on a render, and the build is already running.
        if (signpost) {
          void showProfileSignpost()
        }
      } catch {
        // Undo the half-swap so the user's chat context stays with the guide.
        $newChatProfile.set(previousNewChatProfile)
        $setupHandoff.set({ brief, phase: 'error', plan, task })
        whisperToSetup(buildHandoffFailedNote(task))
      }
    })()
  }, [createBackendSessionForSend, requestGateway, runCreatePinnedTo, setupHandoff])

  // The during-task check-in: first-build.ts decides WHEN (see its header),
  // this delivers it — a hidden note into the build's own session, which the
  // agent answers as a short status plus one ask.
  const checkIn = useStore($setupCheckIn)

  useEffect(() => {
    if (!checkIn) {
      return
    }

    // Routed by profile, not over the active gateway: the user can be sitting
    // back in the welcome chat when a check-in comes due, which would send the
    // note to the wrong backend entirely.
    void requestGatewayForProfile(
      checkIn.profile,
      'prompt.submit',
      { display_kind: 'hidden', session_id: checkIn.sessionId, text: checkIn.note },
      PROMPT_SUBMIT_REQUEST_TIMEOUT_MS
    ).catch(() => undefined)
  }, [checkIn])

  return kickoffFirstChat
}

/**
 * Never-dead-end contract: the created chat must exist on the build profile's
 * backend, or the handoff would "succeed" into a session the sidebar scope
 * hides and the wrong persona owns. A positive mismatch — the row missing from
 * that backend's registry, or tagged with another profile — is a failed
 * authoritative write, so it throws and the caller falls back to building in
 * the welcome chat. A failed verification READ stays fail-open: a transient
 * list error must not destroy a create that already succeeded.
 */
async function verifyLandedOnBuildProfile(
  requestGateway: OnboardingHandoffOptions['requestGateway'],
  chatTitle: string,
  runtimeId: string
): Promise<void> {
  const storedId = $selectedStoredSessionId.get()

  type SessionRow = { id?: string; profile?: string; resolved_id?: string }

  const verification = await requestGatewayForProfile<{ sessions?: SessionRow[] }>(BUILD_PROFILE, 'session.list', {
    include_hidden: true,
    title: chatTitle
  }).catch(() => null)

  if (!verification?.sessions) {
    return
  }

  const row = verification.sessions.find(
    (candidate: SessionRow) => candidate.id === (storedId ?? runtimeId) || candidate.resolved_id === runtimeId
  )

  const rowProfile = row?.profile == null ? null : normalizeProfileKey(row.profile)

  if (row && (rowProfile === null || rowProfile === normalizeProfileKey(BUILD_PROFILE))) {
    return
  }

  // Same shape as the mid-create drift abort: close the misrouted session
  // best-effort before surfacing the failure.
  void requestGatewayForProfile(BUILD_PROFILE, 'session.close', { session_id: runtimeId }).catch(() => undefined)

  throw new Error(`handoff create landed on the wrong profile (wanted "${BUILD_PROFILE}", got "${rowProfile ?? 'missing'}")`)
}
