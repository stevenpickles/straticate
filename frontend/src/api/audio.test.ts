import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import {
  deleteAudio,
  getAudioAnalysis,
  startAudioUpload,
  uploadAudio,
} from './audio'
import { ApiError } from './client'
import { installMockXhr, lastXhr } from '../test/mockXhr'
import { sampleAudioFile } from '../test/fixtures'
import type { StereoAnalysis } from './types'

function makeFile(name = 'song.wav'): File {
  return new File(['RIFF....WAVE'], name, { type: 'audio/wav' })
}

beforeEach(() => {
  installMockXhr()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('uploadAudio', () => {
  it('POSTs multipart form data with a `file` field to /api/v1/audio', async () => {
    const promise = uploadAudio(makeFile())
    const xhr = lastXhr()
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/v1/audio')
    expect(xhr.sentBody).toBeInstanceOf(FormData)
    const body = xhr.sentBody as FormData
    expect(body.get('file')).toBeInstanceOf(File)
    expect((body.get('file') as File).name).toBe('song.wav')

    xhr.respond(201, JSON.stringify(sampleAudioFile))
    await expect(promise).resolves.toEqual(sampleAudioFile)
  })

  it('reports determinate progress as a 0..1 fraction', async () => {
    const onProgress = vi.fn()
    const promise = uploadAudio(makeFile(), onProgress)
    const xhr = lastXhr()

    xhr.emitUploadProgress(25, 100)
    xhr.emitUploadProgress(100, 100)
    expect(onProgress).toHaveBeenNthCalledWith(1, 0.25)
    expect(onProgress).toHaveBeenNthCalledWith(2, 1)

    xhr.respond(201, JSON.stringify(sampleAudioFile))
    await promise
  })

  it('reports null progress when the length is not computable', async () => {
    const onProgress = vi.fn()
    const promise = uploadAudio(makeFile(), onProgress)
    const xhr = lastXhr()

    xhr.emitUploadProgress(1024, 0, false)
    expect(onProgress).toHaveBeenCalledWith(null)

    xhr.respond(201, JSON.stringify(sampleAudioFile))
    await promise
  })

  it('turns the backend error envelope into a typed ApiError', async () => {
    const promise = uploadAudio(makeFile())
    const xhr = lastXhr()
    xhr.respond(
      413,
      JSON.stringify({
        error: {
          code: 'audio_too_large',
          message: 'The uploaded file exceeds the maximum allowed size.',
        },
      }),
    )

    const error = await promise.catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(413)
    expect(apiError.code).toBe('audio_too_large')
    expect(apiError.message).toBe(
      'The uploaded file exceeds the maximum allowed size.',
    )
  })

  it('falls back to a generic ApiError when the error body is not the envelope', async () => {
    const promise = uploadAudio(makeFile())
    lastXhr().respond(502, 'Bad Gateway')

    const error = await promise.catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(502)
    expect(apiError.code).toBe('unknown_error')
    expect(apiError.message).toBe('HTTP 502')
  })

  it('rejects with network_error when the request fails at the network level', async () => {
    const promise = uploadAudio(makeFile())
    lastXhr().failNetwork()

    const error = await promise.catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('network_error')
  })
})

describe('startAudioUpload', () => {
  it('abort() aborts the XHR and rejects with upload_aborted', async () => {
    const handle = startAudioUpload(makeFile())
    handle.abort()

    const error = await handle.promise.catch((e: unknown) => e)
    expect(lastXhr().aborted).toBe(true)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('upload_aborted')
  })
})

describe('deleteAudio', () => {
  it('issues DELETE and resolves on 204 No Content', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(deleteAudio('01ABC')).resolves.toBeUndefined()
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/audio/01ABC', {
      method: 'DELETE',
    })
  })

  it('throws a typed ApiError on a not-found envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: 'audio_not_found', message: 'No such audio.' },
          }),
          { status: 404, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const error = await deleteAudio('missing').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('audio_not_found')
    expect((error as ApiError).status).toBe(404)
  })
})

describe('getAudioAnalysis', () => {
  it('GETs the analysis of one upload', async () => {
    const analysis: StereoAnalysis = {
      l_r_correlation: 0.229,
      wide_stereo: true,
    }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(analysis), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getAudioAnalysis('01ABC')).resolves.toEqual(analysis)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/audio/01ABC/analysis')
  })

  it('encodes the audio id into the path', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({ l_r_correlation: null, wide_stereo: false }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await getAudioAnalysis('a/b?c')
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/v1/audio/a%2Fb%3Fc/analysis',
    )
  })

  it('throws a typed ApiError when the analysis times out', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: 'audio_analysis_timed_out',
              message: 'Measuring the uploaded file timed out.',
            },
          }),
          { status: 504, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const error = await getAudioAnalysis('01ABC').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('audio_analysis_timed_out')
    expect((error as ApiError).status).toBe(504)
  })
})
