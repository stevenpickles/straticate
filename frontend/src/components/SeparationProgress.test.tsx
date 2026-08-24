import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SeparationProgress } from './SeparationProgress'
import {
  JobStateProvider,
  initialJobState,
  type JobStateValue,
} from '../state/jobState'
import { JobEventBridge } from '../ws/JobEventBridge'
import { JobEventClient } from '../ws/client'
import { FakeScheduler, FakeWebSocketFactory } from '../test/mockWebSocket'
import { sampleJob, sampleJobId, sampleResult } from '../test/fixtures'
import type { Job, JobState, WebSocketEvent } from '../api/types'

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

function errorResponse(
  code: string,
  message: string,
  status: number,
): Response {
  return jsonResponse({ error: { code, message } }, status)
}

/** The sample job in `state`, with optional overrides. */
function jobIn(state: JobState, overrides: Partial<Job> = {}): Job {
  return { ...sampleJob, state, ...overrides }
}

type FetchMock = ReturnType<typeof vi.fn>

/**
 * Stub `fetch`, routing by URL: the cancel command answers with `cancel`
 * (by default a job that is *still separating*, which is what cancelling a
 * running job really returns), and a job read answers with `job`.
 */
function stubFetch(
  options: { job?: Job; cancel?: Response | Promise<Response> } = {},
): FetchMock {
  const fetchMock = vi.fn((url: string) => {
    if (url.endsWith('/cancel')) {
      return Promise.resolve(
        options.cancel ?? jsonResponse(jobIn('separating')),
      )
    }
    if (url.includes('/jobs/')) {
      return Promise.resolve(jsonResponse(options.job ?? sampleJob))
    }
    throw new Error(`unexpected fetch: ${String(url)}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** `{url, method}` of every cancel command issued so far, in order. */
function cancelRequests(
  fetchMock: FetchMock,
): { url: string; method: unknown }[] {
  return fetchMock.mock.calls
    .map((call) => ({
      url: String(call[0]),
      method: (call[1] as { method?: unknown } | undefined)?.method,
    }))
    .filter((call) => call.url.endsWith('/cancel'))
}

/**
 * Render the panel with the socket live, so events drive the UI exactly as
 * they do in the app. The connection settles at `open`, which also resyncs
 * the tracked job over REST — hence the stubbed `fetch`.
 */
async function renderLive(jobState: Partial<JobStateValue> = {}) {
  const view = render(
    <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
      <JobEventBridge client={client} />
      <SeparationProgress />
    </JobStateProvider>,
  )
  await act(async () => {
    sockets.last.emitOpen()
  })
  return view
}

/** Render the panel over a fixed store state, without a socket. */
function renderStatic(jobState: Partial<JobStateValue> = {}) {
  return render(
    <JobStateProvider
      initialState={{ ...initialJobState, connection: 'open', ...jobState }}
    >
      <SeparationProgress />
    </JobStateProvider>,
  )
}

/** Deliver one decoded event over the live socket. */
function emit(event: WebSocketEvent): void {
  act(() => {
    sockets.last.emitMessage(JSON.stringify(event))
  })
}

describe('SeparationProgress without a job', () => {
  it('says nothing is being tracked', () => {
    renderStatic()
    expect(
      screen.getByText('No separation job is being tracked.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('SeparationProgress queued job', () => {
  it('says the job is waiting and shows no progress bar', () => {
    renderStatic({ job: jobIn('queued') })
    expect(screen.getByText('Queued')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Waiting in the queue — this separation has not started yet.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('still offers to cancel', () => {
    renderStatic({ job: jobIn('queued') })
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })
})

describe('SeparationProgress stages', () => {
  const processingStates: [JobState, string][] = [
    ['preparing', 'Preparing'],
    ['decoding', 'Decoding'],
    ['loading_model', 'Loading model'],
    ['separating', 'Separating'],
    ['post_processing', 'Post processing'],
    ['encoding', 'Encoding'],
  ]

  it.each(processingStates)('humanizes the %s state', (state, label) => {
    renderStatic({ job: jobIn(state) })
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it.each(processingStates)(
    'shows a determinate progress bar while %s',
    (state) => {
      renderStatic({ job: jobIn(state, { progress: 0.4 }) })
      const bar = screen.getByRole('progressbar')
      expect(bar).toHaveAttribute('aria-valuenow', '40')
      expect(bar).toHaveAttribute('aria-valuemin', '0')
      expect(bar).toHaveAttribute('aria-valuemax', '100')
      expect(screen.getByText('40%')).toBeInTheDocument()
    },
  )
})

describe('SeparationProgress live progress', () => {
  it('renders chunk-grained progress from a job_progress event', async () => {
    stubFetch({ job: jobIn('separating') })
    await renderLive({ job: jobIn('separating') })

    emit({
      type: 'job_progress',
      job_id: sampleJobId,
      stage: 'separating',
      progress: 0.65,
      chunks_completed: 31,
      chunks_total: 48,
      elapsed_seconds: 18.2,
      audio_processed_seconds: 148.0,
      audio_total_seconds: 227.4,
    })

    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuenow',
      '65',
    )
    expect(screen.getByText('65%')).toBeInTheDocument()
    expect(screen.getByText('31 / 48')).toBeInTheDocument()
    expect(screen.getByText('0:18')).toBeInTheDocument()
    expect(screen.getByText('2:28 / 3:47')).toBeInTheDocument()
  })

  it('follows the stage a job_progress event reports', async () => {
    stubFetch({ job: jobIn('loading_model') })
    await renderLive({ job: jobIn('loading_model') })
    expect(screen.getByText('Loading model')).toBeInTheDocument()

    emit({
      type: 'job_progress',
      job_id: sampleJobId,
      stage: 'separating',
      progress: 0.1,
      chunks_completed: 5,
      chunks_total: 48,
      elapsed_seconds: 2,
      audio_processed_seconds: 22,
      audio_total_seconds: 227.4,
    })

    expect(screen.getByText('Separating')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute(
      'aria-valuenow',
      '10',
    )
  })
})

describe('SeparationProgress terminal states', () => {
  it('reports a completed job and hands off to the results UI', () => {
    renderStatic({
      job: jobIn('completed', { progress: 1, result: sampleResult }),
    })
    expect(screen.getByText('Completed')).toBeInTheDocument()
    expect(
      screen.getByText(/Separation complete — 2 stems are ready\./),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Playback and export arrive with the results UI\./),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('reports the stage a cancelled job was stopped at', () => {
    renderStatic({
      job: jobIn('cancelled'),
      cancelledAtStage: 'loading_model',
    })
    expect(
      screen.getByText('Separation cancelled while loading model.'),
    ).toBeInTheDocument()
  })

  it('reports a cancellation whose stage is not known', () => {
    renderStatic({ job: jobIn('cancelled') })
    expect(screen.getByText('Separation cancelled.')).toBeInTheDocument()
  })

  it('reports a failed job with its message and code', () => {
    renderStatic({
      job: jobIn('failed', {
        error: {
          code: 'cuda_out_of_memory',
          message: 'The GPU ran out of memory.',
        },
      }),
    })
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The GPU ran out of memory.',
    )
    expect(
      screen.getByText('Error code: cuda_out_of_memory'),
    ).toBeInTheDocument()
  })

  it.each<JobState>(['completed', 'cancelled', 'failed'])(
    'offers no cancel button once the job is %s',
    (state) => {
      renderStatic({ job: jobIn(state) })
      expect(screen.queryByRole('button')).not.toBeInTheDocument()
    },
  )
})

describe('SeparationProgress cancellation', () => {
  it('posts one cancel, waits, and settles on the job_cancelled event', async () => {
    const fetchMock = stubFetch({ job: jobIn('separating') })
    await renderLive({ job: jobIn('separating') })

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(cancelRequests(fetchMock)).toEqual([
      { url: `/api/v1/jobs/${sampleJobId}/cancel`, method: 'POST' },
    ])

    // The response is still a processing state: the request is not the stop.
    const cancelling = await screen.findByRole('button', {
      name: 'Cancelling…',
    })
    expect(cancelling).toBeDisabled()
    expect(
      screen.getByText('Waiting for the job to stop at its next checkpoint.'),
    ).toBeInTheDocument()
    expect(screen.getByText('Separating')).toBeInTheDocument()

    emit({
      type: 'job_cancelled',
      job_id: sampleJobId,
      stage_at_cancellation: 'separating',
    })

    expect(
      screen.getByText('Separation cancelled while separating.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(cancelRequests(fetchMock)).toHaveLength(1)
  })

  it('issues a single cancel for a double click', async () => {
    const fetchMock = stubFetch({ job: jobIn('separating') })
    await renderLive({ job: jobIn('separating') })

    await userEvent.dblClick(screen.getByRole('button', { name: 'Cancel' }))

    expect(cancelRequests(fetchMock)).toHaveLength(1)
    await screen.findByRole('button', { name: 'Cancelling…' })
    expect(cancelRequests(fetchMock)).toHaveLength(1)
  })

  it('shows the envelope message of a failed cancel and stays retryable', async () => {
    const fetchMock = stubFetch({
      job: jobIn('separating'),
      cancel: errorResponse(
        'service_unavailable',
        'The job manager is shutting down.',
        503,
      ),
    })
    await renderLive({ job: jobIn('separating') })

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The job manager is shutting down.',
    )
    const retry = screen.getByRole('button', { name: 'Cancel' })
    expect(retry).toBeEnabled()

    await userEvent.click(retry)
    await waitFor(() => {
      expect(cancelRequests(fetchMock)).toHaveLength(2)
    })
  })
})

describe('SeparationProgress connection status', () => {
  it('surfaces a socket that dropped', async () => {
    stubFetch({ job: jobIn('separating') })
    await renderLive({ job: jobIn('separating') })
    expect(screen.queryByText(/live progress/i)).not.toBeInTheDocument()

    act(() => {
      sockets.last.emitClose()
    })

    expect(
      screen.getByText('Live progress interrupted — reconnecting…'),
    ).toBeInTheDocument()
  })

  it('says so while the socket is still connecting', () => {
    renderStatic({ job: jobIn('separating'), connection: 'connecting' })
    expect(
      screen.getByText('Connecting for live progress…'),
    ).toBeInTheDocument()
  })

  it('says so when the socket is closed', () => {
    renderStatic({ job: jobIn('separating'), connection: 'closed' })
    expect(
      screen.getByText('Live progress is disconnected.'),
    ).toBeInTheDocument()
  })
})
