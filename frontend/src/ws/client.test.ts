import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  JobEventClient,
  KNOWN_EVENT_TYPES,
  WS_PATH,
  decodeEvent,
  defaultWebSocketUrl,
  type ConnectionStatus,
  type JobEventClientOptions,
} from './client'
import type { WebSocketEvent } from '../api/types'
import { FakeScheduler, FakeWebSocketFactory } from '../test/mockWebSocket'

/**
 * Event payloads copied from `docs/contracts/websocket-events.md`. The
 * `job_created`/`job_completed` samples replace the document's `"…"`
 * placeholders with concrete `Job`/`SeparationResult` objects.
 */
const contractEvents: Record<WebSocketEvent['type'], string> = {
  job_created: JSON.stringify({
    type: 'job_created',
    job_id: '01JOB...',
    job: {
      id: '01JOB...',
      audio_id: '01ABC...',
      configuration: {
        audio_id: '01ABC...',
        mode_id: 'vocals',
        quality_id: 'high_quality',
        device_id: 'cuda:0',
      },
      model_id: 'vocals-hq-001',
      state: 'queued',
      progress: 0,
      created_at: '2026-08-23T12:00:00Z',
      started_at: null,
      finished_at: null,
      error: null,
      result: null,
    },
  }),
  job_started: `{ "type": "job_started", "job_id": "01JOB...", "started_at": "2026-08-23T12:00:05Z" }`,
  job_stage_changed: `{ "type": "job_stage_changed", "job_id": "01JOB...", "stage": "separating", "previous_stage": "loading_model" }`,
  job_progress: `{
    "type": "job_progress",
    "job_id": "01JOB...",
    "stage": "separating",
    "progress": 0.65,
    "chunks_completed": 31,
    "chunks_total": 48,
    "elapsed_seconds": 18.2,
    "audio_processed_seconds": 148.0,
    "audio_total_seconds": 227.4
  }`,
  runtime_metrics: `{
    "type": "runtime_metrics",
    "job_id": "01JOB...",
    "model": {
      "id": "vocals-hq-001",
      "display_name": "Vocals — High Quality",
      "architecture": "mel_band_roformer",
      "version": "1.0",
      "separation_mode": "vocals",
      "stem_count": 2
    },
    "gpu": {
      "device_id": "cuda:0",
      "name": "NVIDIA GeForce RTX 5090",
      "backend": "cuda",
      "memory_allocated_bytes": 9234179686,
      "memory_peak_bytes": 10133099161,
      "memory_total_bytes": 34359738368,
      "utilization": 0.91,
      "temperature_celsius": 63
    },
    "processing": {
      "stage": "separating",
      "chunks_completed": 31,
      "chunks_total": 48,
      "elapsed_seconds": 18.2,
      "audio_processed_seconds": 148.0,
      "realtime_factor": 7.9
    }
  }`,
  job_completed: JSON.stringify({
    type: 'job_completed',
    job_id: '01JOB...',
    result: {
      job_id: '01JOB...',
      model_id: 'vocals-hq-001',
      stems: [
        {
          name: 'vocals',
          duration_seconds: 227.4,
          sample_rate_hz: 44100,
          channels: 2,
        },
      ],
      metrics: { processing_seconds: 28.8, realtime_factor: 7.9 },
    },
  }),
  job_cancelled: `{ "type": "job_cancelled", "job_id": "01JOB...", "stage_at_cancellation": "separating" }`,
  job_failed: `{
    "type": "job_failed",
    "job_id": "01JOB...",
    "error": { "code": "cuda_out_of_memory", "message": "…", "detail": {} }
  }`,
}

const silentLogger = { warn: vi.fn() }

let sockets: FakeWebSocketFactory
let scheduler: FakeScheduler

function makeClient(overrides: JobEventClientOptions = {}): JobEventClient {
  return new JobEventClient({
    createWebSocket: sockets.create,
    schedule: scheduler.schedule,
    random: () => 0,
    logger: silentLogger,
    ...overrides,
  })
}

beforeEach(() => {
  sockets = new FakeWebSocketFactory()
  scheduler = new FakeScheduler()
  silentLogger.warn.mockClear()
})

describe('defaultWebSocketUrl', () => {
  it('derives ws:// from an http page on the same origin', () => {
    expect(
      defaultWebSocketUrl({ protocol: 'http:', host: 'localhost:5173' }),
    ).toBe(`ws://localhost:5173${WS_PATH}`)
  })

  it('derives wss:// from an https page', () => {
    expect(
      defaultWebSocketUrl({ protocol: 'https:', host: 'straticate.local' }),
    ).toBe(`wss://straticate.local${WS_PATH}`)
  })
})

