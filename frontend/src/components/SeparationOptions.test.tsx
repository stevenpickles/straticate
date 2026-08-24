import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SeparationOptions } from './SeparationOptions'
import {
  AppStateProvider,
  initialConfigureState,
  useAppState,
  type AppState,
} from '../state/appState'
import { JobStateProvider, useJobState } from '../state/jobState'
import {
  sampleAudioFile,
  sampleJob,
  sampleSeparationModes,
} from '../test/fixtures'
import type { SeparationMode } from '../api/types'

const [twoStemMode, fourStemMode] = sampleSeparationModes

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

/** A promise plus its resolver, for holding a request in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

type FetchMock = ReturnType<typeof vi.fn>

/**
 * Stub `fetch`, routing by URL: `/separation-modes` gets `modes` (or the
 * `Response` given), `/jobs` gets `job`.
 */
function stubFetch(options: {
  modes?: SeparationMode[] | Response | Promise<Response>
  job?: Response | Promise<Response>
}): FetchMock {
  const modes = options.modes ?? sampleSeparationModes
  const fetchMock = vi.fn((url: string) => {
    if (url.endsWith('/separation-modes')) {
      return Promise.resolve(Array.isArray(modes) ? jsonResponse(modes) : modes)
    }
    if (url.endsWith('/jobs')) {
      return Promise.resolve(options.job ?? jsonResponse(sampleJob, 201))
    }
    throw new Error(`unexpected fetch: ${url}`)
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Surfaces the workflow phase and the tracked job id for assertions. */
function Probe() {
  const { phase } = useAppState()
  const { job } = useJobState()
  return (
    <>
      <p data-testid="phase">{phase}</p>
      <p data-testid="tracked-job">{job?.id ?? 'none'}</p>
    </>
  )
}

function renderOptions() {
  const initialState: AppState = {
    phase: 'configure',
    upload: { status: 'uploaded', file: sampleAudioFile },
    configure: initialConfigureState,
  }
  return render(
    <AppStateProvider initialState={initialState}>
      <JobStateProvider>
        <SeparationOptions />
        <Probe />
      </JobStateProvider>
    </AppStateProvider>,
  )
}

/** The request bodies of every `POST /api/v1/jobs` call made so far. */
function postedJobBodies(fetchMock: FetchMock): unknown[] {
  return fetchMock.mock.calls
    .filter(([url]) => (url as string).endsWith('/jobs'))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SeparationOptions catalog rendering', () => {
  it('renders every mode, its stems and its quality tiers from the response', async () => {
    stubFetch({})
    renderOptions()

    // Modes: the two-stem and the four-stem mode alike.
    for (const mode of sampleSeparationModes) {
      expect(
        await screen.findByRole('radio', { name: mode.display_name }),
      ).toBeInTheDocument()
      // Every stem the mode produces is shown, however many there are.
      for (const stem of mode.stems) {
        expect(screen.getAllByText(stem).length).toBeGreaterThan(0)
      }
    }
    expect(fourStemMode?.stems).toHaveLength(4)

    // Tiers of the preselected (first) mode.
    for (const option of twoStemMode?.quality_options ?? []) {
      expect(
        screen.getByRole('radio', { name: option.display_name }),
      ).toBeInTheDocument()
    }
  })

  it('preselects the first mode and its first quality tier', async () => {
    stubFetch({})
    renderOptions()

    expect(
      await screen.findByRole('radio', { name: twoStemMode?.display_name }),
    ).toBeChecked()
    expect(
      screen.getByRole('radio', {
        name: twoStemMode?.quality_options[0]?.display_name,
      }),
    ).toBeChecked()
  })

  it('loads the catalog exactly once per mount', async () => {
    const fetchMock = stubFetch({})
    renderOptions()

    await screen.findByRole('radio', { name: twoStemMode?.display_name })
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        (url as string).endsWith('/separation-modes'),
      ),
    ).toHaveLength(1)
  })

  it('renders a mode served by a single model with its one tier', async () => {
    stubFetch({})
    renderOptions()

    await userEvent.click(
      await screen.findByRole('radio', { name: fourStemMode?.display_name }),
    )

    expect(fourStemMode?.quality_options).toHaveLength(1)
    expect(
      screen.getByRole('radio', {
        name: fourStemMode?.quality_options[0]?.display_name,
      }),
    ).toBeChecked()
  })

  it('selecting a mode swaps its quality options and resets the selection', async () => {
    stubFetch({})
    renderOptions()

    // Pick the second tier of the two-stem mode…
    await userEvent.click(
      await screen.findByRole('radio', {
        name: twoStemMode?.quality_options[1]?.display_name,
      }),
    )
    expect(
      screen.getByRole('radio', {
        name: twoStemMode?.quality_options[1]?.display_name,
      }),
    ).toBeChecked()

    // …then switch modes: the other mode's tiers replace them.
    await userEvent.click(
      screen.getByRole('radio', { name: fourStemMode?.display_name }),
    )
    expect(
      screen.queryByRole('radio', {
        name: twoStemMode?.quality_options[1]?.display_name,
      }),
    ).not.toBeInTheDocument()

    // Switching back resets to the first tier, not the one chosen before.
    await userEvent.click(
      screen.getByRole('radio', { name: twoStemMode?.display_name }),
    )
    expect(
      screen.getByRole('radio', {
        name: twoStemMode?.quality_options[0]?.display_name,
      }),
    ).toBeChecked()
    expect(
      screen.getByRole('radio', {
        name: twoStemMode?.quality_options[1]?.display_name,
      }),
    ).not.toBeChecked()
  })
})

