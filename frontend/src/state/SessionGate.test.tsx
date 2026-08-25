import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { SessionGate, restoredPhase } from './SessionGate'
import { AppStateProvider, useAppState } from './appState'
import { JobStateProvider, useJobState } from './jobState'
import { SESSION_STORAGE_KEY, readSessionSnapshot } from './persistence'
import { sampleAudioFile, sampleJob, sampleJobId } from '../test/fixtures'
import type { Job, JobState } from '../api/types'

/** The sample job in `state`, with optional overrides. */
function jobIn(state: JobState, overrides: Partial<Job> = {}): Job {
  return { ...sampleJob, state, ...overrides }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(code: string, status: number): Response {
  return jsonResponse({ error: { code, message: 'Gone.' } }, status)
}

/**
 * Stub `fetch`, routing by path: `/jobs/{id}` answers with `job` and
 * `/audio/{id}` with `file`. A `null` answer becomes a 404 in the backend's
 * error envelope, which is what a stale id really gets.
 */
function stubFetch(options: {
  job?: Job | null
  file?: typeof sampleAudioFile | null
}) {
  const fetchMock = vi.fn((url: string) => {
    if (url.includes('/jobs/')) {
      return Promise.resolve(
        options.job == null
          ? errorResponse('job_not_found', 404)
          : jsonResponse(options.job),
      )
    }
    if (url.includes('/audio/')) {
      return Promise.resolve(
        options.file == null
          ? errorResponse('audio_not_found', 404)
          : jsonResponse(options.file),
      )
    }
    throw new Error(`unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Store a snapshot the way a previous page would have left it. */
function storeSnapshot(snapshot: Record<string, unknown>): void {
  sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(snapshot))
}

/** Surfaces everything the restore is supposed to have put back. */
function Probe() {
  const { phase, upload } = useAppState()
  const { job } = useJobState()
  return (
    <>
      <p data-testid="phase">{phase}</p>
      <p data-testid="upload">
        {upload.status === 'uploaded' ? upload.file.id : upload.status}
      </p>
      <p data-testid="job">{job?.id ?? 'none'}</p>
      <p data-testid="job-state">{job?.state ?? 'none'}</p>
    </>
  )
}

function renderGate() {
  return render(
    <AppStateProvider>
      <JobStateProvider>
        <SessionGate>
          <Probe />
        </SessionGate>
      </JobStateProvider>
    </AppStateProvider>,
  )
}

/** Render and wait for rehydration to settle. */
async function renderRestored(): Promise<void> {
  renderGate()
  await waitFor(() => {
    expect(screen.getByTestId('phase')).toBeInTheDocument()
  })
}

beforeEach(() => {
  sessionStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  sessionStorage.clear()
})

describe('SessionGate with nothing stored', () => {
  it('renders the workspace immediately and issues no requests', () => {
    const fetchMock = stubFetch({})
    renderGate()

    // Synchronous: a first visit must not be gated behind anything.
    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('SessionGate rehydration', () => {
  it('restores a running job to the separate phase', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'separate',
    })
    stubFetch({ job: jobIn('separating'), file: sampleAudioFile })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    expect(screen.getByTestId('job')).toHaveTextContent(sampleJobId)
    expect(screen.getByTestId('job-state')).toHaveTextContent('separating')
    expect(screen.getByTestId('upload')).toHaveTextContent(sampleAudioFile.id)
  })

  it('rehydrates a job that finished while the page was closed as completed', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'separate',
    })
    // The page was closed while the job ran; the backend answers with what
    // actually happened, which no cached record could have known.
    stubFetch({ job: jobIn('completed'), file: sampleAudioFile })

    await renderRestored()

    expect(screen.getByTestId('job-state')).toHaveTextContent('completed')
    expect(screen.getByTestId('phase')).toHaveTextContent('separate')
  })

  it('returns to the results when that is where the user was', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'inspect',
    })
    stubFetch({ job: jobIn('completed'), file: sampleAudioFile })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('inspect')
    expect(screen.getByTestId('job-state')).toHaveTextContent('completed')
  })

  it('fetches the records rather than trusting anything stored', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'separate',
    })
    const fetchMock = stubFetch({
      job: jobIn('separating'),
      file: sampleAudioFile,
    })

    await renderRestored()

    const paths = fetchMock.mock.calls.map((call) => call[0])
    expect(paths).toContain(`/api/v1/jobs/${sampleJobId}`)
    expect(paths).toContain(`/api/v1/audio/${sampleAudioFile.id}`)
  })

  it('takes the audio id from the job record, not the snapshot', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: '01STALEAUDIOULID000000000',
      phase: 'separate',
    })
    const fetchMock = stubFetch({
      job: jobIn('separating'),
      file: sampleAudioFile,
    })

    await renderRestored()

    expect(fetchMock.mock.calls.map((call) => call[0])).toContain(
      `/api/v1/audio/${sampleJob.audio_id}`,
    )
  })

  it('restores an upload with no job to the configure phase', async () => {
    storeSnapshot({
      jobId: null,
      audioId: sampleAudioFile.id,
      phase: 'configure',
    })
    stubFetch({ file: sampleAudioFile })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('configure')
    expect(screen.getByTestId('job')).toHaveTextContent('none')
  })
})

describe('SessionGate when the stored ids are stale', () => {
  it('starts cleanly on a job the backend has never heard of', async () => {
    storeSnapshot({ jobId: sampleJobId, audioId: null, phase: 'separate' })
    stubFetch({ job: null })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(screen.getByTestId('job')).toHaveTextContent('none')
    // Nothing the user could act on: no alert, and the stored id is gone.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await waitFor(() => {
      expect(readSessionSnapshot().jobId).toBeNull()
    })
  })

  it('falls back to file selection when the upload is gone too', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'inspect',
    })
    stubFetch({ job: null, file: null })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(screen.getByTestId('upload')).toHaveTextContent('idle')
  })

  it('keeps a live job usable when only its upload has gone', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'separate',
    })
    stubFetch({ job: jobIn('separating'), file: null })

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    expect(screen.getByTestId('job')).toHaveTextContent(sampleJobId)
    expect(screen.getByTestId('upload')).toHaveTextContent('idle')
  })

  it('starts cleanly when the backend cannot be reached at all', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'separate',
    })
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    )

    await renderRestored()

    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('gives up rather than waiting on a backend that never answers', async () => {
    vi.useFakeTimers()
    storeSnapshot({ jobId: sampleJobId, audioId: null, phase: 'separate' })
    // A request that accepts and never resolves: the UI must not be gated
    // behind it forever.
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise(() => {})))

    renderGate()
    expect(screen.getByText('Restoring your session…')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })

    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(readSessionSnapshot().jobId).toBeNull()
  })
})

describe('SessionGate persistence', () => {
  it('keeps the restored ids stored for the next reload', async () => {
    storeSnapshot({
      jobId: sampleJobId,
      audioId: sampleAudioFile.id,
      phase: 'inspect',
    })
    stubFetch({ job: jobIn('completed'), file: sampleAudioFile })

    await renderRestored()

    await waitFor(() => {
      expect(readSessionSnapshot()).toEqual({
        jobId: sampleJobId,
        audioId: sampleAudioFile.id,
        phase: 'inspect',
      })
    })
  })

  it('works normally when storage refuses to answer', async () => {
    vi.stubGlobal('sessionStorage', {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
      removeItem: () => {
        throw new Error('blocked')
      },
    })
    const fetchMock = stubFetch({})

    renderGate()

    expect(screen.getByTestId('phase')).toHaveTextContent('select')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('restoredPhase', () => {
  const file = sampleAudioFile

  it('never restores a phase whose data is gone', () => {
    expect(restoredPhase('inspect', null, null)).toBe('select')
    expect(restoredPhase('separate', null, file)).toBe('configure')
  })

  it('sends a running job to separate whatever was stored', () => {
    for (const phase of ['select', 'configure', 'inspect', 'export'] as const) {
      expect(restoredPhase(phase, jobIn('separating'), file)).toBe('separate')
      expect(restoredPhase(phase, jobIn('queued'), file)).toBe('separate')
    }
  })

  it('renders a terminal job in the phase that has a way out of it', () => {
    expect(restoredPhase('inspect', jobIn('cancelled'), file)).toBe('separate')
    expect(restoredPhase('inspect', jobIn('failed'), file)).toBe('separate')
    expect(restoredPhase('separate', jobIn('completed'), file)).toBe('separate')
  })

  it('maps the export phase onto inspect, which renders the export panel', () => {
    expect(restoredPhase('export', jobIn('completed'), file)).toBe('inspect')
  })

  it('defaults a completed job to separate when no phase was stored', () => {
    expect(restoredPhase(null, jobIn('completed'), file)).toBe('separate')
  })
})
