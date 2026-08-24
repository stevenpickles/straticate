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
 * tracked job over REST whenever the socket (re)opens.
 *
 * REST is the source of truth on connect: the hub never replays missed
 * events, and a client that falls behind is disconnected with `1013` and
 * expected to resync (`docs/contracts/websocket-events.md`). So on every
 * `open` — the first one included — the tracked job is refetched and
 * re-tracked, and events are applied on top of that authoritative record.
 *
 * Does nothing when no job is tracked, which is the whole `select` and
 * `configure` part of the workflow.
 *
 * Must be rendered under a `JobStateProvider`.
 */
export function JobEventBridge({ client }: JobEventBridgeProps = {}) {
  const { job } = useJobState()
  const dispatch = useJobDispatch()

  // The socket subscription must not be torn down and rebuilt every time
  // the tracked job changes, so `onOpen` reads the current job id from a
  // ref rather than closing over it.
  const trackedJobIdRef = useRef<string | null>(null)
  useEffect(() => {
    trackedJobIdRef.current = job?.id ?? null
  }, [job])

  const resync = useCallback(() => {
    const jobId = trackedJobIdRef.current
    if (jobId === null) {
      return
    }
    getJob(jobId)
      .then((fetched) => {
        // A different job may have been started while the request was in
        // flight; re-tracking the old one would rewind the store.
        if (trackedJobIdRef.current === jobId) {
          dispatch({ type: 'job/track', job: fetched })
        }
      })
      .catch(() => {
        // A failed resync is not worth a UI of its own: the connection
        // status is already rendered, events keep arriving, and the next
        // reconnect tries again.
      })
  }, [dispatch])

  useJobEvents({ client, onOpen: resync })

  return null
}
