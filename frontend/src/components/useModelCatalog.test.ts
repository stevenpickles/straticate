import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useModelCatalog } from './useModelCatalog'
import { sampleBuiltInModel, sampleInstallableModel } from '../test/fixtures'

const CATALOG = [sampleInstallableModel, sampleBuiltInModel]

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** Stub `fetch` with a scripted sequence of `GET /models` answers. */
function stubCatalogReads(reads: Response[]): ReturnType<typeof vi.fn> {
  let index = 0
  const fetchMock = vi.fn(() => {
    const next = reads[Math.min(index, reads.length - 1)]
    index += 1
    return Promise.resolve(next ?? jsonResponse(CATALOG))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useModelCatalog', () => {
  it('reads the catalog once on mount', async () => {
    const fetchMock = stubCatalogReads([jsonResponse(CATALOG)])
    const { result } = renderHook(() => useModelCatalog())

    expect(result.current.status).toBe('loading')
    expect(result.current.models).toEqual([])

    await waitFor(() => {
      expect(result.current.status).toBe('loaded')
    })
    expect(result.current.models).toEqual(CATALOG)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/models', undefined)
  })

  it('never polls: reading the catalog is not watching a download', async () => {
    // Each row watches its own model (`useModelInstallation`); re-reading the
    // whole collection on a timer would duplicate that and cost a request per
    // second whether or not anything was downloading.
    vi.useFakeTimers()
    try {
      const fetchMock = stubCatalogReads([jsonResponse(CATALOG)])
      renderHook(() => useModelCatalog())
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000)
      })
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('surfaces a failed read with the backend’s own code and message', async () => {
    stubCatalogReads([
      jsonResponse(
        { error: { code: 'internal_error', message: 'Catalog unreadable.' } },
        500,
      ),
    ])
    const { result } = renderHook(() => useModelCatalog())

    await waitFor(() => {
      expect(result.current.status).toBe('error')
    })
    expect(result.current.error).toEqual({
      code: 'internal_error',
      message: 'Catalog unreadable.',
    })
    expect(result.current.models).toEqual([])
  })

  it('retries on refresh, and stops claiming the read failed while it does', async () => {
    const fetchMock = stubCatalogReads([
      jsonResponse({ error: { code: 'x', message: 'Nope.' } }, 500),
      jsonResponse(CATALOG),
    ])
    const { result } = renderHook(() => useModelCatalog())
    await waitFor(() => {
      expect(result.current.status).toBe('error')
    })

    act(() => {
      result.current.refresh()
    })
    // The stale failure is dropped the moment the retry starts: a view that
    // still says "could not be read" while the request that may disprove it is
    // in flight is telling the user something it does not know.
    expect(result.current.status).toBe('loading')

    await waitFor(() => {
      expect(result.current.status).toBe('loaded')
    })
    expect(result.current.models).toEqual(CATALOG)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps the catalog on screen while a refresh is in flight', async () => {
    const fetchMock = stubCatalogReads([jsonResponse(CATALOG)])
    const { result } = renderHook(() => useModelCatalog())
    await waitFor(() => {
      expect(result.current.status).toBe('loaded')
    })

    act(() => {
      result.current.refresh()
    })
    expect(result.current.status).toBe('loaded')
    expect(result.current.models).toEqual(CATALOG)

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })
  })

  it('applies nothing after unmount', async () => {
    let settle!: (response: Response) => void
    const pending = new Promise<Response>((resolve) => {
      settle = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(() => pending),
    )

    const { unmount } = renderHook(() => useModelCatalog())
    unmount()
    await act(async () => {
      settle(jsonResponse(CATALOG))
      await pending
    })
    // No "state update on an unmounted component" warning, and nothing to
    // assert beyond that: the cancelled read simply does not land.
  })
})