describe('SeparationOptions starting a separation', () => {
  it('posts the selected audio, mode and tier with no device_id', async () => {
    const fetchMock = stubFetch({})
    renderOptions()

    await userEvent.click(
      await screen.findByRole('radio', {
        name: twoStemMode?.quality_options[1]?.display_name,
      }),
    )
    await userEvent.click(
      screen.getByRole('button', { name: 'Start separation' }),
    )

    await waitFor(() => {
      expect(postedJobBodies(fetchMock)).toHaveLength(1)
    })
    const [body] = postedJobBodies(fetchMock)
    expect(body).toEqual({
      audio_id: sampleAudioFile.id,
      mode_id: twoStemMode?.id,
      quality_id: twoStemMode?.quality_options[1]?.id,
    })
    expect(Object.keys(body as object)).not.toContain('device_id')

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        audio_id: sampleAudioFile.id,
        mode_id: twoStemMode?.id,
        quality_id: twoStemMode?.quality_options[1]?.id,
      }),
    })
  })

  it('tracks the returned job and advances the workflow to separate', async () => {
    stubFetch({})
    renderOptions()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Start separation' }),
    )

    await waitFor(() => {
      expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    })
    expect(screen.getByTestId('tracked-job')).toHaveTextContent(sampleJob.id)
  })

  it('disables the button while the create is in flight', async () => {
    const pending = deferred<Response>()
    stubFetch({ job: pending.promise })
    renderOptions()

    const button = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await userEvent.click(button)

    await waitFor(() => {
      expect(button).toBeDisabled()
    })

    pending.resolve(jsonResponse(sampleJob, 201))
    await waitFor(() => {
      expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    })
  })

  it('submits once when the button is clicked twice', async () => {
    const pending = deferred<Response>()
    const fetchMock = stubFetch({ job: pending.promise })
    renderOptions()

    const button = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await userEvent.dblClick(button)

    expect(postedJobBodies(fetchMock)).toHaveLength(1)

    pending.resolve(jsonResponse(sampleJob, 201))
    await waitFor(() => {
      expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    })
  })

  it('shows the envelope message and re-enables the button on failure', async () => {
    stubFetch({
      job: errorResponse('audio_not_found', 'No such audio file.', 404),
    })
    renderOptions()

    const button = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await userEvent.click(button)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'No such audio file.',
    )
    expect(button).toBeEnabled()
    expect(screen.getByTestId('phase')).toHaveTextContent('configure')
    expect(screen.getByTestId('tracked-job')).toHaveTextContent('none')
  })

  it('lets the user retry after a failed create', async () => {
    let jobAttempts = 0
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith('/separation-modes')) {
        return Promise.resolve(jsonResponse(sampleSeparationModes))
      }
      jobAttempts += 1
      return Promise.resolve(
        jobAttempts === 1
          ? errorResponse('service_unavailable', 'Server is busy.', 503)
          : jsonResponse(sampleJob, 201),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    renderOptions()

    const button = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await userEvent.click(button)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Server is busy.',
    )

    await userEvent.click(button)
    await waitFor(() => {
      expect(screen.getByTestId('phase')).toHaveTextContent('separate')
    })
    expect(postedJobBodies(fetchMock)).toHaveLength(2)
  })
})

describe('SeparationOptions catalog failure', () => {
  it('shows the envelope message with a retry that refetches', async () => {
    let attempts = 0
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith('/separation-modes')) {
        attempts += 1
        return Promise.resolve(
          attempts === 1
            ? errorResponse(
                'model_catalog_unavailable',
                'The model catalog could not be read.',
                503,
              )
            : jsonResponse(sampleSeparationModes),
        )
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    renderOptions()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The model catalog could not be read.',
    )
    expect(
      screen.queryByRole('button', { name: 'Start separation' }),
    ).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(
      await screen.findByRole('radio', { name: twoStemMode?.display_name }),
    ).toBeChecked()
    expect(attempts).toBe(2)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