describe('JobEventClient connection', () => {
  it('connects to the same-origin /api/v1/ws endpoint by default', () => {
    makeClient().connect()

    expect(sockets.sockets).toHaveLength(1)
    expect(sockets.last.url).toBe(`ws://${globalThis.location.host}${WS_PATH}`)
  })

  it('uses an explicitly configured url', () => {
    makeClient({ url: 'ws://example.test/socket' }).connect()

    expect(sockets.last.url).toBe('ws://example.test/socket')
  })

  it('is a no-op when connect() is called while already connected', () => {
    const client = makeClient()
    client.connect()
    client.connect()

    expect(sockets.sockets).toHaveLength(1)
  })

  it('closes the socket on close()', () => {
    const client = makeClient()
    client.connect()
    sockets.last.emitOpen()
    client.close()

    expect(sockets.last.closed).toBe(true)
    expect(client.status).toBe('closed')
  })
})

describe('JobEventClient event decoding', () => {
  it.each([...KNOWN_EVENT_TYPES])('decodes the documented %s event', (type) => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe((event) => received.push(event))
    client.connect()
    sockets.last.emitOpen()

    sockets.last.emitMessage(contractEvents[type])

    expect(received).toHaveLength(1)
    expect(received[0]).toEqual(JSON.parse(contractEvents[type]))
    expect(received[0]?.type).toBe(type)
    expect(received[0]?.job_id).toBe('01JOB...')
  })

  it('exposes typed fields of a decoded job_progress event', () => {
    const client = makeClient()
    let received: WebSocketEvent | null = null
    client.subscribe((event) => {
      received = event
    })
    client.connect()
    sockets.last.emitMessage(contractEvents.job_progress)

    const event = received as WebSocketEvent | null
    expect(event?.type).toBe('job_progress')
    if (event?.type !== 'job_progress') {
      throw new Error('expected a job_progress event')
    }
    expect(event.chunks_completed).toBe(31)
    expect(event.chunks_total).toBe(48)
    expect(event.progress).toBeCloseTo(0.65)
  })

  it('ignores an unknown event type without throwing', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe((event) => received.push(event))
    client.connect()

    expect(() => {
      sockets.last.emitMessage(
        '{"type": "job_teleported", "job_id": "01JOB...", "destination": "mars"}',
      )
    }).not.toThrow()
    expect(received).toEqual([])
  })

  it('ignores malformed JSON without throwing or closing', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe((event) => received.push(event))
    client.connect()

    expect(() => {
      sockets.last.emitMessage('{ not json at all')
    }).not.toThrow()
    expect(received).toEqual([])
    expect(silentLogger.warn).toHaveBeenCalled()

    // The socket stays usable afterwards.
    sockets.last.emitMessage(contractEvents.job_started)
    expect(received).toHaveLength(1)
  })

  it('ignores payloads that are not objects or carry no type', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe((event) => received.push(event))
    client.connect()

    sockets.last.emitMessage('42')
    sockets.last.emitMessage('null')
    sockets.last.emitMessage('"job_progress"')
    sockets.last.emitMessage('{"job_id": "01JOB..."}')
    sockets.last.emitMessage('{"type": 7}')

    expect(received).toEqual([])
  })

  it('stops delivering events after unsubscribe', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    const unsubscribe = client.subscribe((event) => received.push(event))
    client.connect()

    sockets.last.emitMessage(contractEvents.job_started)
    unsubscribe()
    sockets.last.emitMessage(contractEvents.job_started)

    expect(received).toHaveLength(1)
  })

  it('keeps delivering to other handlers when one throws', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe(() => {
      throw new Error('handler blew up')
    })
    client.subscribe((event) => received.push(event))
    client.connect()

    expect(() => {
      sockets.last.emitMessage(contractEvents.job_started)
    }).not.toThrow()
    expect(received).toHaveLength(1)
  })
})

describe('decodeEvent', () => {
  it('returns null for non-string data', () => {
    expect(decodeEvent(new ArrayBuffer(4))).toBeNull()
  })

  it('returns the parsed event for a known type', () => {
    expect(decodeEvent(contractEvents.job_cancelled)).toEqual({
      type: 'job_cancelled',
      job_id: '01JOB...',
      stage_at_cancellation: 'separating',
    })
  })
})

