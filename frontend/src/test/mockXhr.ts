/**
 * Minimal `XMLHttpRequest` test double for upload tests. Records the
 * request and lets tests drive upload progress and the final response.
 */
import { vi } from 'vitest'

type Listener = (event: ProgressEvent) => void

/** Scriptable stand-in for the browser `XMLHttpRequest`. */
export class MockXMLHttpRequest {
  /** Every instance constructed since the last {@link installMockXhr}. */
  static instances: MockXMLHttpRequest[] = []

  status = 0
  responseText = ''
  responseType = ''
  method: string | null = null
  url: string | null = null
  sentBody: unknown = null
  aborted = false

  private listeners = new Map<string, Listener[]>()
  private uploadListeners = new Map<string, Listener[]>()

  readonly upload = {
    addEventListener: (name: string, listener: Listener) => {
      const existing = this.uploadListeners.get(name) ?? []
      this.uploadListeners.set(name, [...existing, listener])
    },
  }

  constructor() {
    MockXMLHttpRequest.instances.push(this)
  }

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  send(body?: unknown): void {
    this.sentBody = body ?? null
  }

  abort(): void {
    this.aborted = true
    this.dispatch('abort')
  }

  addEventListener(name: string, listener: Listener): void {
    const existing = this.listeners.get(name) ?? []
    this.listeners.set(name, [...existing, listener])
  }

  /** Fire an upload `progress` event. */
  emitUploadProgress(
    loaded: number,
    total: number,
    lengthComputable = true,
  ): void {
    const event = { loaded, total, lengthComputable } as ProgressEvent
    for (const listener of this.uploadListeners.get('progress') ?? []) {
      listener(event)
    }
  }

  /** Complete the request with a status and raw response body. */
  respond(status: number, body: string): void {
    this.status = status
    this.responseText = body
    this.dispatch('load')
  }

  /** Fail the request at the network level. */
  failNetwork(): void {
    this.dispatch('error')
  }

  private dispatch(name: string): void {
    const event = {} as ProgressEvent
    for (const listener of this.listeners.get(name) ?? []) {
      listener(event)
    }
  }
}

/**
 * Replace the global `XMLHttpRequest` with {@link MockXMLHttpRequest} and
 * reset the recorded instances. Pair with `vi.unstubAllGlobals()` in
 * `afterEach`.
 */
export function installMockXhr(): void {
  MockXMLHttpRequest.instances = []
  vi.stubGlobal('XMLHttpRequest', MockXMLHttpRequest)
}

/** The most recently constructed mock request, or throw when none exists. */
export function lastXhr(): MockXMLHttpRequest {
  const instance = MockXMLHttpRequest.instances.at(-1)
  if (instance === undefined) {
    throw new Error('No XMLHttpRequest was constructed')
  }
  return instance
}
