import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { fetchStemAudio, getSeparationResult, stemUrl } from './stems'
import { sampleJobId, sampleResult } from '../test/fixtures'

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

describe('stemUrl', () => {
  it('builds the stem streaming URL under the v1 prefix', () => {
    expect(stemUrl(sampleJobId, 'vocals')).toBe(
      `/api/v1/jobs/${sampleJobId}/stems/vocals`,
    )
  })

  it('percent-encodes the job id and the stem name', () => {
    expect(stemUrl('a/b?c', '../secret')).toBe(
      '/api/v1/jobs/a%2Fb%3Fc/stems/..%2Fsecret',
    )
  })

  it('builds a URL for every stem of a result, whatever they are called', () => {
    const names = ['vocals', 'drums', 'bass', 'other']
    expect(names.map((name) => stemUrl(sampleJobId, name))).toEqual(
      names.map((name) => `/api/v1/jobs/${sampleJobId}/stems/${name}`),
    )
  })
})

describe('getSeparationResult', () => {
  it('GETs /jobs/{id}/result and parses the result', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleResult))

    await expect(getSeparationResult(sampleJobId)).resolves.toEqual(
      sampleResult,
    )
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/jobs/${sampleJobId}/result`,
      undefined,
    )
  })

  it('percent-encodes the job id', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleResult))

    await getSeparationResult('a/b?c')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/jobs/a%2Fb%3Fc/result',
      undefined,
    )
  })

  it('parses a four-stem result without knowing any stem name', async () => {
    const fourStem = {
      ...sampleResult,
      stems: ['vocals', 'drums', 'bass', 'other'].map((name) => ({
        name,
        duration_seconds: 227.4,
        sample_rate_hz: 44100,
        channels: 2,
      })),
    }
    stubFetch(jsonResponse(fourStem))

    const result = await getSeparationResult(sampleJobId)

    expect(result.stems).toHaveLength(4)
  })

  it('rejects with a typed ApiError carrying the 409 state detail', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'result_not_available',
            message: 'The job has no result.',
            detail: { job_id: sampleJobId, state: 'cancelled' },
          },
        },
        409,
      ),
    )

    const error = await getSeparationResult(sampleJobId).catch(
      (reason: unknown) => reason,
    )
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(409)
    expect(apiError.code).toBe('result_not_available')
    expect(apiError.detail).toEqual({
      job_id: sampleJobId,
      state: 'cancelled',
    })
  })

  it('rejects with a typed ApiError for an unknown job', async () => {
    stubFetch(
      jsonResponse(
        { error: { code: 'job_not_found', message: 'No such job.' } },
        404,
      ),
    )

    const error = await getSeparationResult(sampleJobId).catch(
      (reason: unknown) => reason,
    )
    expect((error as ApiError).code).toBe('job_not_found')
  })
})

describe('fetchStemAudio', () => {
  it('fetches the stem URL and returns its bytes', async () => {
    const bytes = new Uint8Array([1, 2, 3, 4])
    const fetchMock = stubFetch(new Response(bytes, { status: 200 }))

    const url = stemUrl(sampleJobId, 'vocals')
    const buffer = await fetchStemAudio(url)

    expect(fetchMock).toHaveBeenCalledWith(url, { signal: undefined })
    expect(new Uint8Array(buffer)).toEqual(bytes)
  })

  it('passes the abort signal through to fetch', async () => {
    const fetchMock = stubFetch(
      new Response(new Uint8Array([1]), { status: 200 }),
    )
    const controller = new AbortController()

    await fetchStemAudio(stemUrl(sampleJobId, 'vocals'), controller.signal)

    expect(fetchMock).toHaveBeenCalledWith(expect.any(String), {
      signal: controller.signal,
    })
  })

  it('rejects when the signal is already aborted', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.signal?.aborted === true) {
          return Promise.reject(new DOMException('Aborted', 'AbortError'))
        }
        return Promise.resolve(new Response(new Uint8Array([1])))
      }),
    )
    const controller = new AbortController()
    controller.abort()

    await expect(
      fetchStemAudio(stemUrl(sampleJobId, 'vocals'), controller.signal),
    ).rejects.toThrow(/Aborted/)
  })

  it('rejects with a typed ApiError when the stem file is gone', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'stem_file_missing',
            message: 'The stem file is missing.',
            detail: { stem: 'vocals' },
          },
        },
        404,
      ),
    )

    const error = await fetchStemAudio(stemUrl(sampleJobId, 'vocals')).catch(
      (reason: unknown) => reason,
    )

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.code).toBe('stem_file_missing')
    expect(apiError.message).toBe('The stem file is missing.')
  })

  it('falls back to a generic envelope for a non-JSON failure', async () => {
    stubFetch(new Response('Requested Range Not Satisfiable', { status: 416 }))

    const error = await fetchStemAudio(stemUrl(sampleJobId, 'vocals')).catch(
      (reason: unknown) => reason,
    )

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('unknown_error')
    expect((error as ApiError).status).toBe(416)
  })
})
