import { useCallback, useEffect, useRef } from 'react'
import { ApiError } from '../api/client'
import { createJob } from '../api/jobs'
import { listSeparationModes } from '../api/modes'
import { useAppDispatch, useAppState } from '../state/appState'
import { useJobDispatch } from '../state/jobState'
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

/** DOM id of the radio input for `kind`/`id`, and of its description. */
function optionId(kind: string, id: string): string {
  return `separation-${kind}-${id}`
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
  const creating = create.status === 'creating'
  const canStart =
    !creating && audioId !== null && modeId !== null && qualityId !== null

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
              {selectedMode.quality_options.map((option) => (
                <div className="separation-option" key={option.id}>
                  <input
                    type="radio"
                    id={optionId('quality', option.id)}
                    name="separation-quality"
                    value={option.id}
                    checked={option.id === qualityId}
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
                </div>
              ))}
            </fieldset>
          )}

          <button
            type="button"
            className="separation-options-start"
            disabled={!canStart}
            aria-busy={creating}
            onClick={start}
          >
            Start separation
          </button>

          {create.status === 'error' && (
            <p className="separation-options-error" role="alert">
              {create.message}
            </p>
          )}
        </>
      )}
    </section>
  )
}
