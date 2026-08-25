import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { getModel, installModel, removeModelWeights } from './models'
import {
  modelInstalling,
  sampleBuiltInModel,
  sampleInstallableModel,
  sampleWeightsBytes,
} from '../test/fixtures'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('getModel', () => {
  it('GETs /models/{id} and parses the installation block', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleInstallableModel))

    const model = await getModel(sampleInstallableModel.id)

    expect(model).toEqual(sampleInstallableModel)
    expect(model.installation).toEqual({
      state: 'available',
      requires_download: true,
      total_bytes: sampleWeightsBytes,
      downloaded_bytes: null,
      progress: null,
      error: null,
    })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/models/vocals-hq-001',
      undefined,
    )
  })

  it('parses live download progress while an install runs', async () => {
    stubFetch(
      jsonResponse(
        modelInstalling({
          state: 'downloading',
          downloaded_bytes: sampleWeightsBytes / 4,
          progress: 0.25,
        }),
      ),
    )

    const model = await getModel(sampleInstallableModel.id)

    expect(model.installation?.state).toBe('downloading')
    expect(model.installation?.progress).toBe(0.25)
    expect(model.installation?.downloaded_bytes).toBe(sampleWeightsBytes / 4)
  })

  it('parses a failed install, whose reason rides on the model', async () => {
    stubFetch(
      jsonResponse(
        modelInstalling({
          state: 'failed',
          error: {
            code: 'checksum_mismatch',
            message: 'The downloaded weights for vocals-hq-001 did not match.',
            detail: { actual: 'a1b2c3' },
          },
        }),
      ),
    )

    const model = await getModel(sampleInstallableModel.id)

    expect(model.installation?.state).toBe('failed')
    expect(model.installation?.error?.code).toBe('checksum_mismatch')
    // Feature 025 keeps the download URL and the pinned digest off the wire.
    expect(JSON.stringify(model)).not.toContain('http')
  })

  it('parses a model that needs no download at all', async () => {
    stubFetch(jsonResponse(sampleBuiltInModel))

    const model = await getModel(sampleBuiltInModel.id)

    expect(model.installation?.requires_download).toBe(false)
    expect(model.installation?.state).toBe('installed')
    expect(model.installation?.total_bytes).toBeNull()
  })

  it('percent-encodes the model ID', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleInstallableModel))

    await getModel('../secrets')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/models/..%2Fsecrets',
      undefined,
    )
  })

  it('rejects with a typed ApiError carrying the backend envelope', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'model_not_found',
            message: "Model 'nope' is not in the catalog.",
            detail: { model_id: 'nope' },
          },
        },
        404,
      ),
    )

    const error = await getModel('nope').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.code).toBe('model_not_found')
    expect(apiError.detail).toEqual({ model_id: 'nope' })
  })
})

describe('installModel', () => {
  it('POSTs to /models/{id}/install and resolves the downloading model', async () => {
    const downloading = modelInstalling({
      state: 'downloading',
      downloaded_bytes: 0,
      progress: 0,
    })
    const fetchMock = stubFetch(jsonResponse(downloading, 202))

    await expect(installModel(sampleInstallableModel.id)).resolves.toEqual(
      downloading,
    )
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/models/vocals-hq-001/install',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: undefined,
      },
    )
  })

  it('rejects with model_busy when an install is already running', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'model_busy',
            message: 'An install is already running for vocals-hq-001.',
          },
        },
        409,
      ),
    )

    const error = await installModel(sampleInstallableModel.id).catch(
      (e: unknown) => e,
    )
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('model_busy')
    expect((error as ApiError).status).toBe(409)
  })

  it('rejects with model_not_downloadable for a built-in model', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'model_not_downloadable',
            message: 'Model fake-vocals-001 has no downloadable weights.',
          },
        },
        409,
      ),
    )

    const error = await installModel(sampleBuiltInModel.id).catch(
      (e: unknown) => e,
    )
    expect((error as ApiError).code).toBe('model_not_downloadable')
  })
})

describe('removeModelWeights', () => {
  it('DELETEs /models/{id}/weights and resolves the model it returned', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleInstallableModel))

    const model = await removeModelWeights(sampleInstallableModel.id)

    expect(model.installation?.state).toBe('available')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/models/vocals-hq-001/weights',
      { method: 'DELETE' },
    )
  })

  it('rejects with a typed ApiError for an unknown model', async () => {
    stubFetch(
      jsonResponse(
        { error: { code: 'model_not_found', message: 'No such model.' } },
        404,
      ),
    )

    const error = await removeModelWeights('nope').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('model_not_found')
  })
})
