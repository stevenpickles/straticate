import { useEffect, useRef } from 'react'
import { ApiError } from '../api/client'
import { cancelJob } from '../api/jobs'
import type { JobState } from '../api/types'
import { formatDuration } from '../format'
import { useAppDispatch } from '../state/appState'
import {
  isTerminalJobState,
  useJobDispatch,
  useJobState,
} from '../state/jobState'
import type { ConnectionStatus } from '../ws/client'
import './SeparationProgress.css'

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
 * Humanize a snake_case contract identifier for display: `loading_model`
 * becomes `Loading model`, `post_processing` becomes `Post processing`.
 *
 * Derived rather than tabulated on purpose — the job state machine is a
 * contract the backend owns, and a hand-written label map would silently
 * render nothing for a state added later (ARCHITECTURE.md §6).
 */
function humanize(identifier: string): string {
  const words = identifier.replaceAll('_', ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Same as {@link humanize}, for use mid-sentence (`loading model`). */
function humanizeInline(identifier: string): string {
  return identifier.replaceAll('_', ' ').trim()
}

/** What a non-`open` socket status means for the person watching. */
const CONNECTION_MESSAGES: Record<ConnectionStatus, string | null> = {
  open: null,
  connecting: 'Connecting for live progress…',
  reconnecting: 'Live progress interrupted — reconnecting…',
  closed: 'Live progress is disconnected.',
}

/** Clamp a `0..1` fraction to a whole percentage. */
function toPercent(fraction: number): number {
  if (!Number.isFinite(fraction)) {
    return 0
  }
  return Math.min(100, Math.max(0, Math.round(fraction * 100)))
}

/** A labelled read-only value row. */
function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="separation-progress-field">
      <span className="separation-progress-label">{label}</span>
      <span className="separation-progress-value">{value}</span>
    </div>
  )
}

/**
 * The `separate` step of the workflow: live, chunk-grained progress for the
 * tracked job, the stage it is in, a cancel affordance, and every terminal
 * outcome.
 *
 * Progress is real work — `chunks_completed / chunks_total` as pushed by
 * `job_progress` (AGENTS.md principle 3) — with the job record's own
 * `progress` as the fallback before the first event arrives. Nothing here
 * animates in place of work.
 *
 * Cancellation is a **request**, not a stop: `POST /jobs/{id}/cancel` may
 * answer with a job that is still processing, so the button settles into a
 * "cancelling" affordance and the authoritative transition is the
 * `job_cancelled` event (`docs/contracts/rest-api.md`).
 *
 * A completed job offers "View results", which advances the workflow to the
 * `inspect` phase and the stem player; every terminal job offers "Start
 * another separation", which stops tracking it and returns to `configure`
 * with the same uploaded file (both added by feature 023).
 *
 * Must be rendered under an `AppStateProvider` and a `JobStateProvider`; the
 * socket itself is opened once per session by `JobEventBridge` (see
 * `src/ws/JobEventBridge.tsx`).
 */
