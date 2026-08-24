import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { JobEventBridge } from './JobEventBridge'
import { JobEventClient } from './client'
import {
  JobStateProvider,
  initialJobState,
  useJobState,
  type JobStateValue,
} from '../state/jobState'
import { FakeScheduler, FakeWebSocketFactory } from '../test/mockWebSocket'
import { sampleJob, sampleJobId } from '../test/fixtures'

const silentLogger = { warn: vi.fn() }

let sockets: FakeWebSocketFactory
let scheduler: FakeScheduler
let client: JobEventClient

beforeEach(() => {
  sockets = new FakeWebSocketFactory()
  scheduler = new FakeScheduler()
  silentLogger.warn.mockClear()
  client = new JobEventClient({
    createWebSocket: sockets.create,
    schedule: scheduler.schedule,
    random: () => 0,
    logger: silentLogger,
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Surfaces the tracked job and connection status for assertions. */
function Probe() {
  const { job, connection } = useJobState()
  return (
    <>
      <p data-testid="tracked-job">{job?.id ?? 'none'}</p>
      <p data-testid="tracked-state">{job?.state ?? 'none'}</p>
      <p data-testid="connection">{connection}</p>
    </>
  )
}

function renderBridge(jobState: Partial<JobStateValue> = {}) {
  return render(
    <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
      <JobEventBridge client={client} />
      <Probe />
    </JobStateProvider>,
  )
}

/** Open the socket and let the resync request settle. */
async function open(): Promise<void> {
  await act(async () => {
    sockets.last.emitOpen()
  })
}

describe('JobEventBridge', () => {
  it('opens the socket on mount and closes it on unmount', () => {
    const { unmount } = renderBridge()

    expect(sockets.sockets).toHaveLength(1)
    expect(screen.getByTestId('connection')).toHaveTextContent('connecting')

    unmount()
    expect(sockets.last.closed).toBe(true)
  })

  it('renders nothing of its own', () => {
    const { container } = render(
      <JobStateProvider>
        <JobEventBridge client={client} />
      </JobStateProvider>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('feeds decoded events into the job store', () => {
    renderBridge({ job: sampleJob })

    act(() => {
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_stage_changed',
          job_id: sampleJobId,
          stage: 'separating',
          previous_stage: 'loading_model',
        }),
      )
    })
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('separating')
  })

  it('refetches the tracked job over REST when the socket opens', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ ...sampleJob, state: 'separating' }))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge({ job: sampleJob })

    await open()

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/v1/jobs/${sampleJobId}`)
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('separating')
    expect(screen.getByTestId('connection')).toHaveTextContent('open')
  })

  it('refetches again after a drop and reconnect', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge({ job: sampleJob })

    await open()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    act(() => {
      sockets.last.emitClose()
    })
    expect(screen.getByTestId('connection')).toHaveTextContent('reconnecting')

    act(() => {
      scheduler.runNext()
    })
    await open()

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(screen.getByTestId('connection')).toHaveTextContent('open')
  })

  it('does nothing on open while no job is tracked', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge()

    await open()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.getByTestId('tracked-job')).toHaveTextContent('none')
  })

  it('adopts a job tracked after the socket was already open', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge()
    await open()
    expect(fetchMock).not.toHaveBeenCalled()

    // A job created while the socket is up arrives as an event, and the
    // next reconnect resyncs it — the tracked id is read at open time.
    act(() => {
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_created',
          job_id: sampleJobId,
          job: sampleJob,
        }),
      )
      sockets.last.emitClose()
      scheduler.runNext()
    })
    await open()

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/v1/jobs/${sampleJobId}`)
  })

  it('keeps the store intact when the resync request fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse(
            { error: { code: 'job_not_found', message: 'Gone.' } },
            404,
          ),
        ),
    )
    renderBridge({ job: sampleJob })

    await open()

    await waitFor(() => {
      expect(screen.getByTestId('connection')).toHaveTextContent('open')
    })
    expect(screen.getByTestId('tracked-job')).toHaveTextContent(sampleJobId)
  })
})
