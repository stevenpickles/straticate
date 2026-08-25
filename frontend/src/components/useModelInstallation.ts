/**
 * Watching one model's weights: are they on disk, are they arriving, and did
 * the last attempt fail.
 *
 * Weights are never shipped in the repository (ARCHITECTURE.md §9) and feature
 * 032 stopped offering the development fixtures as quality tiers, so the tier a
 * fresh checkout preselects is backed by a model whose bytes are still on the
 * network. This hook is what lets the configure step say so, offer an Install,
 * and know when Start may be pressed.
 *
 * **Progress is polled, deliberately.** Feature 025 put install progress on the
 * model resource rather than on the event hub, and wrote down why
 * (`docs/features/025-model-download-manager.md`): an install is rare,
 * user-initiated and coarse-grained, REST is already the source of truth for
 * reconnect and refresh (ARCHITECTURE.md §11), and an event would have been a
 * shared-contract change with no consumer. AGENTS.md principle 3 forbids
 * polling for *job* progress — chunk-grained real work with an event stream of
 * its own — not for re-reading a resource while its own download runs.
 *
 * The loop is bounded on every side: it runs only while the model reports
 * `downloading`, stops on any terminal state, stops while the tab is hidden
 * (and re-reads immediately when it comes back), stops on a failed read, and is
 * cleared when the component unmounts or selects a different model.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import { getModel, installModel } from '../api/models'
import type { Model, ModelInstallation } from '../api/types'

/**
 * How long to wait between reads of `GET /models/{id}` while a download runs.
 *
 * One second. The artifact is hundreds of megabytes — the real `vocals-hq-001`
 * weights are 870 MiB, which is a minute or more on a fast connection and much
 * longer on a slow one — so 1 Hz still draws a smooth bar and reports the
 * outcome within a second of it happening. Faster would buy nothing a human can
 * see while multiplying requests against a server that is, on the same event
 * loop, writing the download to disk (feature 025 keeps the per-chunk write
 * there on purpose). Slower would make the settle to `installed` or `failed`
 * feel unresponsive, since that transition is what the Start button waits on.
 */
export const POLL_INTERVAL_MS = 1000

/** Envelope-shaped `{code, message}` — a backend failure, or a local fallback. */
export interface InstallationError {
  readonly code: string
  readonly message: string
}

/** Fallback for a rejection that is not an {@link ApiError}. */
const UNKNOWN_ERROR: InstallationError = {
  code: 'unknown_error',
  message: 'Something went wrong. Please try again.',
}

/** Envelope-shaped `{code, message}` for any rejection reason. */
function errorInfo(reason: unknown): InstallationError {
  return reason instanceof ApiError
    ? { code: reason.code, message: reason.message }
    : UNKNOWN_ERROR
}

/** State of reading one model's installation block. */
export type ModelInstallationStatus = 'idle' | 'loading' | 'loaded' | 'error'

/** What {@link useModelInstallation} hands back. */
export interface ModelInstallationHandle {
  /** The model being watched, or `null` when no tier is selected yet. */
  readonly modelId: string | null
  /** The most recent model record, or `null` before the first read succeeds. */
  readonly model: Model | null
  /** State of the read. */
  readonly status: ModelInstallationStatus
  /**
   * Why the last **request** failed — a read that did not answer, or an
   * install that was refused. A download that failed after starting is not
   * this: it rides on `model.installation.error`.
   */
  readonly error: InstallationError | null
  /** Whether a `POST /install` is in flight (it returns in milliseconds). */
  readonly installing: boolean
  /** Start (or retry) the download. Ignored while a request is in flight. */
  readonly install: () => void
  /** Re-read the model now — after a `model_weights_missing`, or after a failed read. */
  readonly refresh: () => void
}

/**
 * What is known about **one** model, tagged with which model that is.
 *
 * Carrying the ID in the state is what lets a changed selection be a pure
 * derivation ("this record is not about the model you are asking about, so you
 * are loading") rather than an effect that resets state and re-renders.
 */
interface HookState {
  readonly modelId: string | null
  readonly model: Model | null
  readonly error: InstallationError | null
  readonly installing: boolean
}

const IDLE_STATE: HookState = {
  modelId: null,
  model: null,
  error: null,
  installing: false,
}

/**
 * Fold a settled request for `modelId` into the state, dropping anything that
 * described a different model.
 */
function applyRead(
  previous: HookState,
  modelId: string,
  patch: Partial<HookState>,
): HookState {
  const same = previous.modelId === modelId
  return {
    modelId,
    model: same ? previous.model : null,
    error: same ? previous.error : null,
    installing: same && previous.installing,
    ...patch,
  }
}

/** The installation block of a model, or `null` when there is no model yet. */
export function installationOf(model: Model | null): ModelInstallation | null {
  return model?.installation ?? null
}

/**
 * Whether this model's weights stand between the user and a separation:
 * it has a downloadable artifact and that artifact is not installed.
 *
 * A model with `requires_download: false` — every built-in separator — is
 * installed by definition and never answers `true`.
 */
