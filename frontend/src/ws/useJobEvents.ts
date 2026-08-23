/**
 * React integration for the job event socket: connects a
 * {@link JobEventClient} for the lifetime of the calling component and
 * feeds every decoded event into the job store.
 */
import { useEffect, useRef, useState } from 'react'
import { useJobDispatch } from '../state/jobState'
import { JobEventClient } from './client'

/** Options for {@link useJobEvents}. */
export interface UseJobEventsOptions {
  /**
   * Client to drive. Defaults to a client owned by the hook, created once
   * per component instance. Pass one to share a socket or to inject a
   * test double; the hook connects it on mount and closes it on unmount
   * either way, so a shared client must outlive only one consumer.
   */
  readonly client?: JobEventClient
  /**
   * Called whenever the socket reaches `open` — on first connect and after
   * every reconnect. Use it to refetch job state over REST, which stays the
   * source of truth (events are notifications, not the database). The
   * latest callback is always used; it does not need to be stable.
   */
  readonly onOpen?: () => void
}

/**
 * Subscribe to job events for the lifetime of the calling component.
 *
 * On mount the hook subscribes to events and status changes and connects
 * the socket; on unmount it unsubscribes and closes it (an intentional
 * close, so no reconnect is attempted). Events are dispatched to the store
 * as `ws/event` actions and status changes as `ws/status`, so the component
 * must be rendered under a `JobStateProvider`.
 *
 * @returns the client being driven, for status reads or manual control.
 */
export function useJobEvents(
  options: UseJobEventsOptions = {},
): JobEventClient {
  const { client: providedClient, onOpen } = options
  const dispatch = useJobDispatch()
  const [ownedClient] = useState(() => new JobEventClient())
  const client = providedClient ?? ownedClient

  const onOpenRef = useRef(onOpen)
  useEffect(() => {
    onOpenRef.current = onOpen
  }, [onOpen])

  useEffect(() => {
    const unsubscribeEvents = client.subscribe((event) => {
      dispatch({ type: 'ws/event', event })
    })
    const unsubscribeStatus = client.subscribeStatus((status) => {
      dispatch({ type: 'ws/status', status })
      if (status === 'open') {
        onOpenRef.current?.()
      }
    })
    client.connect()
    return () => {
      unsubscribeEvents()
      unsubscribeStatus()
      client.close()
    }
  }, [client, dispatch])

  return client
}
