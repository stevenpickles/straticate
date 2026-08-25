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
  it('says the weights are downloads before it lists anything', () => {
    stubCatalog([])
    const { region } = renderLibrary()
    expect(region).toHaveTextContent(/straticate ships no model weights/i)
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
