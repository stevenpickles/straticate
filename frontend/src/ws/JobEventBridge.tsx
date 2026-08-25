/**
 * Opens the job event socket for the whole session and keeps the job store
 * honest across reconnects.
 *
 * The socket is a session-level resource, not a phase-level one: mounting
 * it beside the workspace (rather than inside the component that happens to
 * render the running job) means events are never missed because the UI was
 * showing something else, and reconnect backoff is not restarted by a
 * re-render of the workflow.
 */
import { useCallback, useEffect, useRef } from 'react'
import { getJob } from '../api/jobs'
import { useJobDispatch, useJobState } from '../state/jobState'
import type { JobEventClient } from './client'
import { useJobEvents } from './useJobEvents'

/** Props for {@link JobEventBridge}. */
export interface JobEventBridgeProps {
  /**
   * Client to drive. Defaults to one owned by the hook; pass a scriptable
   * double in tests.
   */
  readonly client?: JobEventClient
}

/**
 * Renderless component that connects the job event socket and resyncs the
 * tracked job over REST whenever the socket (re)opens **and** whenever a
 * different job becomes the tracked one while the socket is already open.
 *
 * REST is the source of truth on connect: the hub never replays missed
 * events, and a client that falls behind is disconnected with `1013` and
 * expected to resync (`docs/contracts/websocket-events.md`). The invariant
 * this component maintains is therefore: *while a job is tracked and the
 * socket is open, that job's record has been fetched at least once since
 * the socket opened.* Events are then applied on top of that authoritative
 * record.
 *
 * The second half of the invariant is what feature 033 needed. A job
 * restored after a page reload is tracked some time after the socket
 * opened, so the open-time resync ran with nothing to fetch. Without a
 * fetch on the change, a job that reached a terminal state in the window
 * between the backend serving the restore's `GET /jobs/{id}` and the store
 * applying it would have had its terminal event dropped (events are
 * filtered by the tracked job id, and nothing was tracked yet) with no
 * further event ever coming — the stranded-UI failure of features 017 and
 * 031, reached by a different road. The extra fetch closes that window; it
 * is also harmless for a job this client just created, whose `POST /jobs`
 * record is by definition current.
 *
 * Does nothing when no job is tracked, which is the whole `select` and
 * `configure` part of the workflow.
 *
 * Must be rendered under a `JobStateProvider`.
 */
export function JobEventBridge({ client }: JobEventBridgeProps = {}) {
  const { job, connection } = useJobState()
  const dispatch = useJobDispatch()
  const jobId = job?.id ?? null

  // The socket subscription must not be torn down and rebuilt every time
  // the tracked job changes, so the resync reads the current job id from a
  // ref rather than closing over it.
  // Declared before the resync effect so it is already up to date when that
  // one runs (effects fire in declaration order).
  const trackedJobIdRef = useRef<string | null>(null)
  useEffect(() => {
    trackedJobIdRef.current = jobId
  }, [jobId])

  const resync = useCallback(
    (id: string) => {
      getJob(id)
        .then((fetched) => {
          // A different job may have been started, or restored, while the
          // request was in flight; re-tracking the old one would rewind the
          // store. The record's own id is what is compared, so an answer
          // that is not about the tracked job cannot be applied whatever it
          // was requested for.
          if (trackedJobIdRef.current === fetched.id) {
            dispatch({ type: 'job/track', job: fetched })
          }
        })
        .catch(() => {
          // A failed resync is not worth a UI of its own: the connection
          // status is already rendered, events keep arriving, and the next
          // reconnect tries again.
        })
    },
    [dispatch],
  )

  // Driven by the store's connection status rather than by a socket
  // callback, so that "the socket opened" and "the tracked job changed" are
  // the same trigger: either dependency changing re-establishes the
  // invariant above. A `job` object that changed without changing id (every
  // progress event) is not a change here, so events do not cause fetches.
  useEffect(() => {
    if (connection !== 'open' || jobId === null) {
      return
    }
    resync(jobId)
  }, [connection, jobId, resync])

  useJobEvents({ client })

  return null
}
