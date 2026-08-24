import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ExportPanel } from './ExportPanel'
import type { Job, SeparationResult, Stem } from '../api/types'
import {
  JobStateProvider,
  initialJobState,
  useJobDispatch,
  type JobStateValue,
} from '../state/jobState'
import { sampleJob, sampleJobId } from '../test/fixtures'

const twoStemNames = ['vocals', 'instrumental']
const fourStemNames = ['vocals', 'drums', 'bass', 'other']

function stem(name: string): Stem {
  return {
    name,
    duration_seconds: 227.4,
    sample_rate_hz: 44100,
    channels: 2,
  }
}

/** A result over exactly the stem names given — two of them or four. */
function resultOver(names: readonly string[]): SeparationResult {
  return {
    job_id: sampleJobId,
    model_id: 'vocals-hq-001',
    stems: names.map(stem),
    metrics: { processing_seconds: 28.8, realtime_factor: 7.9 },
  }
}

function completedJob(names: readonly string[]): Job {
  return {
    ...sampleJob,
    state: 'completed',
    progress: 1,
    result: resultOver(names),
  }
}

function jobState(job: Job | null): JobStateValue {
  return { ...initialJobState, job }
}

// ---------------------------------------------------------------------------
// Responses
// ---------------------------------------------------------------------------

function exportResponse(filename = `${sampleJobId}-wav_pcm24.zip`): Response {
  return new Response('zip-bytes', {
    status: 200,
    headers: { 'Content-Disposition': `attachment; filename="${filename}"` },
  })
}

