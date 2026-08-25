import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModelInstallPanel } from './ModelInstallPanel'
import { DiskSpaceProvider } from '../state/diskSpace'
import { getSystemStorage } from '../api/system'
import type {
  ModelInstallationHandle,
  ModelInstallationStatus,
} from './useModelInstallation'
import type { Model } from '../api/types'
import { formatFileSize } from '../format'
import {
  modelInstalling,
  sampleBuiltInModel,
  sampleInstallableModel,
  sampleWeightsBytes,
} from '../test/fixtures'

/** The size a user is told about, formatted exactly as the app formats it. */
const SIZE = formatFileSize(sampleWeightsBytes)

/** A `useModelInstallation` handle with recording actions. */
function handle(
  overrides: Partial<ModelInstallationHandle> = {},
): ModelInstallationHandle {
  const model = overrides.model ?? null
  const status: ModelInstallationStatus =
    overrides.status ?? (model === null ? 'loading' : 'loaded')
  return {
    modelId: model?.id ?? sampleInstallableModel.id,
    model,
    status,
    error: null,
    installing: false,
    removing: false,
    weightsMissingMessage: null,
    install: vi.fn(),
    remove: vi.fn(),
    refresh: vi.fn(),
    noteWeightsMissing: vi.fn(),
    ...overrides,
  }
}

function renderPanel(installation: ModelInstallationHandle) {
  return render(<ModelInstallPanel installation={installation} />)
}

vi.mock('../api/system')

const getSystemStorageMock = vi.mocked(getSystemStorage)

afterEach(() => {
  vi.clearAllMocks()
})

/** The panel inside a page that knows the machine's free space. */
function renderPanelWithSpace(
  installation: ModelInstallationHandle,
  freeBytes: number | null,
) {
  getSystemStorageMock.mockResolvedValue({
    free_bytes: freeBytes,
    total_bytes: 512 * 1024 ** 3,
  })
  return render(
    <DiskSpaceProvider>
      <ModelInstallPanel installation={installation} />
    </DiskSpaceProvider>,
  )
}

/** The panel's own region, or `null` when it rendered nothing. */
function panel(): HTMLElement | null {
  return screen.queryByRole('region', { name: 'Model weights' })
}

describe('ModelInstallPanel silence', () => {
  it('shows nothing at all for a model that needs no download', () => {
    renderPanel(handle({ model: sampleBuiltInModel }))
    expect(panel()).toBeNull()
    expect(
      screen.queryByRole('button', { name: 'Install model' }),
    ).not.toBeInTheDocument()
  })

  it('shows nothing before the first read answers', () => {
    renderPanel(handle({ status: 'loading' }))
    expect(panel()).toBeNull()
  })
})

describe('ModelInstallPanel with weights missing', () => {
  it('names the download size and offers to install it', () => {
    renderPanel(handle({ model: sampleInstallableModel }))

    const region = panel()
    expect(region).not.toBeNull()
    expect(region).toHaveTextContent(sampleInstallableModel.display_name)
    expect(region).toHaveTextContent(SIZE)
    expect(screen.getByRole('button', { name: 'Install model' })).toBeEnabled()
  })

  it('installs when the button is pressed', async () => {
    const install = vi.fn()
    renderPanel(handle({ model: sampleInstallableModel, install }))

    await userEvent.click(screen.getByRole('button', { name: 'Install model' }))
    expect(install).toHaveBeenCalledTimes(1)
  })

  it('disables the button while the install request is in flight', () => {
    renderPanel(handle({ model: sampleInstallableModel, installing: true }))

    const button = screen.getByRole('button', { name: 'Install model' })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
  })
})

