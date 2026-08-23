/**
 * Resilient WebSocket client for Straticate job events.
 *
 * One socket (`WS /api/v1/ws`) carries every server → client event; the
 * client decodes each message into the generated {@link WebSocketEvent}
 * union and fans it out to subscribers. Components never touch the raw
 * socket — they consume state fed by `src/state/jobState.tsx`.
 *
 * Robustness rules (see `docs/contracts/websocket-events.md`):
 *
 * - **Unknown `type` values are ignored**, never thrown, so a newer backend
 *   emitting additional events cannot break an older frontend.
 * - Malformed JSON is logged and ignored; the socket stays usable.
 * - Unexpected closes reconnect with exponential backoff plus jitter;
 *   {@link JobEventClient.close} is intentional and never reconnects.
 */

import type { WebSocketEvent } from '../api/types'

/** Path of the WebSocket endpoint (proxied by Vite in development). */
export const WS_PATH = '/api/v1/ws'

/** Every event `type` the client accepts; anything else is ignored. */
export const KNOWN_EVENT_TYPES: readonly WebSocketEvent['type'][] = [
  'job_created',
  'job_started',
  'job_stage_changed',
  'job_progress',
  'runtime_metrics',
  'job_completed',
  'job_cancelled',
  'job_failed',
]

const knownEventTypes = new Set<string>(KNOWN_EVENT_TYPES)

/**
 * Connection status of a {@link JobEventClient}, renderable by the UI:
 *
 * - `closed` — no socket, and none wanted (initial state, or after `close()`);
 * - `connecting` — the first connection attempt is in flight;
 * - `open` — connected and receiving events;
 * - `reconnecting` — the socket dropped; a retry is scheduled or in flight.
 */
export type ConnectionStatus = 'closed' | 'connecting' | 'open' | 'reconnecting'

/** Receives every decoded, known-type event. */
export type JobEventHandler = (event: WebSocketEvent) => void

/** Receives every {@link ConnectionStatus} transition. */
export type ConnectionStatusHandler = (status: ConnectionStatus) => void

/** Removes a subscription registered with `subscribe`/`subscribeStatus`. */
export type Unsubscribe = () => void

/**
 * The slice of the browser `WebSocket` API this client uses. A real
 * `WebSocket` satisfies it, so tests can inject a scriptable double.
 */
export interface WebSocketLike {
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  onerror: ((event: Event) => void) | null
  close(code?: number, reason?: string): void
}

/** Creates a socket for `url`; defaults to `globalThis.WebSocket`. */
export type WebSocketFactory = (url: string) => WebSocketLike

/**
 * Runs `callback` after `delayMs`, returning a canceller. Injecting this
 * makes reconnect timing deterministic in tests without real sleeps.
 */
export type ScheduleFn = (callback: () => void, delayMs: number) => Unsubscribe

/** Subset of `console` used for diagnostics; injectable for quiet tests. */
export interface ClientLogger {
  warn: (message: string, ...details: unknown[]) => void
}

/** Options for {@link JobEventClient}; every field has a browser-sensible default. */
export interface JobEventClientOptions {
  /** Full socket URL. Defaults to {@link defaultWebSocketUrl}. */
  readonly url?: string
  /** Socket factory. Defaults to `new WebSocket(url)`. */
  readonly createWebSocket?: WebSocketFactory
  /** Timer used for reconnect backoff. Defaults to `setTimeout`. */
  readonly schedule?: ScheduleFn
  /** Source of jitter in `[0, 1)`. Defaults to `Math.random`. */
  readonly random?: () => number
  /** Delay before the first reconnect attempt, in ms. Default `500`. */
  readonly initialReconnectDelayMs?: number
  /** Upper bound on the backoff delay, in ms. Default `8000`. */
  readonly maxReconnectDelayMs?: number
  /** Fraction of the delay applied as random jitter in `[0, 1]`. Default `0.25`. */
  readonly jitterRatio?: number
  /** Diagnostics sink. Defaults to `console`. */
  readonly logger?: ClientLogger
}

/**
 * Build the WebSocket URL for the current origin, upgrading `https:` to
 * `wss:` so the page's transport security carries over. Using the same
 * origin lets the Vite dev proxy (`ws: true`) forward the socket.
 */
