import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModelCard } from './ModelCard'
import {
  modelInstalling,
  modelLicensed,
  sampleBuiltInModel,
  sampleInstallableModel,
  samplePermissiveLicensing,
  sampleRestrictiveLicensing,
  sampleWeightsBytes,
} from '../test/fixtures'
import type { Model } from '../api/types'

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

/** Every request the card made, in order. */
interface Recorded {
  readonly method: string
  readonly url: string
}

/**
 * Stub `fetch` for one model: reads answer with whatever `state.model` holds,
 * and `install` / `weights` replace it, exactly as the backend would.
 */
function stubModel(options: {
  model: Model
  install?: (current: Model) => Response
  remove?: (current: Model) => Response
}): { requests: Recorded[]; set: (model: Model) => void } {
  let current = options.model
  const requests: Recorded[] = []
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    requests.push({ method, url })
    if (method === 'DELETE') {
      const response = options.remove?.(current)
      if (response !== undefined) {
        return Promise.resolve(response)
      }
      current = modelInstalling({ state: 'available' }, current)
      return Promise.resolve(jsonResponse(current))
    }
    if (url.endsWith('/install')) {
      const response = options.install?.(current)
      if (response !== undefined) {
        return Promise.resolve(response)
      }
      current = modelInstalling(
        {
          state: 'downloading',
          progress: 0.25,
          downloaded_bytes: sampleWeightsBytes / 4,
        },
        current,
      )
      return Promise.resolve(jsonResponse(current, 202))
    }
    return Promise.resolve(jsonResponse(current))
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    requests,
    set: (model: Model) => {
      current = model
    },
  }
}

/** Render one card and return its region. */
function renderCard(model: Model): HTMLElement {
  render(<ModelCard model={model} />)
  return screen.getByRole('article', { name: model.display_name })
}

/** The requests of one HTTP method. */
function methodCalls(requests: Recorded[], method: string): Recorded[] {
  return requests.filter((request) => request.method === method)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ModelCard inventory', () => {
  it('names the state, the size, the requirements and the terms', async () => {
    stubModel({ model: modelLicensed(samplePermissiveLicensing) })
    const card = renderCard(modelLicensed(samplePermissiveLicensing))

    expect(within(card).getByText('Not installed')).toBeInTheDocument()
    expect(card).toHaveTextContent('870 MB')
    expect(card).toHaveTextContent('Recommended VRAM: 8 GB')
    expect(card).toHaveTextContent('vocals, instrumental')
    expect(card).toHaveTextContent('mel_band_roformer 1.0')
    expect(card).toHaveTextContent('44.1 kHz')
    expect(card).toHaveTextContent('cuda, cpu')
    expect(
      within(card).getByRole('region', {
        name: `Licensing for ${sampleInstallableModel.display_name}`,
      }),
    ).toBeInTheDocument()

    // Let the hook's own first read land before the test ends.
    await waitFor(() => {
      expect(within(card).getByText('Not installed')).toBeInTheDocument()
    })
  })

  it('says a requirement is advisory rather than implying a check', async () => {
    stubModel({ model: sampleInstallableModel })
    const card = renderCard(sampleInstallableModel)
    expect(card).toHaveTextContent(/advisory only/i)
    await waitFor(() => {
      expect(within(card).getByText('Not installed')).toBeInTheDocument()
    })
  })

  it('says so when no hardware requirements are declared', async () => {
    const bare: Model = { ...sampleInstallableModel, requirements: undefined }
    stubModel({ model: bare })
    const card = renderCard(bare)
    expect(card).toHaveTextContent(/no hardware requirements are declared/i)
    await waitFor(() => {
      expect(within(card).getByText('Not installed')).toBeInTheDocument()
    })
  })

  it('labels a development fixture as one', async () => {
    // Feature 032 left this open: a server that opts fixtures back in should
    // say what they are, and this is the screen that can.
    stubModel({ model: sampleBuiltInModel })
    const card = renderCard(sampleBuiltInModel)

    expect(card).toHaveTextContent(/development fixture/i)
    expect(card).toHaveTextContent(/does not perform real separation/i)
    await waitFor(() => {
      expect(within(card).getByText('Built in')).toBeInTheDocument()
    })
  })

  it('offers nothing to install or remove for a built-in model', async () => {
    stubModel({ model: sampleBuiltInModel })
    const card = renderCard(sampleBuiltInModel)

    await waitFor(() => {
      expect(within(card).getByText('Built in')).toBeInTheDocument()
    })
    expect(within(card).queryByRole('button')).not.toBeInTheDocument()
    expect(card).toHaveTextContent(/nothing to download and nothing to remove/i)
  })
})

