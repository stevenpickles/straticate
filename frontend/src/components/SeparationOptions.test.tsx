import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SeparationOptions } from './SeparationOptions'
import {
  AppStateProvider,
  initialConfigureState,
  useAppState,
  type AppState,
} from '../state/appState'
import { JobStateProvider, useJobState } from '../state/jobState'
import { ModelRevisionProvider } from '../state/modelRevision'
import {
  modelInstalling,
  modelLicensed,
  samplePermissiveLicensing,
  sampleRestrictiveLicensing,
  sampleSilentWeightsLicensing,
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
  /**
   * `GET /models`, which feature 037 reads once so every quality tier can be
   * priced where it is chosen. It defaults to an empty catalog: a tier the
   * catalog says nothing about is simply not annotated, which is what keeps
   * every test that is *not* about pricing reading as it did before.
   */
  catalog?: Model[] | Response | Promise<Response>
}): FetchMock {
  const modes = options.modes ?? sampleSeparationModes
  const catalog = options.catalog ?? []
  // `GET /models/{id}` answers with the catalog's own record for that model
  // where there is one, exactly as the backend does — both routes are served
  // from one `ModelInstaller`, so a test whose two answers disagreed would be
  // asserting against a server that cannot exist. Falling back to a model that
  // needs no download is what keeps every test that is *not* about
  // installation reading as it did before feature 035.
  const model =
    options.model ??
    ((modelId: string) => {
      const held = Array.isArray(catalog)
        ? catalog.find((candidate) => candidate.id === modelId)
        : undefined
      return jsonResponse(
        held ?? {
          ...sampleBuiltInModel,
          id: modelId,
          display_name: modelId,
        },
      )
    })
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
    if (url.endsWith('/models')) {
      return Promise.resolve(
        Array.isArray(catalog) ? jsonResponse(catalog) : catalog,
      )
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

/** How many times `GET /models` (the collection) has been read. */
function catalogReads(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(([url]) =>
    (url as string).endsWith('/models'),
  ).length
}

/** How many times a single model has been read. */
function modelReads(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(
    ([url, init]) =>
      (url as string).includes('/models/') &&
      !(url as string).endsWith('/install') &&
      (init as RequestInit | undefined)?.method === undefined,
  ).length
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
      if (url.endsWith('/models')) {
        return Promise.resolve(jsonResponse([]))
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

describe('SeparationOptions pricing every tier', () => {
  const [fastTier, hqTier] = twoStemMode?.quality_options ?? []

  /** The catalog record backing a tier, with an `installation` block. */
  function catalogEntry(
    modelId: string,
    installation: Partial<Model['installation']>,
  ): Model {
    return modelInstalling(installation as never, {
      ...sampleInstallableModel,
      id: modelId,
      display_name: modelId,
    })
  }

  /** The sentence a tier's radio points at with `aria-describedby`. */
  function tierNote(name: string): string | null {
    const radio = screen.getByRole('radio', { name })
    const id = radio.getAttribute('aria-describedby')
    return id === null
      ? null
      : (document.getElementById(id)?.textContent ?? null)
  }

  it('prices every tier, not only the one that happens to be selected', async () => {
    // Feature 035's panel describes the *selection*. A mode with two
    // uninstalled tiers would otherwise have to be clicked through to find out
    // what each of them costs.
    stubFetch({
      catalog: [
        catalogEntry(fastTier?.model_id ?? '', {
          state: 'available',
          total_bytes: sampleWeightsBytes,
        }),
        catalogEntry(hqTier?.model_id ?? '', { state: 'installed' }),
      ],
    })
    renderOptions()
    await screen.findByRole('radio', { name: fastTier?.display_name })

    await waitFor(() => {
      expect(tierNote(fastTier?.display_name ?? '')).toBe(
        `Needs a ${formatFileSize(sampleWeightsBytes)} download`,
      )
    })
    expect(tierNote(hqTier?.display_name ?? '')).toBe('Installed')
  })

  it('shows a tier whose weights are missing rather than hiding it', async () => {
    // The question open since feature 010, answered here: **no**. A hidden
    // tier makes the product silently differ from machine to machine, and on a
    // default server it would empty the configure step altogether. A tier a
    // user can see, price and install is strictly better — and since feature
    // 035 it cannot surprise anyone, because Start is disabled with a reason
    // until the weights are there.
    stubFetch({
      catalog: [
        catalogEntry(fastTier?.model_id ?? '', {
          state: 'available',
          total_bytes: sampleWeightsBytes,
        }),
        catalogEntry(hqTier?.model_id ?? '', {
          state: 'available',
          total_bytes: sampleWeightsBytes,
        }),
      ],
      model: (modelId) =>
        jsonResponse(
          modelInstalling(
            { state: 'available' },
            { ...sampleInstallableModel, id: modelId, display_name: modelId },
          ),
        ),
    })
    renderOptions()

    for (const option of twoStemMode?.quality_options ?? []) {
      expect(
        await screen.findByRole('radio', { name: option.display_name }),
      ).toBeInTheDocument()
    }
    await waitFor(() => {
      expect(tierNote(hqTier?.display_name ?? '')).toMatch(
        /needs a .* download/i,
      )
    })
    expect(
      await screen.findByRole('button', { name: 'Start separation' }),
    ).toBeDisabled()
  })

  it('names a downloading tier and a failed one for what they are', async () => {
    stubFetch({
      catalog: [
        catalogEntry(fastTier?.model_id ?? '', {
          state: 'downloading',
          progress: 0.5,
        }),
        catalogEntry(hqTier?.model_id ?? '', { state: 'failed' }),
      ],
    })
    renderOptions()
    await screen.findByRole('radio', { name: fastTier?.display_name })

    await waitFor(() => {
      expect(tierNote(fastTier?.display_name ?? '')).toBe(
        'Downloading its weights…',
      )
    })
    expect(tierNote(hqTier?.display_name ?? '')).toBe('Its last install failed')
  })

  it('says nothing about a tier the catalog read could not describe', async () => {
    // The annotation is an enrichment, never a gate: a failed catalog read
    // leaves the tiers exactly as feature 011 rendered them.
    stubFetch({ catalog: errorResponse('internal_error', 'No catalog.', 500) })
    renderOptions()

    const radio = await screen.findByRole('radio', {
      name: fastTier?.display_name,
    })
    expect(radio).not.toHaveAttribute('aria-describedby')
    expect(radio).toBeEnabled()
  })

  it('says nothing about a tier whose model needs no download', async () => {
    stubFetch({
      catalog: [
        { ...sampleBuiltInModel, id: fastTier?.model_id ?? '' },
        catalogEntry(hqTier?.model_id ?? '', { state: 'installed' }),
      ],
    })
    renderOptions()
    await screen.findByRole('radio', { name: fastTier?.display_name })

    await waitFor(() => {
      expect(tierNote(hqTier?.display_name ?? '')).toBe('Installed')
    })
    expect(tierNote(fastTier?.display_name ?? '')).toBeNull()
  })
})

describe('SeparationOptions licensing at the point of choice', () => {
  const [fastTier] = twoStemMode?.quality_options ?? []

  it('renders the selected model’s terms and its required attribution', async () => {
    // A credit nobody sees is not a credit given, and the moment before an
    // install is the only one at which terms can still change the decision.
    const licensed = modelLicensed(sampleRestrictiveLicensing, {
      ...sampleInstallableModel,
      id: fastTier?.model_id ?? '',
      display_name: 'Vocals — Fast',
    })
    stubFetch({ catalog: [licensed], model: () => jsonResponse(licensed) })
    renderOptions()

    const licence = await screen.findByRole('region', {
      name: 'Licensing for Vocals — Fast',
    })
    expect(within(licence).getByText('Restricted use')).toBeInTheDocument()
    expect(licence).toHaveTextContent(/research and personal use only/i)
    expect(licence).toHaveTextContent(
      String(sampleRestrictiveLicensing.attribution),
    )
    expect(licence).toHaveTextContent('Not permitted')
  })

  it('shows the terms while the weights are still uninstalled', async () => {
    const licensed = modelLicensed(sampleSilentWeightsLicensing, {
      ...sampleInstallableModel,
      id: fastTier?.model_id ?? '',
      display_name: 'Vocals — Fast',
    })
    stubFetch({ catalog: [licensed], model: () => jsonResponse(licensed) })
    renderOptions()

    const licence = await screen.findByRole('region', {
      name: 'Licensing for Vocals — Fast',
    })
    expect(within(licence).getByText('Terms not stated')).toBeInTheDocument()
    expect(licence).toHaveTextContent(/does not cover the weights/i)
    // …and the install it is about has not happened.
    expect(
      await screen.findByRole('button', { name: 'Install model' }),
    ).toBeEnabled()
  })

  it('follows the selection when the user switches tier', async () => {
    const forId = (modelId: string): Model =>
      modelLicensed(
        modelId === fastTier?.model_id
          ? sampleRestrictiveLicensing
          : samplePermissiveLicensing,
        { ...sampleBuiltInModel, id: modelId, display_name: modelId },
      )
    stubFetch({
      catalog: (twoStemMode?.quality_options ?? []).map((option) =>
        forId(option.model_id),
      ),
      model: (modelId) => jsonResponse(forId(modelId)),
    })
    renderOptions()

    await screen.findByRole('region', {
      name: `Licensing for ${fastTier?.model_id ?? ''}`,
    })

    const other = twoStemMode?.quality_options[1]
    await userEvent.click(
      screen.getByRole('radio', { name: other?.display_name }),
    )
    expect(
      await screen.findByRole('region', {
        name: `Licensing for ${other?.model_id ?? ''}`,
      }),
    ).toBeInTheDocument()
  })
})

describe('SeparationOptions keeping a tier’s price honest', () => {
  const [fastTier, hqTier] = twoStemMode?.quality_options ?? []
  const fastId = fastTier?.model_id ?? ''
  const hqId = hqTier?.model_id ?? ''

  /** A catalog record for `modelId` in a given installation state. */
  function entry(modelId: string, state: 'available' | 'installed'): Model {
    return modelInstalling(
      { state, total_bytes: sampleWeightsBytes },
      { ...sampleInstallableModel, id: modelId, display_name: modelId },
    )
  }

  /** The sentence a tier's radio points at with `aria-describedby`. */
  function tierNote(name: string): string | null {
    const radio = screen.getByRole('radio', { name })
    const id = radio.getAttribute('aria-describedby')
    return id === null
      ? null
      : (document.getElementById(id)?.textContent ?? null)
  }

  it('stops the radio contradicting the panel the moment an install finishes', async () => {
    // Regression. The catalog is read once; the selected tier's model is read
    // again on every poll. Priced from the frozen catalog, the radio went on
    // saying "Needs a 870 MB download" directly above a panel reporting
    // "Model weights installed" — and because that sentence is the radio's
    // `aria-describedby` target, a screen reader announced the tier as needing
    // a download that had just completed.
    let fastState: 'available' | 'installed' = 'available'
    stubFetch({
      catalog: [entry(fastId, 'available'), entry(hqId, 'installed')],
      model: (modelId) =>
        jsonResponse(
          entry(modelId, modelId === fastId ? fastState : 'installed'),
        ),
      install: (modelId) => {
        fastState = 'installed'
        return jsonResponse(entry(modelId, 'installed'), 202)
      },
    })
    renderOptions()

    await waitFor(() => {
      expect(tierNote(fastTier?.display_name ?? '')).toBe(
        `Needs a ${formatFileSize(sampleWeightsBytes)} download`,
      )
    })

    await userEvent.click(
      await screen.findByRole('button', { name: 'Install model' }),
    )
    await screen.findByText(/Model weights installed/)

    expect(
      tierNote(fastTier?.display_name ?? ''),
      'the live record outranks the catalog’s copy of the same model',
    ).toBe('Installed')
  })

  it('keeps a tier honest after the user selects a different one', async () => {
    // The live record covers the *selected* tier. Once the user moves on it is
    // no longer live, so the answer that installed the weights has to be
    // written into the catalog behind it — otherwise the price springs back to
    // "Needs a 870 MB download" for a model that is on disk.
    let fastState: 'available' | 'installed' = 'available'
    stubFetch({
      catalog: [entry(fastId, 'available'), entry(hqId, 'installed')],
      model: (modelId) =>
        jsonResponse(
          entry(modelId, modelId === fastId ? fastState : 'installed'),
        ),
      install: (modelId) => {
        fastState = 'installed'
        return jsonResponse(entry(modelId, 'installed'), 202)
      },
    })
    renderOptions()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Install model' }),
    )
    await screen.findByText(/Model weights installed/)

    await userEvent.click(
      screen.getByRole('radio', { name: hqTier?.display_name }),
    )
    await waitFor(() => {
      expect(
        screen.getByRole('radio', { name: hqTier?.display_name }),
      ).toBeChecked()
    })

    expect(tierNote(fastTier?.display_name ?? '')).toBe('Installed')
  })

  it('re-reads once when another view may have changed what is installed', async () => {
    // The workflow is only *hidden* while the model library is open
    // (`App.tsx`), never unmounted, so it does not re-read on the way back the
    // way a remounted view would. A bumped revision is that signal — and it is
    // a known event, not a timer: nothing re-reads until it changes.
    const fetchMock = stubFetch({
      catalog: [entry(fastId, 'available'), entry(hqId, 'installed')],
      model: (modelId) => jsonResponse(entry(modelId, 'available')),
    })
    const initialState: AppState = {
      phase: 'configure',
      upload: { status: 'uploaded', file: sampleAudioFile },
      configure: initialConfigureState,
    }
    const view = (revision: number) => (
      <AppStateProvider initialState={initialState}>
        <JobStateProvider>
          <ModelRevisionProvider revision={revision}>
            <SeparationOptions />
          </ModelRevisionProvider>
        </JobStateProvider>
      </AppStateProvider>
    )
    const { rerender } = render(view(0))
    await screen.findByRole('radio', { name: fastTier?.display_name })
    await waitFor(() => {
      expect(catalogReads(fetchMock)).toBe(1)
    })
    const modelReadsBefore = modelReads(fetchMock)

    // A re-render that does *not* change the revision changes nothing.
    rerender(view(0))
    expect(catalogReads(fetchMock)).toBe(1)

    rerender(view(1))
    await waitFor(() => {
      expect(catalogReads(fetchMock)).toBe(2)
    })
    expect(modelReads(fetchMock)).toBeGreaterThan(modelReadsBefore)
  })
})
