import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModelLibrary } from './ModelLibrary'
import {
  modelInstalling,
  modelLicensed,
  sampleBuiltInModel,
  sampleInstallableModel,
  samplePermissiveLicensing,
  sampleSilentWeightsLicensing,
} from '../test/fixtures'
import type { Model } from '../api/types'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Stub `fetch` so `GET /models` answers with `catalog` and each model's own
 * `GET /models/{id}` answers with the same record.
 */
function stubCatalog(
  catalog: Model[] | Response,
  onCollection?: () => void,
): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((url: string) => {
    if (url.endsWith('/models')) {
      onCollection?.()
      return Promise.resolve(
        Array.isArray(catalog) ? jsonResponse(catalog) : catalog,
      )
    }
    const id = decodeURIComponent(url.split('/models/')[1]?.split('/')[0] ?? '')
    const model = Array.isArray(catalog)
      ? catalog.find((candidate) => candidate.id === id)
      : undefined
    return Promise.resolve(
      model === undefined
        ? jsonResponse(
            { error: { code: 'model_not_found', message: 'No such model.' } },
            404,
          )
        : jsonResponse(model),
    )
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderLibrary(onClose = vi.fn()) {
  render(<ModelLibrary onClose={onClose} />)
  return {
    onClose,
    region: screen.getByRole('region', { name: 'Model library' }),
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ModelLibrary', () => {
  it('says the weights are downloads before it lists anything', async () => {
    stubCatalog([])
    const { region } = renderLibrary()
    expect(region).toHaveTextContent(/straticate ships no model weights/i)
    // Let the catalog read this render started settle inside `act`, rather
    // than after the test has finished with it.
    await screen.findByText(/offers no models at all/i)
  })

  it('lists every catalogued model, in catalog order', async () => {
    const catalog = [
      modelLicensed(samplePermissiveLicensing),
      sampleBuiltInModel,
    ]
    stubCatalog(catalog)
    renderLibrary()

    await waitFor(() => {
      expect(screen.getAllByRole('article')).toHaveLength(2)
    })
    expect(
      screen
        .getAllByRole('article')
        .map((card) => card.getAttribute('aria-label')),
    ).toEqual([
      sampleInstallableModel.display_name,
      sampleBuiltInModel.display_name,
    ])
  })

  it('counts what is catalogued and what is installed', async () => {
    stubCatalog([modelInstalling({ state: 'installed' }), sampleBuiltInModel])
    renderLibrary()

    expect(await screen.findByRole('status')).toHaveTextContent(
      '2 models catalogued · 2 installed',
    )
  })

  it('reports one model as a model, not as "1 models"', async () => {
    stubCatalog([sampleInstallableModel])
    renderLibrary()

    expect(await screen.findByRole('status')).toHaveTextContent(
      '1 model catalogued · 0 installed',
    )
  })

  it('shows each model’s terms without the user opening anything', async () => {
    stubCatalog([
      modelLicensed(samplePermissiveLicensing),
      modelLicensed(sampleSilentWeightsLicensing, {
        ...sampleInstallableModel,
        id: 'vocals-fast-001',
        display_name: 'Vocals — Fast',
      }),
    ])
    renderLibrary()

    const permissive = await screen.findByRole('article', {
      name: sampleInstallableModel.display_name,
    })
    expect(within(permissive).getByText('Terms declared')).toBeInTheDocument()
    expect(permissive).toHaveTextContent(/Kimberley Jensen/)

    const silent = screen.getByRole('article', { name: 'Vocals — Fast' })
    expect(within(silent).getByText('Terms not stated')).toBeInTheDocument()
    expect(silent).toHaveTextContent(/does not cover the weights/i)
  })

  it('reads the catalog once, not once per rendered card', async () => {
    let collectionReads = 0
    stubCatalog([sampleInstallableModel, sampleBuiltInModel], () => {
      collectionReads += 1
    })
    renderLibrary()

    await waitFor(() => {
      expect(screen.getAllByRole('article')).toHaveLength(2)
    })
    expect(collectionReads).toBe(1)
  })

  it('offers a retry when the catalog cannot be read', async () => {
    const fetchMock = stubCatalog(
      jsonResponse(
        { error: { code: 'internal_error', message: 'Catalog unreadable.' } },
        500,
      ),
    )
    renderLibrary()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Catalog unreadable.',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })
  })

  it('says so when the server offers no models at all', async () => {
    stubCatalog([])
    const { region } = renderLibrary()

    await waitFor(() => {
      expect(region).toHaveTextContent(/offers no models at all/i)
    })
    expect(screen.queryAllByRole('article')).toHaveLength(0)
  })

  it('closes back to the workflow', async () => {
    stubCatalog([])
    const { onClose } = renderLibrary()

    await userEvent.click(
      screen.getByRole('button', { name: 'Back to workflow' }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('ModelLibrary summary after an install', () => {
  /**
   * Stub `fetch` for a mutable catalog: `GET /models` and `GET /models/{id}`
   * both answer from `models`, and `POST .../install` replaces that model's
   * record, exactly as the backend's one `ModelInstaller` does.
   */
  function stubMutableCatalog(initial: Model[]): { deletes: string[] } {
    let models = initial
    const deletes: string[] = []
    const replace = (model: Model) => {
      models = models.map((candidate) =>
        candidate.id === model.id ? model : candidate,
      )
    }
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith('/models')) {
        return Promise.resolve(jsonResponse(models))
      }
      const id = decodeURIComponent(
        url.split('/models/')[1]?.split('/')[0] ?? '',
      )
      const held = models.find((candidate) => candidate.id === id)
      if (held === undefined) {
        return Promise.resolve(
          jsonResponse(
            { error: { code: 'model_not_found', message: 'No such model.' } },
            404,
          ),
        )
      }
      if (init?.method === 'DELETE') {
        deletes.push(url)
        const removed = modelInstalling({ state: 'available' }, held)
        replace(removed)
        return Promise.resolve(jsonResponse(removed))
      }
      if (url.endsWith('/install')) {
        const installed = modelInstalling({ state: 'installed' }, held)
        replace(installed)
        return Promise.resolve(jsonResponse(installed, 202))
      }
      return Promise.resolve(jsonResponse(held))
    })
    vi.stubGlobal('fetch', fetchMock)
    return { deletes }
  }

  it('counts what is installed now, not what was installed when it opened', async () => {
    // Regression. The catalog is read once, so `installedCount` went on
    // reporting the moment the view opened: a `role="status"` line saying
    // "0 installed" directly above a card saying "Installed — 870 MB on disk".
    stubMutableCatalog([sampleInstallableModel, sampleBuiltInModel])
    renderLibrary()

    expect(await screen.findByRole('status')).toHaveTextContent(
      '2 models catalogued · 1 installed',
    )

    const card = screen.getByRole('article', {
      name: sampleInstallableModel.display_name,
    })
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Install' }),
    )

    await waitFor(() => {
      expect(card).toHaveTextContent('Installed — 870 MB on disk')
    })
    expect(screen.getByRole('status')).toHaveTextContent(
      '2 models catalogued · 2 installed',
    )
  })

  it('counts down again when the weights are removed', async () => {
    stubMutableCatalog([
      modelInstalling({ state: 'installed' }),
      sampleBuiltInModel,
    ])
    renderLibrary()

    expect(await screen.findByRole('status')).toHaveTextContent(
      '2 models catalogued · 2 installed',
    )

    const card = screen.getByRole('article', {
      name: sampleInstallableModel.display_name,
    })
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Remove weights' }),
    )
    await userEvent.click(
      within(card).getByRole('button', { name: 'Delete the weights' }),
    )

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(
        '2 models catalogued · 1 installed',
      )
    })
  })

  it('does not re-read the collection to keep the count honest', async () => {
    // The answer that installed the weights *is* the fact, so it is written
    // into the held catalog rather than fetched again. A second collection
    // read would be a request nobody needs — and the beginning of a poll.
    let collectionReads = 0
    stubMutableCatalog([sampleInstallableModel, sampleBuiltInModel])
    const fetchMock = vi.mocked(fetch)
    const countCollectionReads = () => {
      collectionReads = fetchMock.mock.calls.filter(([url]) =>
        (url as string).endsWith('/models'),
      ).length
    }

    renderLibrary()
    const card = await screen.findByRole('article', {
      name: sampleInstallableModel.display_name,
    })
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Install' }),
    )
    await waitFor(() => {
      expect(card).toHaveTextContent('Installed — 870 MB on disk')
    })

    countCollectionReads()
    expect(collectionReads).toBe(1)
  })
})
