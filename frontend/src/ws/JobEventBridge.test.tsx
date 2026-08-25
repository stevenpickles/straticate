import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { JobEventBridge } from './JobEventBridge'
import { JobEventClient } from './client'
import {
  JobStateProvider,
  initialJobState,
  useJobDispatch,
  useJobState,
  type JobStateValue,
} from '../state/jobState'
import { FakeScheduler, FakeWebSocketFactory } from '../test/mockWebSocket'
import { sampleJob, sampleJobId, sampleResult } from '../test/fixtures'
import type { Job, JobState } from '../api/types'

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

/** A promise plus its resolver, for holding a request in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

/** The sample job in `state`, with optional overrides. */
function jobIn(state: JobState, overrides: Partial<Job> = {}): Job {
  return { ...sampleJob, state, ...overrides }
}

/** Surfaces the tracked job and connection status for assertions. */
function Probe() {
  const { job, connection } = useJobState()
  return (
    <>
      <p data-testid="tracked-job">{job?.id ?? 'none'}</p>
      <p data-testid="tracked-state">{job?.state ?? 'none'}</p>
      <p data-testid="tracked-result">
        {job?.result == null ? 'none' : 'ready'}
      </p>
      <p data-testid="connection">{connection}</p>
    </>
  )
}

/** Tracks `job` on click, standing in for a REST create/fetch response. */
function TrackButton({ job }: { job: Job }) {
  const dispatch = useJobDispatch()
  return (
    <button
      type="button"
      onClick={() => {
        dispatch({ type: 'job/track', job })
      }}
    >
      track
    </button>
  )
}

function renderBridge(
  jobState: Partial<JobStateValue> = {},
  trackable: Job = sampleJob,
) {
  return render(
    <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
      <JobEventBridge client={client} />
      <Probe />
      <TrackButton job={trackable} />
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

  it('opens exactly one socket even as the tracked job changes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(sampleJob)))
    renderBridge()

    await open()
    await userEvent.click(screen.getByRole('button', { name: 'track' }))

    expect(sockets.sockets).toHaveLength(1)
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

  it('never adopts another tab’s job from a job_created broadcast', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge()
    await open()

    // The hub broadcasts to every connected client, so this event may well
    // belong to another tab. Nothing is tracked, and nothing is refetched.
    act(() => {
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_created',
          job_id: sampleJobId,
          job: sampleJob,
        }),
      )
    })
    expect(screen.getByTestId('tracked-job')).toHaveTextContent('none')

    act(() => {
      sockets.last.emitClose()
      scheduler.runNext()
    })
    await open()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('resyncs a job that was tracked after the socket opened', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge()
    await open()
    expect(fetchMock).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'track' }))

    act(() => {
      sockets.last.emitClose()
      scheduler.runNext()
    })
    await open()

    expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/v1/jobs/${sampleJobId}`)
  })

  it('resyncs as soon as a job becomes tracked while the socket is open', async () => {
    // A job restored after a page reload (feature 033) is tracked *after*
    // the socket opened, so the open-time resync had nothing to fetch. Left
    // there, a terminal event that landed while nothing was tracked would
    // be dropped with no further event ever coming.
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(jobIn('separating')))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge()

    await open()
    expect(fetchMock).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'track' }))

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1)
    })
    expect(fetchMock.mock.calls[0]?.[0]).toBe(`/api/v1/jobs/${sampleJobId}`)
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('separating')
  })

  it('does not let the confirming resync rewind a job the events moved on', async () => {
    // The race the `jobId` trigger opens on the hot path of job creation:
    // `POST /jobs` answers `queued`, the bridge confirms with
    // `GET /jobs/{id}`, the backend serves that while the job is still
    // queued — and the socket delivers `separating` before the HTTP response
    // is processed. HTTP and the WebSocket are separate connections; their
    // relative arrival order is not guaranteed.
    //
    // Applied, the stale answer would put the panel back to "waiting in the
    // queue" with no progress bar until the next event landed.
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise))
    renderBridge()

    await open()
    await userEvent.click(screen.getByRole('button', { name: 'track' }))
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('queued')

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

    await act(async () => {
      pending.resolve(jsonResponse(jobIn('queued')))
    })

    expect(screen.getByTestId('tracked-state')).toHaveTextContent('separating')
  })

  it('does not refetch when an event updates the job it already tracks', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(sampleJob))
    vi.stubGlobal('fetch', fetchMock)
    renderBridge({ job: sampleJob })

    await open()
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Progress arrives several times a second; a fetch per event would turn
    // the resync into the polling loop the architecture forbids.
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
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('ignores a resync answer that is not about the tracked job', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ ...sampleJob, id: '01OTHERJOBULID000000000000' }),
        ),
    )
    renderBridge({ job: jobIn('separating') })

    await open()

    await waitFor(() => {
      expect(screen.getByTestId('connection')).toHaveTextContent('open')
    })
    expect(screen.getByTestId('tracked-job')).toHaveTextContent(sampleJobId)
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('separating')
  })

  it('does not let a stale resync snapshot revert a completed job', async () => {
    // The socket is already open when `onOpen` fires, so events keep
    // streaming in while `getJob` is in flight and its snapshot predates
    // the completion.
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise))
    renderBridge({ job: jobIn('separating') })

    await open()

    act(() => {
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_completed',
          job_id: sampleJobId,
          result: sampleResult,
        }),
      )
    })
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('completed')

    await act(async () => {
      pending.resolve(jsonResponse(jobIn('separating')))
    })

    // The job has genuinely finished, so no further event would ever
    // correct a revert: the stale snapshot must lose.
    expect(screen.getByTestId('tracked-state')).toHaveTextContent('completed')
    expect(screen.getByTestId('tracked-result')).toHaveTextContent('ready')
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
