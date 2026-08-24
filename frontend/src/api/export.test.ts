import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { ApiError } from './client'
import {
  DEFAULT_EXPORT_FORMAT,
  EXPORT_FORMAT_OPTIONS,
  downloadExport,
  exportFormatOption,
  exportUrl,
  filenameFromContentDisposition,
} from './export'
import type { ExportFormat } from './types'
import { sampleJobId } from '../test/fixtures'

const twoStems = ['vocals', 'instrumental']
const fourStems = ['vocals', 'drums', 'bass', 'other']

function selection(
  stems: readonly string[],
  availableStems: readonly string[],
  format: ExportFormat = DEFAULT_EXPORT_FORMAT,
) {
  return { format, stems, availableStems }
}

function blobResponse(
  body: BlobPart,
  headers: Record<string, string> = {},
): Response {
  return new Response(body as BodyInit, { status: 200, headers })
}

function errorResponse(
  status: number,
  code: string,
  message: string,
  detail?: unknown,
): Response {
  return new Response(JSON.stringify({ error: { code, message, detail } }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue(response)
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

// ---------------------------------------------------------------------------
// Object URLs and the save click: jsdom implements neither, so both are
// installed here and taken away again afterwards.
// ---------------------------------------------------------------------------

/** `URL` as the object the object-URL statics are installed on. */
const objectUrls = URL as unknown as Partial<
  Record<'createObjectURL' | 'revokeObjectURL', unknown>
>

let createObjectURL: ReturnType<typeof vi.fn>
let revokeObjectURL: ReturnType<typeof vi.fn>
/** Every anchor whose `click()` the download triggered, in order. */
let clicked: HTMLAnchorElement[]
/** Makes the next save click fail, standing in for a blocked download. */
let clickFails: boolean

beforeEach(() => {
  createObjectURL = vi.fn(() => 'blob:straticate/export')
  revokeObjectURL = vi.fn()
  clicked = []
  clickFails = false
  objectUrls.createObjectURL = createObjectURL
  objectUrls.revokeObjectURL = revokeObjectURL
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
    // The download anchor is in the document when it is clicked.
    const anchor = document.querySelector('a[download]')
    if (anchor !== null) {
      clicked.push(anchor as HTMLAnchorElement)
    }
    if (clickFails) {
      throw new Error('download blocked')
    }
  })
})

afterEach(async () => {
  // Revokes are deferred by a task, so let any still-pending one land against
  // this test's mocks rather than the next test's.
  await new Promise<void>((resolve) => setTimeout(resolve, 0))
  delete objectUrls.createObjectURL
  delete objectUrls.revokeObjectURL
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The anchor the download created and clicked. */
function clickedAnchor(): HTMLAnchorElement {
  const anchor = clicked[0]
  if (anchor === undefined) {
    throw new Error('no download anchor was clicked')
  }
  return anchor
}

describe('export formats', () => {
  it('offers every format of the generated ExportFormat union', () => {
    // Keyed by the union in api/export.ts, so this list cannot drift from the
    // backend's enum without a type error.
    expect(EXPORT_FORMAT_OPTIONS.map((option) => option.id)).toEqual([
      'wav_pcm24',
      'wav_float32',
      'flac',
    ])
  })

  it('defaults to the format the backend applies when none is sent', () => {
    expect(DEFAULT_EXPORT_FORMAT).toBe('wav_pcm24')
  })

  it('gives every format a label, an honest note and an extension', () => {
    for (const option of EXPORT_FORMAT_OPTIONS) {
      expect(option.label).not.toBe('')
      expect(option.note).not.toBe('')
      expect(option.extension).toMatch(/^[a-z0-9]+$/)
      expect(exportFormatOption(option.id)).toEqual(option)
    }
  })

  it('says out loud that the WAV formats add no information', () => {
    expect(exportFormatOption('wav_pcm24').note).toMatch(/adds no detail/i)
    expect(exportFormatOption('wav_float32').note).toMatch(/adds no detail/i)
  })
})

describe('exportUrl', () => {
  it('omits the stems parameter when every stem is selected', () => {
    expect(exportUrl(sampleJobId, selection(twoStems, twoStems))).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24`,
    )
    expect(exportUrl(sampleJobId, selection(fourStems, fourStems))).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24`,
    )
  })

  it('omits the stems parameter however the full selection is ordered', () => {
    expect(
      exportUrl(sampleJobId, selection([...fourStems].reverse(), fourStems)),
    ).toBe(`/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24`)
  })

  it('joins a subset with commas, in the result’s own order', () => {
    expect(
      exportUrl(sampleJobId, selection(['bass', 'vocals'], fourStems)),
    ).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=vocals,bass`,
    )
  })

  it('sends a single stem on its own', () => {
    expect(exportUrl(sampleJobId, selection(['vocals'], twoStems))).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=vocals`,
    )
  })

  it('carries the chosen format', () => {
    expect(exportUrl(sampleJobId, selection(twoStems, twoStems, 'flac'))).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=flac`,
    )
    expect(
      exportUrl(sampleJobId, selection(['vocals'], twoStems, 'wav_float32')),
    ).toBe(`/api/v1/jobs/${sampleJobId}/export?format=wav_float32&stems=vocals`)
  })

  it('deduplicates a repeated stem instead of sending it twice', () => {
    expect(exportUrl(sampleJobId, selection(['bass', 'bass'], fourStems))).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=bass`,
    )
  })

  it('refuses an empty selection rather than sending "stems="', () => {
    expect(() => exportUrl(sampleJobId, selection([], twoStems))).toThrow(
      /at least one stem/i,
    )
  })

  it('percent-encodes the job id and every stem name', () => {
    expect(
      exportUrl('a/b?c', selection(['drums & bass'], ['drums & bass', 'x'])),
    ).toBe(
      '/api/v1/jobs/a%2Fb%3Fc/export?format=wav_pcm24&stems=drums%20%26%20bass',
    )
  })

  it('sends an unknown stem name rather than silently exporting everything', () => {
    // Same count as the result, so a length-only check would call this "all".
    expect(
      exportUrl(sampleJobId, selection(['vocals', 'ghost'], twoStems)),
    ).toBe(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=vocals,ghost`,
    )
  })
})

describe('filenameFromContentDisposition', () => {
  it('reads a quoted filename', () => {
    expect(
      filenameFromContentDisposition('attachment; filename="job-flac.zip"'),
    ).toBe('job-flac.zip')
  })

  it('reads an unquoted filename', () => {
    expect(
      filenameFromContentDisposition('attachment; filename=stem.wav'),
    ).toBe('stem.wav')
  })

  it('prefers the RFC 5987 extended parameter', () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="fallback.wav"; filename*=UTF-8\'\'caf%C3%A9.wav',
      ),
    ).toBe('café.wav')
  })

  it('strips any path the server put in the name', () => {
    expect(
      filenameFromContentDisposition('attachment; filename="/etc/passwd"'),
    ).toBe('passwd')
  })

  it('returns null when there is no header or no filename', () => {
    expect(filenameFromContentDisposition(null)).toBeNull()
    expect(filenameFromContentDisposition('attachment')).toBeNull()
    expect(filenameFromContentDisposition('attachment; filename=""')).toBeNull()
  })
})

describe('downloadExport', () => {
  it('fetches the URL the selection builds', async () => {
    const fetchMock = stubFetch(blobResponse('zip-bytes'))

    await downloadExport(sampleJobId, selection(['vocals', 'bass'], fourStems))

    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/jobs/${sampleJobId}/export?format=wav_pcm24&stems=vocals,bass`,
    )
  })

  it('saves under the filename Content-Disposition offers', async () => {
    stubFetch(
      blobResponse('zip-bytes', {
        'Content-Disposition': `attachment; filename="${sampleJobId}-flac.zip"`,
      }),
    )

    const result = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems, 'flac'),
    )

    expect(result.filename).toBe(`${sampleJobId}-flac.zip`)
    expect(clickedAnchor().download).toBe(`${sampleJobId}-flac.zip`)
    expect(clickedAnchor().getAttribute('href')).toBe('blob:straticate/export')
  })

  it('falls back to a contract-shaped name for a single stem', async () => {
    stubFetch(blobResponse('wav-bytes'))

    const result = await downloadExport(
      sampleJobId,
      selection(['vocals'], twoStems, 'wav_float32'),
    )

    expect(result.filename).toBe(`${sampleJobId}-wav_float32-vocals.wav`)
  })

  it('falls back to a zip name for a multi-stem export', async () => {
    stubFetch(blobResponse('zip-bytes'))

    const result = await downloadExport(
      sampleJobId,
      selection(fourStems, fourStems, 'flac'),
    )

    expect(result.filename).toBe(`${sampleJobId}-flac.zip`)
  })

  it('reports the downloaded size', async () => {
    stubFetch(blobResponse('12345'))

    const result = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems),
    )

    expect(result.sizeBytes).toBe(5)
  })

  it('revokes the object URL it created', async () => {
    stubFetch(blobResponse('zip-bytes'))

    await downloadExport(sampleJobId, selection(twoStems, twoStems))

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:straticate/export')
    })
  })

  it('revokes the object URL in a later task than the save click', async () => {
    stubFetch(blobResponse('zip-bytes'))

    await downloadExport(sampleJobId, selection(twoStems, twoStems))

    // Awaiting the download drains microtasks, so a revoke that had run in the
    // click's own task would already show here. Safari and older Firefox abort
    // a download whose object URL is revoked before the browser has taken it,
    // which would resolve this promise with no file saved.
    expect(clicked).toHaveLength(1)
    expect(revokeObjectURL).not.toHaveBeenCalled()

    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:straticate/export')
    })
  })

  it('removes the temporary anchor from the document', async () => {
    stubFetch(blobResponse('zip-bytes'))

    await downloadExport(sampleJobId, selection(twoStems, twoStems))

    expect(document.querySelectorAll('a[download]')).toHaveLength(0)
  })

  it('revokes the object URL even when the save click throws', async () => {
    stubFetch(blobResponse('zip-bytes'))
    clickFails = true

    await expect(
      downloadExport(sampleJobId, selection(twoStems, twoStems)),
    ).rejects.toThrow(/download blocked/)

    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:straticate/export')
    })
    expect(document.querySelectorAll('a[download]')).toHaveLength(0)
  })

  it('never creates an object URL for a failed request', async () => {
    stubFetch(errorResponse(404, 'job_not_found', 'No such job.'))

    await expect(
      downloadExport(sampleJobId, selection(twoStems, twoStems)),
    ).rejects.toBeInstanceOf(ApiError)

    expect(createObjectURL).not.toHaveBeenCalled()
    expect(revokeObjectURL).not.toHaveBeenCalled()
  })

  it('rejects with a typed ApiError for an unknown job', async () => {
    stubFetch(errorResponse(404, 'job_not_found', 'No such job.'))

    const error = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems),
    ).catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
    expect((error as ApiError).code).toBe('job_not_found')
  })

  it('carries the 409 state detail through', async () => {
    stubFetch(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'separating',
      }),
    )

    const error = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems),
    ).catch((reason: unknown) => reason)

    expect((error as ApiError).detail).toEqual({
      job_id: sampleJobId,
      state: 'separating',
    })
  })

  it('carries the export_failed reason through without interpreting it', async () => {
    stubFetch(
      errorResponse(500, 'export_failed', 'The export failed.', {
        job_id: sampleJobId,
        format: 'flac',
        reason: 'transcode_failed',
      }),
    )

    const error = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems, 'flac'),
    ).catch((reason: unknown) => reason)

    expect((error as ApiError).status).toBe(500)
    expect((error as ApiError).code).toBe('export_failed')
  })

  it('falls back to a generic envelope for a non-JSON failure', async () => {
    stubFetch(new Response('Bad Gateway', { status: 502 }))

    const error = await downloadExport(
      sampleJobId,
      selection(twoStems, twoStems),
    ).catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('unknown_error')
    expect((error as ApiError).status).toBe(502)
  })

  it('never requests an empty stems parameter', async () => {
    const fetchMock = stubFetch(blobResponse('zip-bytes'))

    await expect(
      downloadExport(sampleJobId, selection([], twoStems)),
    ).rejects.toThrow(/at least one stem/i)

    expect(fetchMock).not.toHaveBeenCalled()
  })
})
