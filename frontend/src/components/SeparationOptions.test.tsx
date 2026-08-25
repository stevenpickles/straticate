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
  modelInstalling,
  sampleAudioFile,
  sampleBuiltInModel,
  sampleInstallableModel,
  sampleJob,
  sampleSeparationModes,
  sampleWeightsBytes,
} from '../test/fixtures'
import type { Model, SeparationMode } from '../api/types'
import { formatFileSize } from '../format'

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

/** The model ID a request path names (`/api/v1/models/vocals-hq-001`). */
function requestedModelId(url: string): string {
  return decodeURIComponent(url.split('/models/')[1]?.split('/')[0] ?? '')
}

/**
 * Stub `fetch`, routing by URL: `/separation-modes` gets `modes` (or the
 * `Response` given), `/jobs` gets `job`, and `/models/{id}` gets `model`
 * (called with the requested ID, so a test can answer per model).
 *
 * The default model needs no download, which is what keeps every test that is
 * *not* about installation reading as it did before feature 035.
 */
function stubFetch(options: {
  modes?: SeparationMode[] | Response | Promise<Response>
  job?: Response | Promise<Response>
  model?: (modelId: string) => Response | Promise<Response>
  install?: (modelId: string) => Response | Promise<Response>
}): FetchMock {
  const modes = options.modes ?? sampleSeparationModes
  const model =
    options.model ??
    ((modelId: string) =>
      jsonResponse({
        ...sampleBuiltInModel,
        id: modelId,
        display_name: modelId,
      }))
  const fetchMock = vi.fn((url: string) => {
    if (url.endsWith('/separation-modes')) {
      return Promise.resolve(Array.isArray(modes) ? jsonResponse(modes) : modes)
    }
    if (url.endsWith('/jobs')) {
      return Promise.resolve(options.job ?? jsonResponse(sampleJob, 201))
    }
    if (url.endsWith('/install')) {
      const install = options.install ?? model
      return Promise.resolve(install(requestedModelId(url)))
    }
    if (url.includes('/models/')) {
      return Promise.resolve(model(requestedModelId(url)))
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
      if (url.includes('/models/')) {
        return Promise.resolve(jsonResponse(sampleBuiltInModel))
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
      if (url.includes('/models/')) {
        return Promise.resolve(jsonResponse(sampleBuiltInModel))
      }
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

describe('SeparationOptions model weights', () => {
  /**
   * The configure step of a fresh checkout: the selected tier is backed by a
   * model whose weights are a download. `serve` decides what the next read of
   * that model answers, so a test can move it through its states without any
   * dependence on when the poll happens to fire.
   */
  function stubInstallableModel(initial: Model = sampleInstallableModel) {
    let current = initial
    const fetchMock = stubFetch({
      model: () => jsonResponse(current),
      install: () => {
        current = modelInstalling({
          state: 'downloading',
          downloaded_bytes: sampleWeightsBytes / 2,
          progress: 0.5,
        })
        return jsonResponse(current, 202)
      },
    })
    return {
      fetchMock,
      serve(model: Model) {
        current = model
      },
    }
  }

  it('names the download size and disables Start with a reason', async () => {
    stubInstallableModel()
    renderOptions()

    const panel = await screen.findByRole('region', { name: 'Model weights' })
    expect(panel).toHaveTextContent(formatFileSize(sampleWeightsBytes))
    expect(panel).toHaveTextContent(sampleInstallableModel.display_name)

    const button = screen.getByRole('button', { name: 'Start separation' })
    expect(button).toBeDisabled()
    const reasonId = button.getAttribute('aria-describedby')
    expect(reasonId).not.toBeNull()
    const reason = document.getElementById(reasonId ?? '')
    expect(reason?.textContent ?? '').toMatch(/weights/i)
  })

  it('shows real progress once the install starts, and still disables Start', async () => {
    stubInstallableModel()
    renderOptions()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Install model' }),
    )

    const bar = await screen.findByRole('progressbar', {
      name: 'Model download progress',
    })
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(
      screen.getByRole('button', { name: 'Start separation' }),
    ).toBeDisabled()

    // The rest of the configure step keeps working while the download runs.
    const otherMode = fourStemMode?.display_name ?? ''
    await userEvent.click(screen.getByRole('radio', { name: otherMode }))
    expect(screen.getByRole('radio', { name: otherMode })).toBeChecked()
  })

  it('enables Start once the weights are installed', async () => {
    stubInstallableModel(modelInstalling({ state: 'installed' }))
    renderOptions()

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Start separation' }),
      ).toBeEnabled()
    })
    expect(
      await screen.findByRole('region', { name: 'Model weights' }),
    ).toHaveTextContent('Model weights installed')
    expect(
      screen.getByRole('button', { name: 'Start separation' }),
    ).not.toHaveAttribute('aria-describedby')
  })

  it('shows none of it for a model that needs no download', async () => {
    stubFetch({})
    renderOptions()

    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Start separation' }),
      ).toBeEnabled()
    })
    expect(
      screen.queryByRole('region', { name: 'Model weights' }),
    ).not.toBeInTheDocument()
  })

  it('renders a model_weights_missing job failure as an install, not a raw error', async () => {
    let current: Model = modelInstalling({ state: 'installed' })
    const fetchMock = stubFetch({
      model: () => jsonResponse(current),
      job: errorResponse(
        'model_weights_missing',
        "Model 'vocals-hq-001' is catalogued but its weights are not installed.",
        409,
      ),
    })
    renderOptions()

    const button = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await waitFor(() => {
      expect(button).toBeEnabled()
    })

    // The weights vanish between the check and the job.
    current = sampleInstallableModel
    await userEvent.click(button)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'its weights are not installed',
    )
    // Actionable: the panel offers the install, and Start is blocked with a
    // reason until it is done.
    expect(
      await screen.findByRole('button', { name: 'Install model' }),
    ).toBeEnabled()
    await waitFor(() => {
      expect(button).toBeDisabled()
    })
    expect(button).toHaveAttribute('aria-describedby')
    // Exactly one message about it, and the model was re-read after the 409.
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(
      fetchMock.mock.calls.filter(
        ([url]) =>
          (url as string).includes('/models/') &&
          !(url as string).endsWith('/install'),
      ).length,
    ).toBeGreaterThanOrEqual(2)
  })

  it('surfaces a refused install and lets the user try again', async () => {
    let attempts = 0
    stubFetch({
      model: () => jsonResponse(sampleInstallableModel),
      install: () => {
        attempts += 1
        return attempts === 1
          ? errorResponse('model_busy', 'An install is already running.', 409)
          : jsonResponse(modelInstalling({ state: 'downloading' }), 202)
      },
    })
    renderOptions()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Install model' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'An install is already running',
    )

    await userEvent.click(screen.getByRole('button', { name: 'Install model' }))
    expect(
      await screen.findByRole('progressbar', {
        name: 'Model download progress',
      }),
    ).toBeInTheDocument()
    expect(attempts).toBe(2)
  })
})

