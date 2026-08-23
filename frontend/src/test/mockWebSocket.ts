/**
 * Scriptable `WebSocket` and timer doubles for the job event client tests.
 *
 * Tests drive the socket explicitly (`emitOpen`, `emitMessage`,
 * `emitClose`) and fire reconnect timers by hand, so no test depends on
 * real network I/O or real elapsed time.
 */
import type { ScheduleFn, WebSocketLike } from '../ws/client'

/** A `WebSocketLike` whose lifecycle events are driven by the test. */
export class FakeWebSocket implements WebSocketLike {
  /** URL the client asked for. */
  readonly url: string
  /** True once {@link close} has been called. */
  closed = false
  /** Number of times {@link close} has been called. */
  closeCount = 0

  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null

  constructor(url: string) {
    this.url = url
  }

  close(): void {
    this.closed = true
    this.closeCount += 1
  }

  /** Fire the socket's `open` event. */
  emitOpen(): void {
    this.onopen?.(new Event('open'))
  }

  /** Fire a `message` event carrying `data` (already-serialized JSON text). */
  emitMessage(data: string): void {
    this.onmessage?.(new MessageEvent('message', { data }))
  }

  /** Fire an `error` event. */
  emitError(): void {
    this.onerror?.(new Event('error'))
  }

  /** Fire a `close` event; defaults to the abnormal-closure code. */
  emitClose(code = 1006): void {
    this.onclose?.(new CloseEvent('close', { code, wasClean: code === 1000 }))
  }
}

/** Records every socket a {@link JobEventClient} creates. */
export class FakeWebSocketFactory {
  /** Sockets created so far, oldest first. */
  readonly sockets: FakeWebSocket[] = []

  /** Pass as the `createWebSocket` option. */
  readonly create = (url: string): FakeWebSocket => {
    const socket = new FakeWebSocket(url)
    this.sockets.push(socket)
    return socket
  }

  /** The most recently created socket. */
  get last(): FakeWebSocket {
    const socket = this.sockets.at(-1)
    if (socket === undefined) {
      throw new Error('No FakeWebSocket has been created yet')
    }
    return socket
  }
}

/** One pending callback registered with {@link FakeScheduler.schedule}. */
export interface ScheduledTask {
  readonly delayMs: number
  readonly callback: () => void
  cancelled: boolean
}

/** Deterministic stand-in for `setTimeout`, driven by the test. */
export class FakeScheduler {
  /** Every task scheduled so far, including cancelled ones. */
  readonly tasks: ScheduledTask[] = []

  private readonly ran = new Set<ScheduledTask>()

  /** Pass as the `schedule` option. */
  readonly schedule: ScheduleFn = (callback, delayMs) => {
    const task: ScheduledTask = { callback, delayMs, cancelled: false }
    this.tasks.push(task)
    return () => {
      task.cancelled = true
    }
  }

  /** Delays of the tasks scheduled so far, in order. */
  get delays(): number[] {
    return this.tasks.map((task) => task.delayMs)
  }

  /** The newest task that has not been cancelled or run. */
  get pending(): ScheduledTask[] {
    return this.tasks.filter((task) => !task.cancelled && !this.ran.has(task))
  }

  /** Run the oldest pending task, as a real timer eventually would. */
  runNext(): void {
    const task = this.pending[0]
    if (task === undefined) {
      throw new Error('No pending scheduled task to run')
    }
    this.ran.add(task)
    task.callback()
  }
}