export function defaultWebSocketUrl(
  location: { protocol: string; host: string } = globalThis.location,
): string {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${location.host}${WS_PATH}`
}

function defaultSchedule(callback: () => void, delayMs: number): Unsubscribe {
  const handle = globalThis.setTimeout(callback, delayMs)
  return () => {
    globalThis.clearTimeout(handle)
  }
}

/**
 * Decode one raw WebSocket payload into a {@link WebSocketEvent}.
 *
 * Returns `null` — never throws — when the payload is not JSON, is not an
 * object, has no `type`, or carries an unknown `type` (forward
 * compatibility). Exported for direct unit testing.
 */
export function decodeEvent(data: unknown): WebSocketEvent | null {
  if (typeof data !== 'string') {
    return null
  }
  let payload: unknown
  try {
    payload = JSON.parse(data)
  } catch {
    return null
  }
  if (typeof payload !== 'object' || payload === null || !('type' in payload)) {
    return null
  }
  const { type } = payload as { type: unknown }
  if (typeof type !== 'string' || !knownEventTypes.has(type)) {
    return null
  }
  return payload as WebSocketEvent
}

/**
 * Subscribable, self-healing client for the job event socket.
 *
 * ```ts
 * const client = new JobEventClient()
 * const off = client.subscribe((event) => { console.log(event.type) })
 * client.connect()
 * // …later
 * off()
 * client.close()
 * ```
 *
 * The client is inert until {@link connect} is called, and subscriptions
 * survive reconnects (and even an intentional `close()` followed by a new
 * `connect()`).
 */
export class JobEventClient {
  private readonly configuredUrl: string | undefined
  private readonly createWebSocket: WebSocketFactory
  private readonly schedule: ScheduleFn
  private readonly random: () => number
  private readonly initialReconnectDelayMs: number
  private readonly maxReconnectDelayMs: number
  private readonly jitterRatio: number
  private readonly logger: ClientLogger

  private readonly eventHandlers = new Set<JobEventHandler>()
  private readonly statusHandlers = new Set<ConnectionStatusHandler>()

  private socket: WebSocketLike | null = null
  private cancelReconnect: Unsubscribe | null = null
  private currentStatus: ConnectionStatus = 'closed'
  private attempt = 0
  /** True between `close()` and the next `connect()`: suppresses reconnects. */
  private intentionallyClosed = true

  constructor(options: JobEventClientOptions = {}) {
    this.configuredUrl = options.url
    this.createWebSocket =
      options.createWebSocket ?? ((url: string) => new WebSocket(url))
    this.schedule = options.schedule ?? defaultSchedule
    this.random = options.random ?? Math.random
    this.initialReconnectDelayMs = options.initialReconnectDelayMs ?? 500
    this.maxReconnectDelayMs = options.maxReconnectDelayMs ?? 8_000
    this.jitterRatio = options.jitterRatio ?? 0.25
    this.logger = options.logger ?? console
  }

  /** The current connection status. */
  get status(): ConnectionStatus {
    return this.currentStatus
  }

  /**
   * The URL the next connection uses. Resolved lazily so constructing a
   * client never touches `location` (and never opens a socket).
   */
  get url(): string {
    return this.configuredUrl ?? defaultWebSocketUrl()
  }

  /**
   * Register an event handler and return its unsubscribe function.
   * Handlers are called in registration order; a handler that throws is
   * logged and does not prevent the others from running.
   */
  subscribe(handler: JobEventHandler): Unsubscribe {
    this.eventHandlers.add(handler)
    return () => {
      this.unsubscribe(handler)
    }
  }

  /** Remove a handler registered with {@link subscribe}. */
  unsubscribe(handler: JobEventHandler): void {
    this.eventHandlers.delete(handler)
  }

  /**
   * Register a status handler and return its unsubscribe function. The
   * handler is invoked immediately with the current status so a freshly
   * mounted UI renders the truth without waiting for a transition.
   */
  subscribeStatus(handler: ConnectionStatusHandler): Unsubscribe {
    this.statusHandlers.add(handler)
    handler(this.currentStatus)
    return () => {
      this.unsubscribeStatus(handler)
    }
  }

  /** Remove a handler registered with {@link subscribeStatus}. */
  unsubscribeStatus(handler: ConnectionStatusHandler): void {
    this.statusHandlers.delete(handler)
  }

  /**
   * Open the socket. Safe to call repeatedly: a no-op while a socket is
   * already connecting or open. Calling it after {@link close} restarts the
   * client with a fresh backoff sequence.
   */
  connect(): void {
    this.intentionallyClosed = false
    if (this.socket !== null) {
      return
    }
    // A reconnect may already be pending; connecting now supersedes it.
    this.cancelPendingReconnect()
    this.attempt = 0
    this.open('connecting')
  }

  /**
   * Close the socket intentionally: cancels any pending reconnect, stops
   * further reconnection attempts, and settles the status at `closed`.
   * Subscriptions are kept, so {@link connect} can resume later.
   */
  close(): void {
    this.intentionallyClosed = true
    this.cancelPendingReconnect()
    this.attempt = 0
    const socket = this.socket
    this.socket = null
    if (socket !== null) {
      this.detach(socket)
      socket.close()
    }
    this.setStatus('closed')
  }

  private open(status: ConnectionStatus): void {
    this.setStatus(status)
    let socket: WebSocketLike
    try {
      socket = this.createWebSocket(this.url)
    } catch (error) {
      this.logger.warn('[ws] failed to open the job event socket', error)
      this.socket = null
      this.scheduleReconnect()
      return
    }
    this.socket = socket
    socket.onopen = () => {
      if (this.socket !== socket) {
        return
      }
      this.attempt = 0
      this.setStatus('open')
    }
    socket.onmessage = (event: MessageEvent) => {
      if (this.socket !== socket) {
        return
      }
      this.handleMessage(event.data)
    }
    socket.onerror = () => {
      // Browsers follow `error` with `close`; reconnection is handled there.
      this.logger.warn('[ws] job event socket error')
    }
    socket.onclose = () => {
      if (this.socket !== socket) {
        return
      }
      this.detach(socket)
      this.socket = null
      if (this.intentionallyClosed) {
        this.setStatus('closed')
        return
      }
      this.scheduleReconnect()
    }
  }

  private detach(socket: WebSocketLike): void {
    socket.onopen = null
    socket.onmessage = null
    socket.onclose = null
    socket.onerror = null
  }

  private handleMessage(data: unknown): void {
    const event = decodeEvent(data)
    if (event === null) {
      // Malformed or unknown payloads are ignored by contract.
      this.logger.warn('[ws] ignoring undecodable job event payload', data)
      return
    }
    for (const handler of [...this.eventHandlers]) {
      try {
        handler(event)
      } catch (error) {
        this.logger.warn('[ws] job event handler threw', error)
      }
    }
  }

  private scheduleReconnect(): void {
    this.cancelPendingReconnect()
    const delay = this.reconnectDelay(this.attempt)
    this.attempt += 1
    this.setStatus('reconnecting')
    this.cancelReconnect = this.schedule(() => {
      this.cancelReconnect = null
      if (this.intentionallyClosed) {
        return
      }
      this.open('reconnecting')
    }, delay)
  }

  /**
   * Exponential backoff with full-width jitter: attempt `n` waits
   * `min(initial * 2^n, max)` milliseconds, reduced by up to
   * `jitterRatio` of that delay so many clients do not retry in lockstep.
   */
  private reconnectDelay(attempt: number): number {
    const exponential = this.initialReconnectDelayMs * 2 ** attempt
    const capped = Math.min(exponential, this.maxReconnectDelayMs)
    const jitter = capped * this.jitterRatio * this.random()
    return Math.round(capped - jitter)
  }

  private cancelPendingReconnect(): void {
    if (this.cancelReconnect !== null) {
      this.cancelReconnect()
      this.cancelReconnect = null
    }
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.currentStatus === status) {
      return
    }
    this.currentStatus = status
    for (const handler of [...this.statusHandlers]) {
      try {
        handler(status)
      } catch (error) {
        this.logger.warn('[ws] connection status handler threw', error)
      }
    }
  }
}