describe('SeparationOptions after a model_weights_missing job', () => {
  /**
   * The whole flow this feature exists for, in order: the record says the
   * weights are there, `POST /jobs` says otherwise, and the user installs them
   * from the panel that answer produced.
   */
  function stubRefusedThenInstall() {
    let current: Model = modelInstalling({ state: 'installed' })
    let installs = 0
    const fetchMock = stubFetch({
      model: () => jsonResponse(current),
      job: errorResponse(
        'model_weights_missing',
        "Model 'vocals-hq-001' is catalogued but its weights are not installed.",
        409,
      ),
      install: () => {
        installs += 1
        current = modelInstalling({
          state: 'downloading',
          downloaded_bytes: sampleWeightsBytes / 2,
          progress: 0.5,
        })
        return jsonResponse(current, 202)
      },
    })
    return {
      fetchMock,
      installs: () => installs,
      // The weights really are gone; the next read says so.
      vanish() {
        current = sampleInstallableModel
      },
      finish() {
        current = modelInstalling({ state: 'installed' })
      },
    }
  }

  it('shows the install it started, not the job error it followed', async () => {
    const backend = stubRefusedThenInstall()
    renderOptions()

    const start = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await waitFor(() => {
      expect(start).toBeEnabled()
    })
    backend.vanish()
    await userEvent.click(start)

    // The refusal is rendered as the invitation…
    const install = await screen.findByRole('button', { name: 'Install model' })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'its weights are not installed',
    )

    // …and pressing it shows the download, not 870 MB of nothing behind a
    // stale error.
    await userEvent.click(install)
    const bar = await screen.findByRole('progressbar', {
      name: 'Model download progress',
    })
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(
      screen.getByRole('region', { name: 'Model weights' }),
    ).toHaveTextContent(
      `${formatFileSize(sampleWeightsBytes / 2)} of ${formatFileSize(sampleWeightsBytes)}`,
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // No second install beside a running one — that only earns `model_busy`.
    expect(
      screen.queryByRole('button', { name: /install/i }),
    ).not.toBeInTheDocument()
    expect(backend.installs()).toBe(1)

    // Start stays refused, now for the honest reason.
    expect(start).toBeDisabled()
    const reasonId = start.getAttribute('aria-describedby')
    expect(document.getElementById(reasonId ?? '')?.textContent ?? '').toMatch(
      /downloading/i,
    )
  })

  it('re-enables Start once the download the refusal prompted has finished', async () => {
    const backend = stubRefusedThenInstall()
    renderOptions()

    const start = await screen.findByRole('button', {
      name: 'Start separation',
    })
    await waitFor(() => {
      expect(start).toBeEnabled()
    })
    backend.vanish()
    await userEvent.click(start)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Install model' }),
    )
    await screen.findByRole('progressbar', { name: 'Model download progress' })

    // The poll lands the terminal state (its interval is real; this waits on
    // the DOM, not on a duration).
    backend.finish()
    await waitFor(
      () => {
        expect(start).toBeEnabled()
      },
      { timeout: 3000 },
    )
    expect(
      screen.getByRole('region', { name: 'Model weights' }),
    ).toHaveTextContent('Model weights installed')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(start).not.toHaveAttribute('aria-describedby')
  })
})

describe('SeparationOptions before the model has been read', () => {
  it('refuses to start while the installation state is still unknown', async () => {
    const pending = deferred<Response>()
    stubFetch({ model: () => pending.promise })
    renderOptions()

    const start = await screen.findByRole('button', {
      name: 'Start separation',
    })
    // Not knowing is not ready: a click here would produce exactly the
    // `model_weights_missing` refusal the affordance exists to prevent.
    expect(start).toBeDisabled()
    const reasonId = start.getAttribute('aria-describedby')
    expect(document.getElementById(reasonId ?? '')?.textContent ?? '').toMatch(
      /checking/i,
    )

    pending.resolve(jsonResponse(sampleBuiltInModel))
    await waitFor(() => {
      expect(start).toBeEnabled()
    })
  })
})