describe('ModelCard installing', () => {
  it('prices the download and admits the disk check cannot be made', async () => {
    stubModel({ model: sampleInstallableModel })
    const card = renderCard(sampleInstallableModel)

    const notice = card.querySelector('.disk-cost')
    expect(notice, 'the card prices the download').not.toBeNull()
    expect(notice).toHaveTextContent('870 MB will be written')
    expect(notice).toHaveTextContent(/cannot check/i)
    await waitFor(() => {
      expect(
        within(card).getByRole('button', { name: 'Install' }),
      ).toBeEnabled()
    })
  })

  it('shows the licence before the install, not after it', async () => {
    stubModel({ model: modelLicensed(sampleRestrictiveLicensing) })
    const card = renderCard(modelLicensed(sampleRestrictiveLicensing))

    expect(within(card).getByText('Restricted use')).toBeInTheDocument()
    expect(card).toHaveTextContent(/research and personal use only/i)
    await waitFor(() => {
      expect(
        within(card).getByRole('button', { name: 'Install' }),
      ).toBeEnabled()
    })
  })

  it('POSTs the install and shows the backend’s own progress', async () => {
    const { requests } = stubModel({ model: sampleInstallableModel })
    const card = renderCard(sampleInstallableModel)

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Install' }),
    )

    const bar = await within(card).findByRole('progressbar', {
      name: 'Model download progress',
    })
    expect(bar).toHaveAttribute('aria-valuenow', '25')
    expect(card).toHaveTextContent('Downloading — 25%')
    expect(card).toHaveTextContent('217.5 MB of 870 MB')
    expect(
      requests.filter((request) => request.url.endsWith('/install')),
    ).toHaveLength(1)
  })

  it('shows a failed install’s own message and offers a retry', async () => {
    stubModel({
      model: modelInstalling({
        state: 'failed',
        error: {
          code: 'checksum_mismatch',
          message: 'The download did not match its checksum.',
        },
      }),
    })
    const card = renderCard(
      modelInstalling({
        state: 'failed',
        error: {
          code: 'checksum_mismatch',
          message: 'The download did not match its checksum.',
        },
      }),
    )

    expect(within(card).getByRole('alert')).toHaveTextContent(
      'The download did not match its checksum.',
    )
    expect(
      await within(card).findByRole('button', { name: 'Retry install' }),
    ).toBeEnabled()
  })

  it('surfaces a refused install with the control that can clear it', async () => {
    stubModel({
      model: sampleInstallableModel,
      install: () =>
        errorResponse('model_busy', 'An install is already running.', 409),
    })
    const card = renderCard(sampleInstallableModel)

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Install' }),
    )

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'An install is already running.',
    )
    expect(
      within(card).getByRole('button', { name: 'Try again' }),
    ).toBeEnabled()
  })
})