export function needsInstall(model: Model | null): boolean {
  const installation = installationOf(model)
  return (
    installation !== null &&
    installation.requires_download &&
    installation.state !== 'installed'
  )
}

/**
 * Why "Start separation" cannot be pressed, in one sentence, or `null` when
 * nothing about the model's weights is in the way.
 *
 * A model that could not be read at all is deliberately **not** a reason: the
 * user is not blocked on a fact the client failed to fetch, and `POST /jobs`
 * answers `model_weights_missing` if the weights really are absent.
 */
export function startBlockedReason(model: Model | null): string | null {
  const installation = installationOf(model)
  if (installation === null || !needsInstall(model)) {
    return null
  }
  switch (installation.state) {
    case 'downloading':
      return 'The model weights are still downloading.'
    case 'failed':
      return 'The model weights could not be installed. Retry the install to continue.'
    default:
      return 'This quality tier needs its model weights installed first.'
  }
}

/**
 * Watch (and install) the weights of one model, by ID.
 *
 * Reads the model once per selection, polls while a download runs, and exposes
 * an {@link ModelInstallationHandle.install} action. Pass `null` while no tier
 * is selected: nothing is fetched and nothing is scheduled.
 */
export function useModelInstallation(
  modelId: string | null,
): ModelInstallationHandle {
  const [state, setState] = useState<HookState>(IDLE_STATE)
  const [readCount, setReadCount] = useState(0)
  const [hidden, setHidden] = useState(
    () => document.visibilityState === 'hidden',
  )

  // Responses are applied only if they belong to the model still selected: a
  // user may switch tiers while a read is in flight.
  const modelIdRef = useRef(modelId)
  useEffect(() => {
    modelIdRef.current = modelId
  }, [modelId])

  // Flips synchronously, before React re-renders, so a double click on
  // "Install" is a single POST (the same guard `SeparationOptions` uses for
  // "Start separation").
  const installingRef = useRef(false)

  const refresh = useCallback(() => {
    setReadCount((count) => count + 1)
  }, [])

  // Read the model: once per selection, and again on every refresh (which is
  // what the poll timer, the tab regaining focus, and the retry button all do).
  useEffect(() => {
    if (modelId === null) {
      return
    }
    let cancelled = false
    getModel(modelId)
      .then((model) => {
        if (!cancelled) {
          setState((previous) =>
            applyRead(previous, modelId, { model, error: null }),
          )
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setState((previous) =>
            applyRead(previous, modelId, { error: errorInfo(reason) }),
          )
        }
      })
    return () => {
      cancelled = true
    }
    // `readCount` is the refresh trigger; the request itself depends only on
    // the model ID.
  }, [modelId, readCount])

  // Everything below describes the model that is *selected right now*. A
  // record left over from another tier is not shown and not polled — no reset
  // effect, no extra render.
  const current = state.modelId === modelId && modelId !== null ? state : null
  const model = current?.model ?? null
  const error = current?.error ?? null

  // The poll. It re-runs on every *successful* read, because each one lands a
  // fresh record; a read that fails leaves the record — and so this effect —
  // untouched, which stops the loop rather than hammering a backend that is
  // not answering. The retry button starts it again.
  useEffect(() => {
    if (model?.installation?.state !== 'downloading' || hidden) {
      return
    }
    const timer = setTimeout(refresh, POLL_INTERVAL_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [model, hidden, refresh])

  // A hidden tab is not watching a progress bar. Stop polling while it is
  // backgrounded and read once immediately when it returns, so the first thing
  // the user sees is current rather than up to a second stale.
  useEffect(() => {
    const onVisibilityChange = () => {
      const isHidden = document.visibilityState === 'hidden'
      setHidden(isHidden)
      if (!isHidden) {
        refresh()
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [refresh])

  const install = useCallback(() => {
    const id = modelId
    if (id === null || installingRef.current) {
      return
    }
    installingRef.current = true
    setState((previous) =>
      applyRead(previous, id, { installing: true, error: null }),
    )
    installModel(id)
      .then((installed) => {
        if (modelIdRef.current === id) {
          setState((previous) =>
            applyRead(previous, id, {
              model: installed,
              error: null,
              installing: false,
            }),
          )
        }
      })
      .catch((reason: unknown) => {
        if (modelIdRef.current === id) {
          setState((previous) =>
            applyRead(previous, id, {
              error: errorInfo(reason),
              installing: false,
            }),
          )
        }
      })
      .finally(() => {
        installingRef.current = false
      })
  }, [modelId])

  let status: ModelInstallationStatus = 'idle'
  if (modelId !== null) {
    // A failure the user may need to act on outranks a record that may be a
    // second old; a record with neither is still on its way.
    status = error !== null ? 'error' : model !== null ? 'loaded' : 'loading'
  }

  return {
    modelId,
    model,
    status,
    error,
    installing: current?.installing ?? false,
    install,
    refresh,
  }
}
