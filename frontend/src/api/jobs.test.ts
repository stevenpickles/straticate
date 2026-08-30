import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import {
  DEFAULT_STEREO_HANDLING,
  STEREO_HANDLING_OPTIONS,
  cancelJob,
  createJob,
  getJob,
  listJobs,
  stereoHandlingOption,
} from './jobs'
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

describe('stereo-handling presentation table (features 041, 062)', () => {
  it('describes every choice the contract offers, in picker order', () => {
    // The table is keyed by the generated union, so a backend that gains a
    // value is a type error here until it is described. The order is least to
    // most done to the recording.
    expect(STEREO_HANDLING_OPTIONS.map((option) => option.id)).toEqual([
      'as_is',
      'mono_bass',
      'mono',
    ])
    for (const option of STEREO_HANDLING_OPTIONS) {
      expect(option.label.length).toBeGreaterThan(0)
      expect(option.note.length).toBeGreaterThan(0)
      expect(stereoHandlingOption(option.id)).toEqual(option)
    }
  })

  it('defaults to the value the backend applies when the field is omitted', () => {
    expect(DEFAULT_STEREO_HANDLING).toBe('as_is')
    expect(STEREO_HANDLING_OPTIONS[0]?.id).toBe(DEFAULT_STEREO_HANDLING)
  })

  it('promises nothing about quality, only about what is done and what it costs', () => {
    // This control changes the user's audio. "Improves separation" would be a
    // claim the app cannot make for an arbitrary mix; feature 041 measured one
    // track, not a population.
    for (const option of STEREO_HANDLING_OPTIONS) {
      expect(option.note).not.toMatch(/improve|better|best|fix(es)?\b/i)
    }
    expect(stereoHandlingOption('mono').note).toMatch(/mono/i)
  })

  it.each(['mono', 'mono_bass'] as const)(
    'frames %s as recovering a stem, not as separating better',
    (handling) => {
      // Features 041 and 062 measured this: the four stems reconstruct the
      // mixture at +0.999 in every case, so nothing is gained overall — a
      // near-silent stem becomes usable because the low end is reassigned. The
      // note must say that and stop there.
      const note = stereoHandlingOption(handling).note
      expect(note).toMatch(/recovers a stem/i)
      expect(note).toMatch(/near-silent/i)
      expect(note).toMatch(/does not otherwise change/i)
    },
  )

  it('says what each fold does to the stems, since that is the visible cost', () => {
    // The two folds differ in exactly one thing a user will notice afterwards,
    // and each note has to be the one that says so.
    expect(stereoHandlingOption('mono').note).toMatch(/come back mono/i)
    expect(stereoHandlingOption('mono_bass').note).toMatch(
      /come back in stereo/i,
    )
  })

  it('does not put a number on the recovery', () => {
    // Feature 062 measured 19.4% of the source's low band on one track. A note
    // that quoted it would be presenting a single measurement as a property of
    // the control; 041 refused the same temptation for the same reason.
    for (const option of STEREO_HANDLING_OPTIONS) {
      expect(option.note).not.toMatch(/\d+\s*(%|dB|Hz)/i)
    }
  })
})