function errorResponse(
  status: number,
  code: string,
  message: string,
  detail?: unknown,
): Response {
  return new Response(JSON.stringify({ error: { code, message, detail } }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(
  response: Response | Promise<Response>,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockReturnValue(Promise.resolve(response))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// ---------------------------------------------------------------------------
// Object URLs and the save click: jsdom implements neither.
// ---------------------------------------------------------------------------

const objectUrls = URL as unknown as Partial<
  Record<'createObjectURL' | 'revokeObjectURL', unknown>
>

let createObjectURL: ReturnType<typeof vi.fn>
let revokeObjectURL: ReturnType<typeof vi.fn>

beforeEach(() => {
  createObjectURL = vi.fn(() => 'blob:straticate/export')
  revokeObjectURL = vi.fn()
  objectUrls.createObjectURL = createObjectURL
  objectUrls.revokeObjectURL = revokeObjectURL
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
    // A real click would start a download; nothing else to do here.
  })
})

afterEach(async () => {
  // Revokes are deferred by a task; let any pending one land here.
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  delete objectUrls.createObjectURL
  delete objectUrls.revokeObjectURL
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderPanel(job: Job | null = completedJob(twoStemNames)) {
  return render(
    <JobStateProvider initialState={jobState(job)}>
      <ExportPanel />
    </JobStateProvider>,
  )
}

/** Tracks `job` in the store, the way the app does when one is started. */
function TrackedJob({ job }: { job: Job }) {
  const dispatch = useJobDispatch()
  useEffect(() => {
    dispatch({ type: 'job/track', job })
  }, [dispatch, job])
  return null
}

function exportButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Export' })
}

function stemCheckbox(name: string): HTMLInputElement {
  return screen.getByRole('checkbox', { name })
}

/** A response the test settles by hand, to hold a download in flight. */
function deferredResponse(): {
  promise: Promise<Response>
  settle: (response: Response) => void
} {
  let settle!: (response: Response) => void
  const promise = new Promise<Response>((resolve) => {
    settle = resolve
  })
  return { promise, settle }
}

/** The URL the one and only export request was sent to. */
function requestedUrl(fetchMock: ReturnType<typeof vi.fn>): string {
  expect(fetchMock).toHaveBeenCalledTimes(1)
  return String(fetchMock.mock.calls[0]?.[0])
}

describe('ExportPanel stem selection', () => {
  it('renders a checkbox per stem of a two-stem job, all selected', () => {
    renderPanel(completedJob(twoStemNames))

    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(2)
    for (const name of twoStemNames) {
      expect(stemCheckbox(name)).toBeChecked()
    }
  })

  it('renders a checkbox per stem of a four-stem job, all selected', () => {
    renderPanel(completedJob(fourStemNames))

    const checkboxes = screen.getAllByRole('checkbox')
    expect(checkboxes).toHaveLength(4)
    for (const name of fourStemNames) {
      expect(stemCheckbox(name)).toBeChecked()
    }
  })

  it('renders whatever stem names the result carries', () => {
    renderPanel(completedJob(['lead vox', 'backing vox', 'everything else']))

    expect(screen.getAllByRole('checkbox')).toHaveLength(3)
    expect(stemCheckbox('backing vox')).toBeChecked()
  })

  it('disables export when every stem is deselected', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch(exportResponse())
    renderPanel(completedJob(twoStemNames))

    for (const name of twoStemNames) {
      await user.click(stemCheckbox(name))
    }

    expect(exportButton()).toBeDisabled()
    expect(
      screen.getByText('Select at least one stem to export.'),
    ).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('re-enables export when a stem is selected again', async () => {
    const user = userEvent.setup()
    stubFetch(exportResponse())
    renderPanel(completedJob(twoStemNames))

    await user.click(stemCheckbox('vocals'))
    await user.click(stemCheckbox('instrumental'))
    expect(exportButton()).toBeDisabled()

    await user.click(stemCheckbox('vocals'))
    expect(exportButton()).toBeEnabled()
  })

  it('says a single stem downloads as one audio file', async () => {
    const user = userEvent.setup()
    renderPanel(completedJob(twoStemNames))

    await user.click(stemCheckbox('instrumental'))

    expect(
      screen.getByText('You will get a single .wav file.'),
    ).toBeInTheDocument()
  })

  it('says several stems download as a zip with separation.json', () => {
    renderPanel(completedJob(fourStemNames))

    expect(
      screen.getByText(
        'You will get a .zip with 4 audio files and separation.json.',
      ),
    ).toBeInTheDocument()
  })
})

describe('ExportPanel format selection', () => {
  it('offers every format of the generated union, defaulting to wav_pcm24', () => {
    renderPanel()

    const select = screen.getByRole('combobox', { name: 'Format' })
    const values = within(select)
      .getAllByRole('option')
      .map((option) => (option as HTMLOptionElement).value)
    expect(values).toEqual(['wav_pcm24', 'wav_float32', 'flac'])
    expect(select).toHaveValue('wav_pcm24')
  })

  it('states that 24-bit and float exports add no information', () => {
    renderPanel()

    expect(
      screen.getByText(/change the encoding without adding\s+information/i),
    ).toBeInTheDocument()
  })

  it('sends the chosen format', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch(exportResponse())
    renderPanel(completedJob(twoStemNames))

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Format' }),
      'flac',
    )
    await user.click(exportButton())

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(requestedUrl(fetchMock)).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=flac`,
    )
  })
})

describe('ExportPanel download', () => {
  it('omits the stems parameter when everything is selected', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch(exportResponse())
    renderPanel(completedJob(fourStemNames))

    await user.click(exportButton())

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(requestedUrl(fetchMock)).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24`,
    )
  })

  it('sends the selected subset in the result’s order', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch(exportResponse())
    renderPanel(completedJob(fourStemNames))

    await user.click(stemCheckbox('drums'))
    await user.click(exportButton())

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(requestedUrl(fetchMock)).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=vocals,bass,other`,
    )
  })

  it('sends a single stem on its own', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch(exportResponse())
    renderPanel(completedJob(twoStemNames))

    await user.click(stemCheckbox('vocals'))
    await user.click(exportButton())

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(requestedUrl(fetchMock)).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=instrumental`,
    )
  })

  it('confirms the download by the filename the server offered', async () => {
    const user = userEvent.setup()
    stubFetch(exportResponse('my-song-stems.zip'))
    renderPanel()

    await user.click(exportButton())

    expect(
      await screen.findByText('Downloaded my-song-stems.zip.'),
    ).toBeInTheDocument()
    // The revoke is deferred by a task so the browser can take the download.
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:straticate/export')
    })
  })

  it('downloads once for a double click', async () => {
    const user = userEvent.setup()
    const pending = deferredResponse()
    const fetchMock = stubFetch(pending.promise)
    renderPanel()

    const button = exportButton()
    await user.dblClick(button)

    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Let the single request settle so nothing is left in flight.
    pending.settle(exportResponse('once.zip'))
    expect(await screen.findByText('Downloaded once.zip.')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('disables the button while the download is in flight', async () => {
    const user = userEvent.setup()
    const pending = deferredResponse()
    stubFetch(pending.promise)
    renderPanel()

    await user.click(exportButton())

    expect(exportButton()).toBeDisabled()
    expect(exportButton()).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByText('Preparing your download…')).toBeInTheDocument()

    pending.settle(exportResponse())
    await waitFor(() => {
      expect(exportButton()).toBeEnabled()
    })
  })
})