export function SeparationProgress() {
  const { job, progress, cancelledAtStage, connection, cancel } = useJobState()
  const dispatch = useJobDispatch()
  const appDispatch = useAppDispatch()
  const cancellingRef = useRef(false)

  // Read at settlement time, not from the closure: the user may have started
  // a different separation while the cancel was in flight.
  const trackedJobIdRef = useRef<string | null>(null)
  useEffect(() => {
    trackedJobIdRef.current = job?.id ?? null
  }, [job])

  const requestCancel = () => {
    // The ref, not `cancel.status`, is what makes a double click a single
    // POST: it flips synchronously, before React has re-rendered.
    if (cancellingRef.current || job === null) {
      return
    }
    cancellingRef.current = true
    const jobId = job.id
    dispatch({ type: 'cancel/requested' })
    cancelJob(jobId)
      .then((updated) => {
        // May still be a processing state; the job_cancelled event is what
        // actually settles the request. The reducer refuses to let this
        // snapshot demote a job the events already carried to a terminal
        // state, so a fast worker cannot be un-cancelled by its own reply.
        if (trackedJobIdRef.current === jobId) {
          dispatch({ type: 'job/track', job: updated })
        }
      })
      .catch((reason: unknown) => {
        // A failure belongs to the job the user asked to cancel; if that is
        // no longer the tracked one, it is not this panel's news to report.
        if (trackedJobIdRef.current === jobId) {
          dispatch({ type: 'cancel/failed', ...errorInfo(reason) })
        }
      })
      .finally(() => {
        cancellingRef.current = false
      })
  }

  /** Advance the workflow to the stem player (feature 023). */
  const viewResults = () => {
    appDispatch({ type: 'results/inspect' })
  }

  /**
   * Start over with the same uploaded file: stop tracking the finished job
   * and return to `configure`. Claimed here by feature 023 — 011 and 017
   * both flagged the missing path back as unowned.
   */
  const startAnother = () => {
    dispatch({ type: 'job/clear' })
    appDispatch({ type: 'results/startAnother' })
  }

  if (job === null) {
    return (
      <section className="separation-progress" aria-label="Separation progress">
        <p className="workspace-hint">No separation job is being tracked.</p>
      </section>
    )
  }

  const state: JobState = job.state
  const terminal = isTerminalJobState(state)
  const connectionMessage = CONNECTION_MESSAGES[connection]
  const cancelling = cancel.status === 'requesting'
  const fraction = progress?.progress ?? job.progress
  const percent = toPercent(fraction)
  const stemCount = job.result?.stems.length ?? 0

  return (
    <section className="separation-progress" aria-label="Separation progress">
      <p className="separation-progress-stage">{humanize(state)}</p>

      {state === 'queued' && (
        <p className="workspace-hint">
          Waiting in the queue — this separation has not started yet.
        </p>
      )}

      {!terminal && state !== 'queued' && (
        <>
          <div
            className="progress-track"
            role="progressbar"
            aria-label="Separation progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
          >
            <div
              className="progress-fill"
              style={{ width: `${String(percent)}%` }}
            />
          </div>
          <p className="separation-progress-percent">{percent}%</p>
          {progress !== null && (
            <div className="separation-progress-fields">
              <Field
                label="Chunks"
                value={`${String(progress.chunksCompleted)} / ${String(progress.chunksTotal)}`}
              />
              <Field
                label="Elapsed"
                value={formatDuration(progress.elapsedSeconds)}
              />
              <Field
                label="Audio processed"
                value={`${formatDuration(progress.audioProcessedSeconds)} / ${formatDuration(progress.audioTotalSeconds)}`}
              />
            </div>
          )}
        </>
      )}

      {state === 'completed' && (
        <>
          <p className="workspace-hint">
            {stemCount === 1
              ? 'Separation complete — 1 stem is ready.'
              : `Separation complete — ${String(stemCount)} stems are ready.`}
          </p>
          <button
            type="button"
            className="separation-progress-view"
            onClick={viewResults}
          >
            View results
          </button>
        </>
      )}

      {state === 'cancelled' && (
        <p className="workspace-hint">
          {cancelledAtStage === null
            ? 'Separation cancelled.'
            : `Separation cancelled while ${humanizeInline(cancelledAtStage)}.`}
        </p>
      )}

      {state === 'failed' && (
        <>
          <p className="separation-progress-error" role="alert">
            {job.error?.message ?? 'The separation failed.'}
          </p>
          {job.error !== null && (
            <p className="separation-progress-code">
              Error code: {job.error.code}
            </p>
          )}
        </>
      )}

      {!terminal && (
        <>
          <button
            type="button"
            className="separation-progress-cancel"
            disabled={cancelling}
            aria-busy={cancelling}
            onClick={requestCancel}
          >
            {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
          {cancelling && (
            <p className="workspace-hint" role="status">
              Waiting for the job to stop at its next checkpoint.
            </p>
          )}
        </>
      )}

      {terminal && (
        <button
          type="button"
          className="separation-progress-restart"
          onClick={startAnother}
        >
          Start another separation
        </button>
      )}

      {cancel.status === 'error' && (
        <p className="separation-progress-error" role="alert">
          {cancel.message}
        </p>
      )}

      {connectionMessage !== null && (
        <p className="separation-progress-connection" role="status">
          {connectionMessage}
        </p>
      )}
    </section>
  )
}
