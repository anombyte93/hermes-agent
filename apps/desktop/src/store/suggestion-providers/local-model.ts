/**
 * PULLING A MODEL DOWN ONTO THIS MACHINE — a stand-in for the real fetch.
 *
 * The app can already TALK to a local endpoint (Ollama, vLLM, anything
 * OpenAI-shaped) but it cannot yet put a model on your disk for you, which is
 * the half a first-run user actually needs: pointing Hermes at a server they
 * have not got is not an answer. That fetch is coming; the offer, the pill, the
 * cancel path and the fill are what this file exists to get right ahead of it.
 *
 * So the progress here is TIME, not bytes. Everything around it is real — the
 * reporting shape, the cancellation, the failure path — and landing the actual
 * download means replacing the body of one function with a byte-counting
 * stream. Nothing else in the flow has to learn a new shape.
 */

/** How long the stand-in takes to "finish". Long enough to watch the pill fill
 *  and to have second thoughts and cancel it; short enough to sit through. */
const STAND_IN_DURATION_MS = 9000

/** Coarse enough to look like a download reporting in chunks — the pill's
 *  animated fill glides between the steps rather than ticking. */
const REPORT_EVERY_MS = 300

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export async function downloadLocalModel(context: {
  cancelled: () => boolean
  progress: (fraction: number) => void
}): Promise<void> {
  const startedAt = Date.now()

  for (;;) {
    if (context.cancelled()) {
      // Rejecting returns the pill to idle so it can be started again. The
      // real fetch will abort its stream and bin the partial file here.
      throw new Error('Local model download cancelled')
    }

    const elapsed = Date.now() - startedAt

    context.progress(Math.min(1, elapsed / STAND_IN_DURATION_MS))

    if (elapsed >= STAND_IN_DURATION_MS) {
      return
    }

    await sleep(REPORT_EVERY_MS)
  }
}
