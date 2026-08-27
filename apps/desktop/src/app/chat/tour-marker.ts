/**
 * `data-tour` handles for the chat surfaces — on the primary one only.
 *
 * A tour target has to be UNIQUE in the document. The composer, the send
 * button, the model pill and the transcript are not: side-by-side session tiles
 * mount the same ChatView tree, so a marker written unconditionally appears
 * once per tile. The collector then throws the target away entirely (it only
 * reports a selector that resolves back to the element it found), so the marker
 * stops working at exactly the moment the app gets interesting.
 *
 * The primary chat is the one a tour means, so it is the one that gets the
 * handle. Everything else keeps its `data-slot` and stays addressable by
 * anyone who wants a specific tile.
 */

import { useSessionView } from '@/app/chat/session-view'

/** The handle if this is the primary chat, nothing if it is a tile. */
export function useTourMarker(name: string): string | undefined {
  return useSessionView().kind === 'primary' ? name : undefined
}
