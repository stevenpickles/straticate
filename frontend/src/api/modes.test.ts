import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { listSeparationModes } from './modes'
import { sampleSeparationModes } from '../test/fixtures'

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

describe('listSeparationModes', () => {
  it('GETs /separation-modes and parses the derived modes', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleSeparationModes))

    await expect(listSeparationModes()).resolves.toEqual(sampleSeparationModes)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/separation-modes',
      undefined,
    )
  })

  it('parses stem lists and quality options of any size', async () => {
    stubFetch(jsonResponse(sampleSeparationModes))

    const modes = await listSeparationModes()
    expect(modes.map((mode) => mode.stems.length)).toEqual([2, 4])
    expect(modes.map((mode) => mode.quality_options.length)).toEqual([2, 1])
    expect(modes[0]?.quality_options[0]).toEqual({
      id: 'fast',
      display_name: 'Fast',
      model_id: 'vocals-fast-001',
    })
  })

  it('rejects with a typed ApiError carrying the backend envelope', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'model_catalog_unavailable',
            message: 'The model catalog could not be read.',
            detail: { path: 'models/catalog.json' },
          },
        },
        503,
      ),
    )

    const error = await listSeparationModes().catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(503)
    expect(apiError.code).toBe('model_catalog_unavailable')
    expect(apiError.message).toBe('The model catalog could not be read.')
    expect(apiError.detail).toEqual({ path: 'models/catalog.json' })
  })
})
