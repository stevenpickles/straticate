import { describe, expect, it, vi, afterEach } from 'vitest'
import { ApiError, get, post, getHealth, getVersion } from './client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client', () => {
  it('getHealth() parses a success response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: 'ok' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getHealth()).resolves.toEqual({ status: 'ok' })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/health', undefined)
  })

  it('getVersion() parses a success response', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ version: '0.1.0' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getVersion()).resolves.toEqual({ version: '0.1.0' })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/version', undefined)
  })

  it('turns the backend error envelope into a typed ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: {
              code: 'not_found',
              message: 'No such job',
              detail: { jobId: 'abc' },
            },
          },
          404,
        ),
      ),
    )

    const error = await get('/jobs/abc').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.code).toBe('not_found')
    expect(apiError.message).toBe('No such job')
    expect(apiError.detail).toEqual({ jobId: 'abc' })
  })

  it('falls back to a generic ApiError when the error body is not the envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('Bad Gateway', { status: 502 })),
    )

    const error = await get('/health').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(502)
    expect(apiError.code).toBe('unknown_error')
    expect(apiError.message).toBe('HTTP 502')
  })

  it('post() sends a JSON body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(post('/jobs', { model: 'demo' })).resolves.toEqual({
      ok: true,
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'demo' }),
    })
  })

  it('resolves undefined for a 204 response instead of parsing a body', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(null, { status: 204 })),
    )

    await expect(get('/audio/abc')).resolves.toBeUndefined()
  })
})