describe('JobEventClient reconnection', () => {
  it('reconnects with exponential backoff after an unexpected close', () => {
    const client = makeClient()
    client.connect()
    sockets.last.emitOpen()

    sockets.last.emitClose()
    expect(scheduler.delays).toEqual([500])

    scheduler.runNext()
    expect(sockets.sockets).toHaveLength(2)

    // Still failing: the delay doubles each attempt.
    sockets.last.emitClose()
    scheduler.runNext()
    sockets.last.emitClose()
    scheduler.runNext()
    expect(scheduler.delays).toEqual([500, 1000, 2000])
    expect(sockets.sockets).toHaveLength(4)
  })

  it('caps the backoff delay', () => {
    const client = makeClient()
    client.connect()
    for (let i = 0; i < 7; i += 1) {
      sockets.last.emitClose()
      scheduler.runNext()
    }

    expect(scheduler.delays).toEqual([500, 1000, 2000, 4000, 8000, 8000, 8000])
  })

  it('resets the backoff after a successful reconnect', () => {
    const client = makeClient()
    client.connect()
    sockets.last.emitClose()
    scheduler.runNext()
    sockets.last.emitClose()
    scheduler.runNext()
    sockets.last.emitOpen()
    sockets.last.emitClose()

    expect(scheduler.delays).toEqual([500, 1000, 500])
  })

  it('subtracts jitter of up to the configured ratio', () => {
    const client = makeClient({ random: () => 1 })
    client.connect()
    sockets.last.emitClose()

    // 500 ms minus the full 25 % jitter window.
    expect(scheduler.delays).toEqual([375])
  })

  it('does not reconnect after close()', () => {
    const client = makeClient()
    client.connect()
    sockets.last.emitOpen()
    client.close()

    expect(scheduler.tasks).toEqual([])
    expect(sockets.sockets).toHaveLength(1)
    expect(client.status).toBe('closed')
  })

  it('cancels a pending reconnect when close() is called', () => {
    const client = makeClient()
    client.connect()
    sockets.last.emitClose()
    expect(scheduler.pending).toHaveLength(1)

    client.close()
    expect(scheduler.pending).toHaveLength(0)
    expect(sockets.sockets).toHaveLength(1)
  })

  it('can be reconnected after an intentional close', () => {
    const client = makeClient()
    client.connect()
    client.close()
    client.connect()

    expect(sockets.sockets).toHaveLength(2)
    expect(client.status).toBe('connecting')
  })

  it('reconnects when the socket cannot be created at all', () => {
    const client = makeClient({
      createWebSocket: () => {
        throw new Error('connection refused')
      },
    })
    client.connect()

    expect(client.status).toBe('reconnecting')
    expect(scheduler.delays).toEqual([500])
  })

  it('ignores events from a superseded socket', () => {
    const client = makeClient()
    const received: WebSocketEvent[] = []
    client.subscribe((event) => received.push(event))
    client.connect()
    const stale = sockets.last
    stale.emitClose()
    scheduler.runNext()

    stale.emitMessage(contractEvents.job_started)
    expect(received).toEqual([])
  })
})

describe('JobEventClient status', () => {
  it('reports every transition, starting from the current status', () => {
    const client = makeClient()
    const statuses: ConnectionStatus[] = []
    client.subscribeStatus((status) => statuses.push(status))

    client.connect()
    sockets.last.emitOpen()
    sockets.last.emitClose()
    scheduler.runNext()
    sockets.last.emitOpen()
    client.close()

    expect(statuses).toEqual([
      'closed',
      'connecting',
      'open',
      'reconnecting',
      'open',
      'closed',
    ])
  })

  it('does not repeat an unchanged status', () => {
    const client = makeClient()
    const statuses: ConnectionStatus[] = []
    client.subscribeStatus((status) => statuses.push(status))
    client.connect()
    sockets.last.emitOpen()
    sockets.last.emitOpen()

    expect(statuses).toEqual(['closed', 'connecting', 'open'])
  })

  it('stops notifying after unsubscribeStatus', () => {
    const client = makeClient()
    const statuses: ConnectionStatus[] = []
    const unsubscribe = client.subscribeStatus((status) =>
      statuses.push(status),
    )
    unsubscribe()
    client.connect()

    expect(statuses).toEqual(['closed'])
  })

  it('logs but survives a socket error event', () => {
    const client = makeClient()
    client.connect()

    expect(() => {
      sockets.last.emitError()
    }).not.toThrow()
    expect(silentLogger.warn).toHaveBeenCalled()
  })
})
