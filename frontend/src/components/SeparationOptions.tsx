import { useCallback, useEffect, useRef } from 'react'
import { ApiError } from '../api/client'
import { createJob } from '../api/jobs'
import { listSeparationModes } from '../api/modes'
import { formatFileSize } from '../format'
import { useAppDispatch, useAppState } from '../state/appState'
import { useJobDispatch } from '../state/jobState'
import type { Model } from '../api/types'
import { ModelInstallPanel } from './ModelInstallPanel'
import { ModelLicence } from './ModelLicence'
import { useModelCatalog } from './useModelCatalog'
import {
  installationOf,
  startBlockedReason,
  useModelInstallation,
} from './useModelInstallation'
import './SeparationOptions.css'

/** Fallback message for a rejection that is not an {@link ApiError}. */
const UNKNOWN_ERROR = {
  code: 'unknown_error',
  message: 'Something went wrong. Please try again.',
}

/** Envelope-shaped `{code, message}` for any rejection reason. */
function errorInfo(reason: unknown): { code: string; message: string } {
  return reason instanceof ApiError
    ? { code: reason.code, message: reason.message }
    : UNKNOWN_ERROR
}

/**
 * DOM id of the sentence explaining why "Start separation" is disabled; the
 * button points at it with `aria-describedby`, so the reason is announced and
 * not merely visible.
 */
const START_REASON_ID = 'separation-start-reason'

/** DOM id of the radio input for `kind`/`id`, and of its description. */
function optionId(kind: string, id: string): string {
  return `separation-${kind}-${id}`
}

/**
 * What a quality tier's weights cost, in a phrase, or `null` when there is
 * nothing worth saying — a model that needs no download, or one the catalog
 * read has not answered for yet.
 *
 * This is feature 037's answer to the question open since feature 010 — *no*,
 * a mode must not hide tiers whose weights are missing — made visible instead
 * of merely decided. A hidden tier is a product that silently differs from one
 * machine to the next, and on a default server it would leave the configure
 * step with nothing in it at all. A tier that says what it would cost is
 * something a user can act on; and since feature 035 an uninstalled tier can
 * no longer produce a surprise, because Start is disabled with a stated reason
 * until its weights are there.
 *
 * The phrase is derived from the catalog, so **every** tier is priced, not
 * only the selected one — which is what makes "see it, price it, install it"
 * true of a list rather than of one radio button at a time.
 */
function tierWeightsNote(model: Model | undefined): string | null {
  const block = installationOf(model ?? null)
  if (block === null || !block.requires_download) {
    return null
  }
  switch (block.state) {
    case 'installed':
      return 'Installed'
    case 'downloading':
      return 'Downloading its weights…'
    case 'failed':
      return 'Its last install failed'
    default: {
      const total = block.total_bytes ?? null
      return total === null
        ? 'Needs a weights download'
        : `Needs a ${formatFileSize(total)} download`
    }
  }
}

/**
 * The `configure` step of the workflow: choose **what** to separate (a
 * separation mode) and **how well** (a quality tier), then start the job.
 *
 * Everything rendered here comes from `GET /api/v1/separation-modes` — mode
 * names, stem lists and tier labels alike. The component knows nothing about
 * how many stems a mode has or which tiers exist (ARCHITECTURE.md §9), so a
 * catalog gaining a mode, a stem or a tier needs no change in this file.
 *
 * A tier's model may still need its weights: they are never shipped in the
 * repository (ARCHITECTURE.md §9), and since feature 032 no development
 * fixture stands in for one. When the selected tier's model reports weights it
 * does not have, `<ModelInstallPanel>` says so with the download size and
 * offers to install it, and "Start separation" is disabled with a visible,
 * announced reason until the weights are there. Everything else on this screen
 * stays usable while a download runs — the transfer belongs to the backend.
 *
 * Feature 037 added the two things that were missing from that. **Every** tier
 * is priced from the model catalog, not only the selected one, which is what
 * makes "no, do not hide tiers whose weights are missing" an answer a user can
 * act on rather than a decision recorded in a document. And the selected
 * model's **licence** is rendered here — code terms, weights terms, what is
 * and is not permitted, and the attribution the licence requires — because
 * this is where the model is chosen, and the moment before an install is the
 * only one at which those terms can still change the decision.
 *
 * Starting a separation posts the selection to `POST /api/v1/jobs` **without**
 * a `device_id`, letting the backend pick the best compute device (its
 * response echoes the resolved one), tracks the returned job in the job store,
 * and advances the workflow to the `separate` phase.
 *
 * Must be rendered under an `AppStateProvider` and a `JobStateProvider`.
 */
