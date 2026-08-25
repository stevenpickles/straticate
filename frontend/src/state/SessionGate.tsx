/**
 * Rehydration of the workflow after a page reload, and the gate that hides
 * the workspace while it happens.
 *
 * The stored snapshot is identifiers only (see `persistence.ts`), so
 * restoring means *fetching*: `GET /jobs/{id}` and `GET /audio/{id}` are
 * the source of truth, and the WebSocket then keeps the restored job
 * current exactly as it does a job the user just started.
 *
 * Everything here is best-effort. Job and audio records are held in memory
 * by the backend, so a stored id is routinely stale — after a backend
 * restart it always is. Nothing that fails to restore produces an error
 * message: an id the backend has never heard of is not something the user
 * can act on, so the workflow simply starts where the evidence supports.
 */
import {
  useCallback,
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
} from 'react'
import { getAudio } from '../api/audio'
import { getJob } from '../api/jobs'
import type { AudioFile, Job } from '../api/types'
import {
  useAppDispatch,
  useAppState,
  type AppAction,
  type WorkflowPhase,
} from './appState'
import { isTerminalJobState, useJobDispatch, useJobState } from './jobState'
import {
  clearSessionSnapshot,
  isEmptySessionSnapshot,
  readSessionSnapshot,
  writeSessionSnapshot,
  type SessionSnapshot,
} from './persistence'

/**
 * How long rehydration may take before the app gives up and starts clean.
 *
 * `fetch` rejects promptly when the backend refuses a connection, but a
 * server that accepts and never answers would otherwise leave the user
 * looking at "Restoring your session" forever. This is the ceiling on that:
 * whatever has not arrived by now is abandoned, its late answer ignored.
 */
export const RESTORE_TIMEOUT_MS = 10_000

/** Where rehydration has got to. */
export type RestoreStatus = 'restoring' | 'settled'

/** Resolve `promise`, or `null` for any failure (404, network, malformed). */
async function orNull<T>(promise: Promise<T>): Promise<T | null> {
  try {
    return await promise
  } catch {
    return null
  }
}

/**
 * The phase to restore, given what the backend actually still knows.
 *
 * The stored phase is a *preference*, never an instruction: the records
 * decide what is possible and the stored phase only disambiguates the
 * places a user can be with the same completed job. That ordering is what
 * makes a stale snapshot harmless — it can never land the user on a phase
 * whose data is gone, which is the dead end feature 030 made the suite
 * assert against.
 *
 * - No job: `configure` when the upload survived, `select` when it did not.
 * - A job still running: `separate`, whatever was stored. There is nothing
 *   to inspect until it finishes.
 * - A completed job: `inspect` if that is where the user was (`export` maps
 *   there too — the export panel is rendered inside `inspect`), otherwise
 *   `separate`, which shows the result summary and the way onward.
 * - A cancelled or failed job: `separate`, which is the phase that renders
 *   a terminal job and offers the route out of it.
 */
export function restoredPhase(
  storedPhase: WorkflowPhase | null,
  job: Job | null,
  file: AudioFile | null,
): WorkflowPhase {
  if (job === null) {
    return file === null ? 'select' : 'configure'
  }
  if (!isTerminalJobState(job.state)) {
    return 'separate'
  }
  if (
    job.state === 'completed' &&
    (storedPhase === 'inspect' || storedPhase === 'export')
  ) {
    return 'inspect'
  }
  return 'separate'
}

/**
 * Fetch the records the snapshot points at and dispatch the restored
 * workflow. Returns without dispatching anything if `isCancelled()` goes
 * true — the restore was abandoned (timed out) or the component unmounted.
 */
