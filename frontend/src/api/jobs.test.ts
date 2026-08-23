import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { cancelJob, createJob, getJob, listJobs } from './jobs'
import { sampleConfiguration, sampleJob, sampleJobId } from '../test/fixtures'

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

describe('createJob', () => {
  it('POSTs the configuration to /jobs and parses the queued job', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleJob, 201))

    await expect(createJob(sampleConfiguration)).resolves.toEqual(sampleJob)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sampleConfiguration),
    })
  })

  it('rejects with a typed ApiError carrying the backend envelope', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'audio_not_found',
            message: 'No such audio file.',
            detail: { audio_id: sampleConfiguration.audio_id },
          },
        },
        404,
      ),
    )

    const error = await createJob(sampleConfiguration).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.code).toBe('audio_not_found')
    expect(apiError.message).toBe('No such audio file.')
    expect(apiError.detail).toEqual({ audio_id: sampleConfiguration.audio_id })
  })
})

describe('listJobs', () => {
  it('GETs /jobs and parses the array of jobs', async () => {
    const fetchMock = stubFetch(jsonResponse([sampleJob]))

    await expect(listJobs()).resolves.toEqual([sampleJob])
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/jobs', undefined)
  })
})

describe('getJob', () => {
  it('GETs /jobs/{id} and parses the job', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleJob))

    await expect(getJob(sampleJobId)).resolves.toEqual(sampleJob)
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/jobs/${sampleJobId}`,
      undefined,
    )
  })

  it('percent-encodes the job id', async () => {
    const fetchMock = stubFetch(jsonResponse(sampleJob))

    await getJob('a/b?c')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/jobs/a%2Fb%3Fc', undefined)
  })

  it('rejects with a typed ApiError for an unknown job', async () => {
    stubFetch(
      jsonResponse(
        { error: { code: 'job_not_found', message: 'No such job.' } },
        404,
      ),
    )

    const error = await getJob(sampleJobId).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('job_not_found')
  })
})

describe('cancelJob', () => {
  it('POSTs to /jobs/{id}/cancel without a body and parses the job', async () => {
    const cancelled = { ...sampleJob, state: 'cancelled' as const }
    const fetchMock = stubFetch(jsonResponse(cancelled))

    await expect(cancelJob(sampleJobId)).resolves.toEqual(cancelled)
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/jobs/${sampleJobId}/cancel`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: undefined,
      },
    )
  })

  it('rejects with a typed ApiError when the job cannot be cancelled', async () => {
    stubFetch(
      jsonResponse(
        {
          error: {
            code: 'job_not_cancellable',
            message: 'The job has already finished.',
          },
        },
        409,
      ),
    )

    const error = await cancelJob(sampleJobId).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(409)
    expect(apiError.code).toBe('job_not_cancellable')
  })
})
