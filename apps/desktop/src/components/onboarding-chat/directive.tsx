/**
 * In-chat onboarding cards — the `::onboarding{step="…"}` transcript
 * directive. The conversational twin of the wizard window: Hermes walks the
 * user through setup in the transcript, and each step's paragraph renders as
 * an interactive picker (same option catalog, same persistence).
 *
 * This module is only the dispatcher. Two tables say what a step means — one
 * writes an answer, the other renders a card — and a step in neither renders
 * nothing, which is the right answer for the model's invisible acks. The cards
 * themselves live in ./cards.
 */

import { useEffect } from 'react'

import { FirstBuildCard, HandoffCard, ProgressCard } from '@/components/onboarding-chat/cards/build'
import { FirstScreenCard } from '@/components/onboarding-chat/cards/first-screen'
import type { CardProps } from '@/components/onboarding-chat/cards/frame'
import { ConnectorsCard, FocusCard, LayoutCard, LookCard } from '@/components/onboarding-chat/cards/setup'
import { advanceSketch, generateModuleCandidates } from '@/store/first-screen-live'
import { $wizardAnswers, setWizardAnswers } from '@/store/onboarding-wizard'

/** Steps that only carry data — the model handing the renderer what the user
 *  said. Each maps to the answer field it writes ('working' is the guided
 *  flow's name for the context answer: same storage, same consumers). */
const DATA_STEPS = {
  context: 'context',
  name: 'name',
  working: 'context'
} as const

/** Steps that render a card. Absent means silent: 'ready' is the model's
 *  invisible ack of the pre-banked greeting, and 'first-screen' is a legacy
 *  emission whose card now rides the context step. */
const STEP_CARDS = {
  connectors: ConnectorsCard,
  first: FirstBuildCard,
  focus: FocusCard,
  handoff: HandoffCard,
  layout: LayoutCard,
  look: LookCard,
  progress: ProgressCard
} satisfies Record<string, (props: CardProps) => React.ReactNode>

type DataStep = keyof typeof DATA_STEPS

/** Writing an answer is an EFFECT, not a render fact. Doing it inline in the
 *  directive's render triggered React's cross-component setState warning and
 *  re-entrant renders (live desktop.log). */
function DataDirective({ step, value }: { step: DataStep; value: string }) {
  const field = DATA_STEPS[step]

  useEffect(() => {
    if (!value || $wizardAnswers.get()[field] === value) {
      return
    }

    setWizardAnswers({ [field]: value })

    // The screen evolves with the conversation: a fresh name retitles the
    // sketch; the context answer is the big one — it fires the module
    // generation (their screen, from their words) and the pane advances to
    // proposals the moment candidates land.
    advanceSketch()

    if (field === 'context') {
      generateModuleCandidates()
    }
  }, [field, value])

  return null
}

export function OnboardingChatDirective({ attrs, streaming }: { attrs: Record<string, string>; streaming: boolean }) {
  const step = attrs.step ?? ''

  if (step in DATA_STEPS) {
    // The context answer also mounts the dashboard card right here, rather
    // than depending on the model remembering to emit a second directive — a
    // live run narrated the card without emitting it and stranded the user
    // with nothing to do.
    return (
      <>
        <DataDirective step={step as DataStep} value={(attrs.value ?? '').trim()} />
        {step === 'context' ? <FirstScreenCard attrs={attrs} locked={streaming} /> : null}
      </>
    )
  }

  const Card = STEP_CARDS[step as keyof typeof STEP_CARDS]

  // Mount as soon as the directive is parsed — returning null until settle
  // grows the transcript by a card when the turn finishes. Keep it inert
  // mid-stream so the growing paragraph can't be clicked through.
  return Card ? <Card attrs={attrs} locked={streaming} /> : null
}