async function applyRestore(
  snapshot: SessionSnapshot,
  isCancelled: () => boolean,
  appDispatch: Dispatch<AppAction>,
  trackJob: (job: Job) => void,
): Promise<void> {
  const job =
    snapshot.jobId === null ? null : await orNull(getJob(snapshot.jobId))
  // A job knows its own input, so its record is preferred over the stored
  // audio id: the two can only disagree if the snapshot was written by an
  // older build, and the job is the authoritative one.
  const audioId = job?.audio_id ?? snapshot.audioId
  const file = audioId === null ? null : await orNull(getAudio(audioId))

  if (isCancelled()) {
    return
  }
  // Order matters: `upload/succeeded` moves `select` to `configure` on its
  // own, so the explicit `phase/set` goes last and has the final say.
  if (file !== null) {
    appDispatch({ type: 'upload/succeeded', file })
  }
  if (job !== null) {
    trackJob(job)
  }
  appDispatch({
    type: 'phase/set',
    phase: restoredPhase(snapshot.phase, job, file),
  })
}

/**
 * Restore the workflow from the stored snapshot on mount, then keep the
 * snapshot current for the next reload.
 *
 * With nothing stored the hook settles synchronously on its first render
 * and issues no requests, so the ordinary first visit is exactly what it
 * was before this feature.
 *
 * @returns whether rehydration is still in flight.
 */
export function useSessionRestore(): RestoreStatus {
  // Read once, before any effect can write: the snapshot describes the
  // session that ended, and the app is about to start overwriting it.
  const [snapshot] = useState(readSessionSnapshot)
  const [status, setStatus] = useState<RestoreStatus>(() =>
    isEmptySessionSnapshot(snapshot) ? 'settled' : 'restoring',
  )

  const appDispatch = useAppDispatch()
  const jobDispatch = useJobDispatch()
  const { phase, upload } = useAppState()
  const { job } = useJobState()

  const trackJob = useCallback(
    (restored: Job) => {
      // `job/track` carries the terminal-state guard of feature 031: a
      // rehydrated record is a REST snapshot like any other, and the store
      // decides whether it may be applied. Nothing is tracked yet at this
      // point, so there is nothing to rewind — but the guard is inherited
      // rather than bypassed, which is the whole reason it lives in the
      // reducer.
      jobDispatch({ type: 'job/track', job: restored })
    },
    [jobDispatch],
  )

  useEffect(() => {
    if (isEmptySessionSnapshot(snapshot)) {
      return
    }
    let cancelled = false
    const abandon = setTimeout(() => {
      cancelled = true
      clearSessionSnapshot()
      setStatus('settled')
    }, RESTORE_TIMEOUT_MS)

    void applyRestore(snapshot, () => cancelled, appDispatch, trackJob).then(
      () => {
        if (cancelled) {
          return
        }
        clearTimeout(abandon)
        setStatus('settled')
      },
    )

    return () => {
      cancelled = true
      clearTimeout(abandon)
    }
  }, [snapshot, appDispatch, trackJob])

  // Persist only once rehydration is done, so the restore reads the session
  // that ended rather than the empty one the app started with.
  useEffect(() => {
    if (status !== 'settled') {
      return
    }
    writeSessionSnapshot({
      jobId: job?.id ?? null,
      audioId: upload.status === 'uploaded' ? upload.file.id : null,
      phase,
    })
    // Only the tracked job's *id* is persisted; depending on the whole `job`
    // would rewrite the snapshot on every progress event for no gain.
  }, [status, job?.id, upload, phase])

  return status
}

/** Props for {@link SessionGate}. */
export interface SessionGateProps {
  /** The workspace, rendered once rehydration has settled. */
  children: ReactNode
}

/**
 * Runs {@link useSessionRestore} and holds the workspace back until it
 * settles.
 *
 * Without the gate a reload would paint the `select` phase and then jump to
 * the restored one, which is not merely ugly: the drop zone would be live
 * for that moment, and a file dropped into it would be overwritten by the
 * restore landing a beat later.
 *
 * Must be rendered under both `AppStateProvider` and `JobStateProvider`.
 */
export function SessionGate({ children }: SessionGateProps) {
  const status = useSessionRestore()
  if (status === 'restoring') {
    return (
      <main className="workspace" aria-busy="true">
        <p className="workspace-hint" role="status">
          Restoring your session…
        </p>
      </main>
    )
  }
  return <>{children}</>
}