describe('ExportPanel errors', () => {
  /** Click Export against `response` and return the alert it rendered. */
  async function exportAndFail(response: Response): Promise<HTMLElement> {
    const user = userEvent.setup()
    stubFetch(response)
    renderPanel()
    await user.click(exportButton())
    return await screen.findByRole('alert')
  }

  it('explains a missing job and re-enables the button', async () => {
    const alert = await exportAndFail(
      errorResponse(404, 'job_not_found', 'No such job.'),
    )

    expect(alert).toHaveTextContent(/no longer knows about this job/i)
    expect(alert).toHaveTextContent(/run the separation again/i)
    expect(exportButton()).toBeEnabled()
  })

  it('explains a job that is still running', async () => {
    const alert = await exportAndFail(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'separating',
      }),
    )

    expect(alert).toHaveTextContent(
      'The stems are not ready yet — this job is separating. Try again once it finishes.',
    )
    expect(exportButton()).toBeEnabled()
  })

  it('explains a cancelled job', async () => {
    const alert = await exportAndFail(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'cancelled',
      }),
    )

    expect(alert).toHaveTextContent(
      'This separation was cancelled, so there are no stems to export.',
    )
  })

  it('explains a failed job', async () => {
    const alert = await exportAndFail(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'failed',
      }),
    )

    expect(alert).toHaveTextContent(
      'This separation failed, so there are no stems to export.',
    )
  })

  it('explains a 409 with no state in its detail', async () => {
    const alert = await exportAndFail(
      errorResponse(409, 'result_not_available', 'No result.'),
    )

    expect(alert).toHaveTextContent('The stems are not ready yet.')
  })

  it('explains a stale stem selection', async () => {
    const alert = await exportAndFail(
      errorResponse(404, 'stem_not_found', 'Unknown stem.', {
        available_stems: twoStemNames,
      }),
    )

    expect(alert).toHaveTextContent(/not part of this job any more/i)
    expect(exportButton()).toBeEnabled()
  })

  it('explains stems that are gone from disk', async () => {
    const alert = await exportAndFail(
      errorResponse(404, 'stem_file_missing', 'Gone.', { stem: 'vocals' }),
    )

    expect(alert).toHaveTextContent(/gone from disk/i)
  })

  it('explains a failed export without surfacing the reason classification', async () => {
    const alert = await exportAndFail(
      errorResponse(500, 'export_failed', 'The export failed.', {
        job_id: sampleJobId,
        format: 'flac',
        reason: 'transcode_failed',
      }),
    )

    expect(alert).toHaveTextContent(/could not be built/i)
    expect(alert).not.toHaveTextContent(/transcode_failed/)
    expect(exportButton()).toBeEnabled()
  })

  it('explains a validation error as the bug it is', async () => {
    const alert = await exportAndFail(
      errorResponse(422, 'validation_error', 'Invalid request.'),
    )

    expect(alert).toHaveTextContent(/bug/i)
  })

  it('falls back to the envelope message for an unexpected code', async () => {
    const alert = await exportAndFail(
      errorResponse(500, 'internal_error', 'Something went wrong.'),
    )

    expect(alert).toHaveTextContent('Something went wrong.')
  })

  it('reports a network failure and stays usable', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )
    renderPanel()

    await user.click(exportButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong. Please try again.',
    )
    expect(exportButton()).toBeEnabled()
  })

  it('lets a failed export be retried', async () => {
    const user = userEvent.setup()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        errorResponse(500, 'export_failed', 'The export failed.', {
          reason: 'filesystem_error',
        }),
      )
      .mockResolvedValueOnce(exportResponse('retry.zip'))
    vi.stubGlobal('fetch', fetchMock)
    renderPanel()

    await user.click(exportButton())
    expect(await screen.findByRole('alert')).toBeInTheDocument()

    await user.click(exportButton())

    expect(await screen.findByText('Downloaded retry.zip.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('ExportPanel outcome staleness', () => {
  it('clears a failure when the selection changes', async () => {
    const user = userEvent.setup()
    stubFetch(
      errorResponse(404, 'stem_not_found', 'Unknown stem.', {
        available_stems: twoStemNames,
      }),
    )
    renderPanel(completedJob(fourStemNames))

    await user.click(exportButton())
    // The message asks the user to change the selection, so doing exactly
    // that must not leave the same error on screen.
    expect(await screen.findByRole('alert')).toBeInTheDocument()

    await user.click(stemCheckbox('drums'))

    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('clears a success when the selection changes', async () => {
    const user = userEvent.setup()
    stubFetch(exportResponse('all-stems.zip'))
    renderPanel(completedJob(fourStemNames))

    await user.click(exportButton())
    expect(
      await screen.findByText('Downloaded all-stems.zip.'),
    ).toBeInTheDocument()

    await user.click(stemCheckbox('drums'))

    expect(screen.queryByText('Downloaded all-stems.zip.')).toBeNull()
  })

  it('clears a success when the format changes', async () => {
    const user = userEvent.setup()
    stubFetch(exportResponse('all-stems.zip'))
    renderPanel(completedJob(twoStemNames))

    await user.click(exportButton())
    expect(
      await screen.findByText('Downloaded all-stems.zip.'),
    ).toBeInTheDocument()

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Format' }),
      'flac',
    )

    expect(screen.queryByText('Downloaded all-stems.zip.')).toBeNull()
  })

  it('keeps the pending state when the selection changes mid-download', async () => {
    const user = userEvent.setup()
    const pending = deferredResponse()
    stubFetch(pending.promise)
    renderPanel(completedJob(fourStemNames))

    await user.click(exportButton())
    await user.click(stemCheckbox('drums'))

    expect(screen.getByText('Preparing your download…')).toBeInTheDocument()
    expect(exportButton()).toBeDisabled()

    pending.settle(exportResponse('all-stems.zip'))
    expect(
      await screen.findByText('Downloaded all-stems.zip.'),
    ).toBeInTheDocument()
  })
})

describe('ExportPanel across jobs', () => {
  it('starts a different job from the defaults', async () => {
    const user = userEvent.setup()
    stubFetch(exportResponse('first.zip'))
    const first = completedJob(twoStemNames)
    const second: Job = {
      ...completedJob(fourStemNames),
      id: '01SECONDJOBULID0000000000',
    }
    const { rerender } = render(
      <JobStateProvider initialState={jobState(null)}>
        <TrackedJob job={first} />
        <ExportPanel />
      </JobStateProvider>,
    )

    await user.click(stemCheckbox('vocals'))
    await user.click(exportButton())
    expect(await screen.findByText('Downloaded first.zip.')).toBeInTheDocument()

    // A second separation of a four-stem model, tracked in the same panel.
    rerender(
      <JobStateProvider initialState={jobState(null)}>
        <TrackedJob job={second} />
        <ExportPanel />
      </JobStateProvider>,
    )

    expect(screen.getAllByRole('checkbox')).toHaveLength(4)
    for (const name of fourStemNames) {
      expect(stemCheckbox(name)).toBeChecked()
    }
    expect(screen.queryByText('Downloaded first.zip.')).toBeNull()

    // The first interaction after the switch must not resurrect the previous
    // job's deselection (`vocals`) or its download outcome — the user would
    // otherwise get an export missing a stem they never unticked.
    const fetchMock = stubFetch(exportResponse('second.zip'))
    await user.click(stemCheckbox('bass'))

    expect(stemCheckbox('vocals')).toBeChecked()
    expect(stemCheckbox('drums')).toBeChecked()
    expect(stemCheckbox('other')).toBeChecked()
    expect(stemCheckbox('bass')).not.toBeChecked()
    expect(screen.queryByText('Downloaded first.zip.')).toBeNull()

    await user.click(exportButton())

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled()
    })
    expect(requestedUrl(fetchMock)).toBe(
      '/api/v1/jobs/01SECONDJOBULID0000000000/export?format=wav_pcm24&stems=vocals,drums,other',
    )
  })

  it('leaves the button working when the job changes mid-download', async () => {
    const user = userEvent.setup()
    const pending = deferredResponse()
    const fetchMock = vi
      .fn()
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(exportResponse('second.zip'))
    vi.stubGlobal('fetch', fetchMock)
    const first = completedJob(twoStemNames)
    const second: Job = {
      ...completedJob(fourStemNames),
      id: '01SECONDJOBULID0000000000',
    }
    const { rerender } = render(
      <JobStateProvider initialState={jobState(null)}>
        <TrackedJob job={first} />
        <ExportPanel />
      </JobStateProvider>,
    )

    // The first export is still running (the first one of a format/selection
    // runs FFmpeg) when the user starts another separation and comes back.
    await user.click(exportButton())
    expect(exportButton()).toBeDisabled()

    rerender(
      <JobStateProvider initialState={jobState(null)}>
        <TrackedJob job={second} />
        <ExportPanel />
      </JobStateProvider>,
    )

    expect(exportButton()).toBeEnabled()
    await user.click(exportButton())

    expect(
      await screen.findByText('Downloaded second.zip.'),
    ).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
      '/api/v1/jobs/01SECONDJOBULID0000000000/export?format=wav_pcm24',
    )

    // The abandoned download settles without writing into the new job's panel.
    pending.settle(exportResponse('first.zip'))
    await waitFor(() => {
      expect(screen.queryByText('Downloaded first.zip.')).toBeNull()
    })
  })
})

describe('ExportPanel without a result', () => {
  it('says so when no job is tracked', () => {
    renderPanel(null)

    expect(
      screen.getByText('No separation job is being tracked.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Export' })).toBeNull()
  })

  it('says so when the tracked job has no result', () => {
    renderPanel({ ...sampleJob, state: 'separating', result: null })

    expect(
      screen.getByText('This job has no result to export yet.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Export' })).toBeNull()
  })
})