describe('ModelInstallPanel while downloading', () => {
  const downloading: Model = modelInstalling({
    state: 'downloading',
    downloaded_bytes: sampleWeightsBytes / 4,
    progress: 0.25,
  })

  it('reports the backend’s own progress on a progressbar', () => {
    renderPanel(handle({ model: downloading }))

    const bar = screen.getByRole('progressbar', {
      name: 'Model download progress',
    })
    expect(bar).toHaveAttribute('aria-valuenow', '25')
    expect(bar).toHaveAttribute('aria-valuemin', '0')
    expect(bar).toHaveAttribute('aria-valuemax', '100')
    expect(panel()).toHaveTextContent('25%')
    expect(panel()).toHaveTextContent(
      `${formatFileSize(sampleWeightsBytes / 4)} of ${SIZE}`,
    )
  })

  it('is indeterminate before the first progress figure arrives', () => {
    renderPanel(handle({ model: modelInstalling({ state: 'downloading' }) }))

    const bar = screen.getByRole('progressbar', {
      name: 'Model download progress',
    })
    expect(bar).not.toHaveAttribute('aria-valuenow')
  })

  it('offers no second install while one is running', () => {
    renderPanel(handle({ model: downloading }))
    expect(
      screen.queryByRole('button', { name: /install/i }),
    ).not.toBeInTheDocument()
  })
})

