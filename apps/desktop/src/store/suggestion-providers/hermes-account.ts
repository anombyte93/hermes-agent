import { translateNow } from '@/i18n'
import { type ComposerSuggestion, offerSuggestions } from '@/store/composer-suggestions'
import { downloadLocalModel } from '@/store/suggestion-providers/local-model'

/**
 * WHERE THIS SHOULD RUN — the one question the first build eventually reaches.
 *
 * Raised once during that build, after the agent has done enough real work to
 * have earned the ask (see first-build.ts), as two pills side by side: take it
 * with you, or keep it on this machine. Both are real actions with a real cost
 * — an account, or a multi-gigabyte download — which is why both get a pill.
 * Neither is urgent: ignoring the pair leaves the user exactly where they are,
 * mid-task, on whatever they are already using.
 *
 * The strip's ignore ledger does the rest: a pill that appears and dies
 * uninvoked enough times stops being offered.
 */

const PROVIDER = 'hermes-account'

function copyFor(group: string) {
  return (key: string) => translateNow(`composer.${group}.${key}`)
}

function signIn(): ComposerSuggestion {
  const copy = copyFor('hermesAccount')

  return {
    id: 'sign-in',
    provider: PROVIDER,
    icon: 'cloud',
    label: copy('label'),
    tip: copy('tip'),
    // No progress: an OAuth round-trip finishes when the user finishes, and a
    // bar that guesses at that is a bar that lies. It spins.
    invoke: async () => {
      await window.hermesDesktop?.cloud?.login()
    },
    workingLabel: copy('working'),
    workingTip: copy('workingTip'),
    doneLabel: copy('done'),
    doneTip: copy('doneTip')
  }
}

function localModel(): ComposerSuggestion {
  const copy = copyFor('localModel')

  return {
    id: 'download',
    provider: PROVIDER,
    icon: 'desktop-download',
    label: copy('label'),
    tip: copy('tip'),
    invoke: downloadLocalModel,
    workingLabel: copy('working'),
    workingTip: copy('workingTip'),
    doneLabel: copy('done'),
    doneTip: copy('doneTip')
  }
}

/** Offer the pair in `sessionId`, unless this install is already signed in. */
export function offerAccountChoice(sessionId: null | string | undefined): void {
  void Promise.resolve(window.hermesDesktop?.cloud?.status())
    .then(status => {
      if (!status?.signedIn) {
        offerSuggestions(sessionId, PROVIDER, [signIn(), localModel()])
      }
    })
    .catch(() => undefined)
}