describe('ModelCard cancelling versus removing', () => {
  it('offers Cancel download — never Remove — while a download runs', async () => {
    // One route, two intents. A single ambiguous button would be the wrong
    // reading of `DELETE .../weights` in both directions.
    stubModel({
      model: modelInstalling({ state: 'downloading', progress: 0.5 }),
    })
    const card = renderCard(
      modelInstalling({ state: 'downloading', progress: 0.5 }),
    )

    expect(
      await within(card).findByRole('button', { name: 'Cancel download' }),
    ).toBeEnabled()
    expect(
      within(card).queryByRole('button', { name: 'Remove weights' }),
    ).not.toBeInTheDocument()
    expect(card).toHaveTextContent(/deletes the partly downloaded file/i)
    expect(card).toHaveTextContent(/nothing is kept/i)
  })

  it('cancels straight away: escaping a stuck download must not need a dialog', async () => {
    const { requests } = stubModel({
      model: modelInstalling({ state: 'downloading', progress: 0.5 }),
    })
    const card = renderCard(
      modelInstalling({ state: 'downloading', progress: 0.5 }),
    )

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Cancel download' }),
    )

    await waitFor(() => {
      expect(methodCalls(requests, 'DELETE')).toHaveLength(1)
    })
    expect(methodCalls(requests, 'DELETE')[0]?.url).toBe(
      `/api/v1/models/${sampleInstallableModel.id}/weights`,
    )
    expect(
      await within(card).findByRole('button', { name: 'Install' }),
    ).toBeEnabled()
  })

  it('offers Remove weights — never Cancel — once they are installed', async () => {
    stubModel({ model: modelInstalling({ state: 'installed' }) })
    const card = renderCard(modelInstalling({ state: 'installed' }))

    expect(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    ).toBeEnabled()
    expect(
      within(card).queryByRole('button', { name: 'Cancel download' }),
    ).not.toBeInTheDocument()
    expect(card).toHaveTextContent('Installed — 870 MB on disk')
  })

  it('asks before deleting a download the user waited for', async () => {
    const { requests } = stubModel({
      model: modelInstalling({ state: 'installed' }),
    })
    const card = renderCard(modelInstalling({ state: 'installed' }))

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    )

    const confirm = within(card).getByRole('group', { name: 'Confirm removal' })
    expect(confirm).toHaveTextContent('870 MB')
    expect(confirm).toHaveTextContent(
      /installing again downloads the whole artifact/i,
    )
    expect(methodCalls(requests, 'DELETE')).toHaveLength(0)
  })

  it('keeps the weights when the confirmation is declined', async () => {
    const { requests } = stubModel({
      model: modelInstalling({ state: 'installed' }),
    })
    const card = renderCard(modelInstalling({ state: 'installed' }))

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    )
    await userEvent.click(
      within(card).getByRole('button', { name: 'Keep them' }),
    )

    expect(methodCalls(requests, 'DELETE')).toHaveLength(0)
    expect(
      within(card).getByRole('button', { name: 'Remove weights' }),
    ).toBeEnabled()
  })

  it('DELETEs the weights once the confirmation is accepted', async () => {
    const { requests } = stubModel({
      model: modelInstalling({ state: 'installed' }),
    })
    const card = renderCard(modelInstalling({ state: 'installed' }))

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    )
    await userEvent.click(
      within(card).getByRole('button', { name: 'Delete the weights' }),
    )

    await waitFor(() => {
      expect(methodCalls(requests, 'DELETE')).toHaveLength(1)
    })
    expect(
      await within(card).findByRole('button', { name: 'Install' }),
    ).toBeEnabled()
    expect(card).toHaveTextContent('Not installed')
  })

  it('surfaces a refused removal without losing the model', async () => {
    stubModel({
      model: modelInstalling({ state: 'installed' }),
      remove: () =>
        errorResponse(
          'model_not_downloadable',
          'This model has no weights to remove.',
          409,
        ),
    })
    const card = renderCard(modelInstalling({ state: 'installed' }))

    await userEvent.click(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    )
    await userEvent.click(
      within(card).getByRole('button', { name: 'Delete the weights' }),
    )

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'This model has no weights to remove.',
    )
    expect(card).toHaveTextContent('Installed — 870 MB on disk')
  })
})
