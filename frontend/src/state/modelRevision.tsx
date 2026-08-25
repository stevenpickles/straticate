/**
 * A counter that says "something about the installed models changed somewhere
 * else in this page".
 *
 * The model library and the configure step read the same models over the same
 * routes, but they are separate component trees with separate reads — and the
 * workflow is only *hidden* while the library is open (`App.tsx`), never
 * unmounted, so it does not re-read on the way back the way a remounted view
 * would. Install a model from a library card, close the library, and the
 * configure step would still be describing the world as it was when the user
 * left it: the tier priced at "Needs a 870 MB download", "Start separation"
 * disabled for weights that are on disk.
 *
 * So closing the library bumps this number, and a view that derives anything
 * from model state re-reads once when it changes. That is deliberately a
 * **known event, not a timer**: it fires when a user leaves a screen on which
 * they could have installed or removed something, and never on its own.
 *
 * A number rather than a boolean or an event emitter: it is trivially
 * comparable ("is this the value I last acted on?"), it needs no subscription
 * to clean up, and React already re-renders consumers when it changes.
 *
 * This is **not** workflow state. It is not a phase, it is not persisted
 * across a reload (feature 033), and nothing in the reducer may branch on it —
 * which is why it lives here and not in `appState.tsx`.
 */

import { createContext, useContext, type ReactNode } from 'react'

/**
 * The context's default is `0`, so a component rendered without the provider
 * (every unit test that mounts one in isolation) simply never sees a change.
 */
const ModelRevisionContext = createContext(0)

/** Props of {@link ModelRevisionProvider}. */
export interface ModelRevisionProviderProps {
  /** The current revision; bump it when models may have changed elsewhere. */
  readonly revision: number
  readonly children: ReactNode
}

/** Publish the current model revision to the tree. */
export function ModelRevisionProvider({
  revision,
  children,
}: ModelRevisionProviderProps) {
  return (
    <ModelRevisionContext.Provider value={revision}>
      {children}
    </ModelRevisionContext.Provider>
  )
}

/**
 * The current model revision. It changes only when another view has had the
 * chance to install or remove weights; treat a change as "re-read once", never
 * as a value to render.
 */
export function useModelRevision(): number {
  return useContext(ModelRevisionContext)
}
