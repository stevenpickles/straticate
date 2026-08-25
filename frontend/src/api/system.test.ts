import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { getSystemStorage } from './system'

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

describe('getSystemStorage', () => {
  it('GETs /system/storage and parses the figures', async () => {
    const fetchMock = stubFetch(
      jsonResponse({ free_bytes: 2_147_483_648, total_bytes: 512_110_190_592 }),
    )

    const report = await getSystemStorage()

    expect(report).toEqual({
      free_bytes: 2_147_483_648,
      total_bytes: 512_110_190_592,
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/system/storage', undefined)
  })

  it('parses the documented unknown answer as a value, not a failure', async () => {
    // A host that cannot produce the figures still answers 200 with nulls —
    // the backend degrades rather than raising (feature 018's pattern). The
    // caller distinguishes "unknown" from "the request failed"; both are
    // cautious, but only one is worth offering a retry for.
    stubFetch(jsonResponse({ free_bytes: null, total_bytes: null }))

    await expect(getSystemStorage()).resolves.toEqual({
      free_bytes: null,
      total_bytes: null,
    })
  })

  it('parses a full disk as zero free rather than as unknown', async () => {
    stubFetch(jsonResponse({ free_bytes: 0, total_bytes: 512_110_190_592 }))

    const report = await getSystemStorage()

    expect(report.free_bytes).toBe(0)
    expect(report.free_bytes).not.toBeNull()
  })

  it('rejects with a typed ApiError when the request itself fails', async () => {
    stubFetch(
      jsonResponse({ error: { code: 'unknown_error', message: 'Nope.' } }, 500),
    )

    await expect(getSystemStorage()).rejects.toBeInstanceOf(ApiError)
  })
})