describe('ModelInstallPanel outcomes', () => {
  it('confirms an installed download and offers nothing to press', () => {
    renderPanel(handle({ model: modelInstalling({ state: 'installed' }) }))

    expect(panel()).toHaveTextContent('Model weights installed')
    expect(panel()).toHaveTextContent(SIZE)
    expect(
      screen.queryByRole('button', { name: /install/i }),
    ).not.toBeInTheDocument()
  })

  it('shows the backend’s failure message and a retry', async () => {
    const install = vi.fn()
    renderPanel(
      handle({
        install,
        model: modelInstalling({
          state: 'failed',
          error: {
            code: 'checksum_mismatch',
            message:
              'The weights downloaded for vocals-hq-001 did not match the expected digest.',
          },
        }),
      }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'did not match the expected digest',
    )
    // Feature 025 keeps the download URL out of the message; nothing here
    // invents one.
    expect(panel()?.textContent).not.toContain('http')

    await userEvent.click(screen.getByRole('button', { name: 'Retry install' }))
    expect(install).toHaveBeenCalledTimes(1)
  })

  it('shows a refused install request beside the model it is about', () => {
    renderPanel(
      handle({
        model: sampleInstallableModel,
        status: 'error',
        error: {
          code: 'model_busy',
          message: 'An install is already running for vocals-hq-001.',
        },
      }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'An install is already running',
    )
    expect(screen.getByRole('button', { name: 'Install model' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
  })

  it('offers a retry when the model could not be read at all', async () => {
    const refresh = vi.fn()
    renderPanel(
      handle({
        status: 'error',
        refresh,
        error: { code: 'service_unavailable', message: 'Backend is down.' },
      }),
    )

    expect(panel()).toHaveTextContent(
      'Could not check whether the model weights are installed',
    )
    // Nothing is known about this model, so nothing is claimed about it.
    expect(
      screen.queryByRole('button', { name: /install/i }),
    ).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})

describe('ModelInstallPanel and a model_weights_missing job', () => {
  const REFUSED =
    "Model 'vocals-hq-001' is catalogued but its weights are not installed."

  it('renders the backend message as the install invitation', async () => {
    const install = vi.fn()
    renderPanel(
      handle({
        model: modelInstalling({ state: 'installed' }),
        install,
        weightsMissingMessage: REFUSED,
      }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'its weights are not installed',
    )
    // The record claiming `installed` is the stale one the refusal is about.
    expect(panel()).not.toHaveTextContent('Model weights installed')
    await userEvent.click(screen.getByRole('button', { name: 'Install model' }))
    expect(install).toHaveBeenCalledTimes(1)
  })

  it('speaks up even for a model that claims it needs no download', () => {
    renderPanel(
      handle({
        model: sampleBuiltInModel,
        weightsMissingMessage: 'The weights are gone.',
      }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent('The weights are gone.')
  })

  it('still names the download size, so the invitation is priced', () => {
    renderPanel(
      handle({ model: sampleInstallableModel, weightsMissingMessage: REFUSED }),
    )

    expect(panel()).toHaveTextContent(SIZE)
    expect(screen.getByRole('button', { name: 'Install model' })).toBeEnabled()
  })

  it('shows the download it started, not the job error it followed', () => {
    // The flow this feature exists for: Start → refused → Install. The live
    // state is newer than the refusal, so it wins — otherwise the user watches
    // 870 MB arrive behind a stale alert, with no progress at all.
    renderPanel(
      handle({
        model: modelInstalling({
          state: 'downloading',
          downloaded_bytes: sampleWeightsBytes / 2,
          progress: 0.5,
        }),
        weightsMissingMessage: REFUSED,
      }),
    )

    expect(
      screen.getByRole('progressbar', { name: 'Model download progress' }),
    ).toHaveAttribute('aria-valuenow', '50')
    expect(panel()).toHaveTextContent(
      `${formatFileSize(sampleWeightsBytes / 2)} of ${SIZE}`,
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('offers no second install beside a download a stale error is about', () => {
    renderPanel(
      handle({
        model: modelInstalling({ state: 'downloading', progress: 0.5 }),
        weightsMissingMessage: REFUSED,
      }),
    )

    // A second POST /install would earn `model_busy` for a transfer that is
    // going perfectly well.
    expect(
      screen.queryByRole('button', { name: /install/i }),
    ).not.toBeInTheDocument()
  })
})

describe('ModelInstallPanel recovering from a failed read', () => {
  it('offers a retry when a read fails mid-download', async () => {
    const refresh = vi.fn()
    renderPanel(
      handle({
        model: modelInstalling({ state: 'downloading', progress: 0.4 }),
        status: 'error',
        error: { code: 'service_unavailable', message: 'Backend is down.' },
        refresh,
      }),
    )

    // The bar is frozen at the last real figure and the poll has stopped, so
    // the one control that can restart it has to be here — this is otherwise a
    // dead end escapable only by switching tiers or reloading the page.
    expect(
      screen.getByRole('progressbar', { name: 'Model download progress' }),
    ).toHaveAttribute('aria-valuenow', '40')
    expect(screen.getByRole('alert')).toHaveTextContent('Backend is down.')

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})

describe('ModelInstallPanel and the machine’s free space', () => {
  it('prices the download against the room the backend reports', async () => {
    renderPanelWithSpace(
      handle({ model: sampleInstallableModel }),
      4 * 1024 ** 3,
    )

    const region = panel()
    expect(region).not.toBeNull()
    await waitFor(() => {
      expect(region).toHaveTextContent('4 GB is free there.')
    })
    expect(region).toHaveTextContent(`${SIZE} will be written`)
    expect(region).not.toHaveTextContent(/cannot check/i)
    // Read where the install is offered, once — not on a timer.
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
  })

  it('warns that a download will not fit, and still offers to install it', async () => {
    renderPanelWithSpace(
      handle({ model: sampleInstallableModel }),
      128 * 1024 * 1024,
    )

    await waitFor(() => {
      expect(panel()).toHaveTextContent(/will not fit/i)
    })
    // Warn, never refuse: the reading is one moment old on a disk that moves,
    // and a false refusal here is the worse failure (see the feature doc).
    expect(screen.getByRole('button', { name: 'Install model' })).toBeEnabled()
  })

  it('keeps the honest wording when the host cannot answer', async () => {
    renderPanelWithSpace(handle({ model: sampleInstallableModel }), null)

    await waitFor(() => {
      expect(panel()).toHaveTextContent(/cannot check/i)
    })
    expect(panel()).toHaveTextContent(`${SIZE} will be written`)
    expect(screen.getByRole('button', { name: 'Install model' })).toBeEnabled()
  })

  it('asks nothing while a download is running, because none is offered', async () => {
    renderPanelWithSpace(
      handle({
        model: modelInstalling({ state: 'downloading', progress: 0.5 }),
      }),
      4 * 1024 ** 3,
    )

    expect(panel()).toHaveTextContent(/Downloading the model weights/i)
    expect(getSystemStorageMock).not.toHaveBeenCalled()
  })
})