export function SeparationOptions() {
  const { upload, configure } = useAppState()
  const dispatch = useAppDispatch()
  const jobDispatch = useJobDispatch()
  const requestedRef = useRef(false)
  const creatingRef = useRef(false)

  const loadModes = useCallback(() => {
    requestedRef.current = true
    dispatch({ type: 'configure/modesRequested' })
    listSeparationModes()
      .then((modes) => {
        dispatch({ type: 'configure/modesLoaded', modes })
      })
      .catch((reason: unknown) => {
        dispatch({ type: 'configure/modesFailed', ...errorInfo(reason) })
      })
  }, [dispatch])

  // Load the catalog once per mount; the retry button reloads on demand.
  useEffect(() => {
    if (!requestedRef.current) {
      loadModes()
    }
  }, [loadModes])

  const { modes, modeId, qualityId, create } = configure
  const audioId = upload.status === 'uploaded' ? upload.file.id : null
  const selectedMode =
    modes.status === 'loaded'
      ? modes.modes.find((mode) => mode.id === modeId)
      : undefined
  const selectedOption = selectedMode?.quality_options.find(
    (option) => option.id === qualityId,
  )

  // Each tier names the model backing it, and a catalogued model is not
  // necessarily a ready one: weights are a download (ARCHITECTURE.md §9).
  const installation = useModelInstallation(selectedOption?.model_id ?? null)

  // …and every *other* tier's model is one too. The catalog is read once so
  // each tier can be priced where it is chosen rather than only after it is
  // selected — feature 035's panel describes the selection, and a mode with
  // two uninstalled tiers would otherwise have to be clicked through to find
  // out what each costs. A failed catalog read simply leaves the tiers
  // unannotated: it is an enrichment, never a gate.
  const catalog = useModelCatalog()
  const modelFor = (modelId: string | undefined): Model | undefined =>
    modelId === undefined
      ? undefined
      : catalog.models.find((model) => model.id === modelId)

  // Licensing is read from whichever record is fresher, but it is manifest
  // data and identical in both: the point is that it is on screen *before* an
  // install, which is the only moment its terms can still change the decision.
  const selectedModel = installation.model ?? modelFor(selectedOption?.model_id)

  // The single question "may a separation start?", asked of the whole handle:
  // an unread model is not a ready one, so this also covers the round trip
  // right after entering the step or switching mode.
  const blockedReason = startBlockedReason(installation)

  // Weights can vanish between the check and the job, so `POST /jobs` can
  // still answer `model_weights_missing`. The install panel renders that
  // answer as the actionable state, and the hook drops it as soon as a read
  // supersedes it — so a raw duplicate here would only ever be stale.
  const weightsMissing =
    create.status === 'error' && create.code === 'model_weights_missing'

  const creating = create.status === 'creating'
  const canStart =
    !creating &&
    audioId !== null &&
    modeId !== null &&
    qualityId !== null &&
    blockedReason === null

  const start = () => {
    // The ref, not `create.status`, is what makes a double click a single
    // POST: it flips synchronously, before React has re-rendered.
    if (creatingRef.current) {
      return
    }
    if (audioId === null || modeId === null || qualityId === null) {
      return
    }
    creatingRef.current = true
    dispatch({ type: 'configure/createStarted' })
    // `device_id` is deliberately omitted: the backend picks the best
    // device and echoes the resolved one on the job it returns.
    createJob({ audio_id: audioId, mode_id: modeId, quality_id: qualityId })
      .then((job) => {
        jobDispatch({ type: 'job/track', job })
        dispatch({ type: 'configure/createSucceeded' })
      })
      .catch((reason: unknown) => {
        dispatch({ type: 'configure/createFailed', ...errorInfo(reason) })
        if (
          reason instanceof ApiError &&
          reason.code === 'model_weights_missing'
        ) {
          // The record we based the check on is stale; hand the message to
          // the panel and re-read, so what the user sees next is the real
          // state with the install offered.
          installation.noteWeightsMissing(reason.message)
        }
      })
      .finally(() => {
        creatingRef.current = false
      })
  }

  return (
    <section className="separation-options" aria-label="Separation options">
      {(modes.status === 'idle' || modes.status === 'loading') && (
        <p className="workspace-hint">Loading separation options…</p>
      )}

      {modes.status === 'error' && (
        <div className="separation-options-failure">
          <p className="separation-options-error" role="alert">
            {modes.message}
          </p>
          <button
            type="button"
            className="separation-options-retry"
            onClick={loadModes}
          >
            Try again
          </button>
        </div>
      )}

      {modes.status === 'loaded' && (
        <>
          <fieldset className="separation-options-group">
            <legend className="separation-options-legend">
              Separation mode
            </legend>
            {modes.modes.map((mode) => (
              <div className="separation-option" key={mode.id}>
                <input
                  type="radio"
                  id={optionId('mode', mode.id)}
                  name="separation-mode"
                  value={mode.id}
                  checked={mode.id === modeId}
                  aria-describedby={`${optionId('mode', mode.id)}-stems`}
                  onChange={() => {
                    dispatch({
                      type: 'configure/modeSelected',
                      modeId: mode.id,
                    })
                  }}
                />
                <label
                  className="separation-option-label"
                  htmlFor={optionId('mode', mode.id)}
                >
                  {mode.display_name}
                </label>
                <ul
                  className="separation-option-stems"
                  id={`${optionId('mode', mode.id)}-stems`}
                >
                  {mode.stems.map((stem) => (
                    <li className="separation-option-stem" key={stem}>
                      {stem}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </fieldset>

          {selectedMode !== undefined && (
            <fieldset className="separation-options-group">
              <legend className="separation-options-legend">Quality</legend>
              {selectedMode.quality_options.map((option) => {
                const note = tierWeightsNote(modelFor(option.model_id))
                const noteId = `${optionId('quality', option.id)}-weights`
                return (
                  <div className="separation-option" key={option.id}>
                    <input
                      type="radio"
                      id={optionId('quality', option.id)}
                      name="separation-quality"
                      value={option.id}
                      checked={option.id === qualityId}
                      // The note lives outside the label so the tier's own
                      // name stays its accessible name, and is announced as a
                      // description instead.
                      aria-describedby={note === null ? undefined : noteId}
                      onChange={() => {
                        dispatch({
                          type: 'configure/qualitySelected',
                          qualityId: option.id,
                        })
                      }}
                    />
                    <label
                      className="separation-option-label"
                      htmlFor={optionId('quality', option.id)}
                    >
                      {option.display_name}
                    </label>
                    {note !== null && (
                      <p className="separation-option-weights" id={noteId}>
                        {note}
                      </p>
                    )}
                  </div>
                )
              })}
            </fieldset>
          )}

          {/*
            The terms of the model this tier runs, where the tier is chosen —
            before an install, which is the only moment they can still change
            the decision, and including the attribution the licence requires.
            A credit nobody sees is not a credit given.
          */}
          {selectedModel !== undefined && (
            <ModelLicence model={selectedModel} compact />
          )}

          <ModelInstallPanel installation={installation} />

          <div className="separation-options-start-row">
            <button
              type="button"
              className="separation-options-start"
              disabled={!canStart}
              aria-busy={creating}
              aria-describedby={
                blockedReason === null ? undefined : START_REASON_ID
              }
              onClick={start}
            >
              Start separation
            </button>
            {blockedReason !== null && (
              <p className="separation-options-hint" id={START_REASON_ID}>
                {blockedReason}
              </p>
            )}
          </div>

          {create.status === 'error' && !weightsMissing && (
            <p className="separation-options-error" role="alert">
              {create.message}
            </p>
          )}
        </>
      )}
    </section>
  )
}
