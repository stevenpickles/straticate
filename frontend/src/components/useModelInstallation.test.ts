import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import {
  POLL_INTERVAL_MS,
  needsInstall,
  startBlockedReason,
  useModelInstallation,
} from './useModelInstallation'
import type { Model } from '../api/types'
import {
  modelInstalling,
  sampleBuiltInModel,
  sampleInstallableModel,
  sampleWeightsBytes,
} from '../test/fixtures'

const MODEL_ID = sampleInstallableModel.id

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(
  code: string,
  message: string,
  status: number,
): Response {
  return jsonResponse({ error: { code, message } }, status)
}

type FetchMock = ReturnType<typeof vi.fn>

/** One scripted answer to `GET /models/{id}`. */
type ModelRead = Model | Response | Promise<Response>

/**
 * Stub `fetch` with a scripted sequence of `GET /models/{id}` answers: each
 * read takes the next entry, and the last one repeats forever. `install`
 * answers `POST .../install`.
 */
function stubModelReads(
  reads: ModelRead[],
  install?: Response | Promise<Response>,
): FetchMock {
  let index = 0
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith('/install')) {
      return Promise.resolve(
        install ?? jsonResponse(modelInstalling({ state: 'downloading' }), 202),
      )
    }
    if (init?.method !== undefined) {
      throw new Error(`unexpected ${init.method} ${url}`)
    }
    const next = reads[Math.min(index, reads.length - 1)]
    index += 1
    if (next instanceof Promise || next instanceof Response) {
      return Promise.resolve(next)
    }
    return Promise.resolve(jsonResponse(next ?? sampleInstallableModel))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Number of `GET /models/{id}` reads made so far. */
function reads(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(
    ([url, init]) =>
      (url as string).includes('/models/') &&
      !(url as string).endsWith('/install') &&
      (init as RequestInit | undefined)?.method === undefined,
  ).length
}

/** Let every pending promise settle without advancing the clock. */
async function settle(): Promise<void> {
  await act(async () => {
    // A read is `fetch` → `response.json()` → `setState`: several microtask
    // hops, none of them a duration.
    for (let hop = 0; hop < 5; hop += 1) {
      await vi.advanceTimersByTimeAsync(0)
    }
  })
}

/** Advance to the next scheduled poll and let its response settle. */
async function tick(times = 1): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
  }
}

/** A promise plus its resolver, for parking a request in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settleWith) => {
    resolve = settleWith
  })
  return { promise, resolve }
}

/** Set `document.visibilityState` and fire the event the browser would fire. */
function setVisibility(value: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', {
    value,
    configurable: true,
  })
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  setVisibility('visible')
})

/** A `startBlockedReason` argument for a model that has been read. */
function read(model: Model) {
  return { modelId: model.id, model, status: 'loaded' as const }
}

describe('needsInstall / startBlockedReason', () => {
  it('a model with no downloadable artifact is never in the way', () => {
    expect(needsInstall(sampleBuiltInModel)).toBe(false)
    expect(startBlockedReason(read(sampleBuiltInModel))).toBeNull()
  })

  it('an installed downloadable model is not in the way either', () => {
    const installed = modelInstalling({ state: 'installed' })
    expect(needsInstall(installed)).toBe(false)
    expect(startBlockedReason(read(installed))).toBeNull()
  })

  it('gives a distinct reason for each state that blocks a start', () => {
    const reasons = (['available', 'downloading', 'failed'] as const).map(
      (state) => startBlockedReason(read(modelInstalling({ state }))),
    )
    expect(reasons.every((reason) => reason !== null)).toBe(true)
    expect(new Set(reasons).size).toBe(3)
  })

  it('blocks while the model has not been read yet — unknown is not ready', () => {
    expect(needsInstall(null)).toBe(false)
    expect(
      startBlockedReason({
        modelId: MODEL_ID,
        model: null,
        status: 'loading',
      }),
    ).not.toBeNull()
    expect(
      startBlockedReason({ modelId: MODEL_ID, model: null, status: 'error' }),
    ).not.toBeNull()
  })

  it('blocks nothing when no tier is selected at all', () => {
    expect(
      startBlockedReason({ modelId: null, model: null, status: 'idle' }),
    ).toBeNull()
  })
})

describe('useModelInstallation reading', () => {
  it('reads the selected model once and exposes its installation block', async () => {
    const fetchMock = stubModelReads([sampleInstallableModel])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))

    expect(result.current.status).toBe('loading')
    await settle()

    expect(result.current.status).toBe('loaded')
    expect(result.current.model?.installation?.total_bytes).toBe(
      sampleWeightsBytes,
    )
    expect(reads(fetchMock)).toBe(1)
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/models/${MODEL_ID}`,
      undefined,
    )
  })

  it('reads nothing at all while no tier is selected', async () => {
    const fetchMock = stubModelReads([sampleInstallableModel])
    const { result } = renderHook(() => useModelInstallation(null))

    await settle()
    expect(result.current.status).toBe('idle')
    expect(result.current.model).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('never describes the previous model after the selection changes', async () => {
    stubModelReads([
      sampleInstallableModel,
      // Never settles: the second model's read is still in flight.
      new Promise<Response>(() => undefined),
    ])
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useModelInstallation(id),
      { initialProps: { id: MODEL_ID } },
    )
    await settle()
    expect(result.current.model?.id).toBe(MODEL_ID)

    rerender({ id: 'other-model-001' })
    expect(result.current.model).toBeNull()
    expect(result.current.status).toBe('loading')
  })

  it('surfaces a failed read as an error the user can retry', async () => {
    const fetchMock = stubModelReads([
      errorResponse('model_not_found', 'No such model.', 404),
      sampleInstallableModel,
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    expect(result.current.status).toBe('error')
    expect(result.current.error?.code).toBe('model_not_found')

    act(() => {
      result.current.refresh()
    })
    await settle()
    expect(result.current.status).toBe('loaded')
    expect(reads(fetchMock)).toBe(2)
  })
})

describe('useModelInstallation polling', () => {
  it('polls once per interval while downloading and stops when installed', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading', progress: 0.1 }),
      modelInstalling({ state: 'downloading', progress: 0.5 }),
      modelInstalling({ state: 'installed', progress: 1 }),
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    expect(reads(fetchMock)).toBe(1)

    await tick()
    expect(reads(fetchMock)).toBe(2)
    expect(result.current.model?.installation?.progress).toBe(0.5)

    await tick()
    expect(reads(fetchMock)).toBe(3)
    expect(result.current.model?.installation?.state).toBe('installed')

    // Terminal: nothing further is scheduled, however long we wait.
    await tick(5)
    expect(reads(fetchMock)).toBe(3)
  })

  it('stops on a failed install, which is terminal too', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading' }),
      modelInstalling({
        state: 'failed',
        error: { code: 'checksum_mismatch', message: 'Digest mismatch.' },
      }),
    ])
    renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    await tick(4)

    expect(reads(fetchMock)).toBe(2)
  })

  it('never polls a model that needs no download', async () => {
    const fetchMock = stubModelReads([sampleBuiltInModel])
    renderHook(() => useModelInstallation(sampleBuiltInModel.id))
    await settle()
    await tick(5)

    expect(reads(fetchMock)).toBe(1)
  })

  it('leaks no timer when the component unmounts mid-download', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading' }),
    ])
    const { unmount } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    expect(reads(fetchMock)).toBe(1)

    unmount()
    await tick(10)

    expect(reads(fetchMock)).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('pauses while the tab is hidden and re-reads the moment it returns', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading' }),
    ])
    renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    expect(reads(fetchMock)).toBe(1)

    setVisibility('hidden')
    await tick(5)
    expect(reads(fetchMock), 'a hidden tab polls nothing').toBe(1)

    setVisibility('visible')
    await settle()
    expect(reads(fetchMock), 'and is current again immediately').toBe(2)

    await tick()
    expect(reads(fetchMock)).toBe(3)
  })

  it('stops polling when a read fails, rather than hammering the backend', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading' }),
      errorResponse('service_unavailable', 'Backend is down.', 503),
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    await tick(4)

    expect(reads(fetchMock)).toBe(2)
    expect(result.current.error?.code).toBe('service_unavailable')
  })
})

describe('useModelInstallation installing', () => {
  it('POSTs the install and adopts the downloading model it returns', async () => {
    const fetchMock = stubModelReads(
      [sampleInstallableModel, modelInstalling({ state: 'installed' })],
      jsonResponse(
        modelInstalling({
          state: 'downloading',
          downloaded_bytes: 0,
          progress: 0,
        }),
        202,
      ),
    )
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    act(() => {
      result.current.install()
    })
    await settle()
    expect(result.current.model?.installation?.state).toBe('downloading')
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/models/${MODEL_ID}/install`,
      expect.objectContaining({ method: 'POST' }),
    )

    // The POST's answer starts the poll, with no extra prompting.
    await tick()
    expect(result.current.model?.installation?.state).toBe('installed')
  })

  it('sends one POST for a double click', async () => {
    const fetchMock = stubModelReads([sampleInstallableModel])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    act(() => {
      result.current.install()
      result.current.install()
    })
    await settle()

    expect(
      fetchMock.mock.calls.filter(([url]) =>
        (url as string).endsWith('/install'),
      ),
    ).toHaveLength(1)
  })

  it('surfaces a refused install and leaves the model readable', async () => {
    stubModelReads(
      [sampleInstallableModel],
      errorResponse('model_busy', 'An install is already running.', 409),
    )
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    act(() => {
      result.current.install()
    })
    await settle()

    expect(result.current.error?.code).toBe('model_busy')
    expect(result.current.installing).toBe(false)
    expect(result.current.model?.installation?.state).toBe('available')
  })
})

describe('useModelInstallation recovering', () => {
  it('a retry after a failed read resumes the poll', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'downloading', progress: 0.1 }),
      errorResponse('service_unavailable', 'Backend is down.', 503),
      modelInstalling({ state: 'downloading', progress: 0.5 }),
      modelInstalling({ state: 'installed', progress: 1 }),
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    // The read that fails mid-download stops the loop…
    await tick(4)
    expect(reads(fetchMock)).toBe(2)
    expect(result.current.error?.code).toBe('service_unavailable')
    expect(result.current.model?.installation?.state).toBe('downloading')

    // …and one retry — the panel's "Try again" — is all it takes to resume it.
    act(() => {
      result.current.refresh()
    })
    await settle()
    expect(reads(fetchMock)).toBe(3)
    expect(result.current.error).toBeNull()

    await tick()
    expect(reads(fetchMock)).toBe(4)
    expect(result.current.model?.installation?.state).toBe('installed')
  })

  it('a read already in flight cannot overwrite the install it raced', async () => {
    // The read the panel's own `noteWeightsMissing` refresh started, still in
    // the air when the user clicks Install one round trip later.
    const stale = deferred<Response>()
    stubModelReads(
      [sampleInstallableModel, stale.promise],
      jsonResponse(
        modelInstalling({ state: 'downloading', progress: 0.1 }),
        202,
      ),
    )
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    act(() => {
      result.current.refresh()
    })
    act(() => {
      result.current.install()
    })
    await settle()
    expect(result.current.model?.installation?.state).toBe('downloading')

    // The read answers with what was true before the install started.
    stale.resolve(jsonResponse(sampleInstallableModel))
    await settle()

    expect(
      result.current.model?.installation?.state,
      'the older answer does not un-start the download',
    ).toBe('downloading')
    expect(startBlockedReason(result.current)).toBe(
      'The model weights are still downloading.',
    )
  })

  it('a hung install for one tier still lets another be installed', async () => {
    const otherId = 'vocals-fast-001'
    const other: Model = { ...sampleInstallableModel, id: otherId }
    const installs: string[] = []
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith('/install')) {
        installs.push(url)
        // Never settles: the POST hangs.
        return new Promise<Response>(() => undefined)
      }
      return Promise.resolve(
        jsonResponse(url.includes(otherId) ? other : sampleInstallableModel),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useModelInstallation(id),
      { initialProps: { id: MODEL_ID } },
    )
    await settle()
    act(() => {
      result.current.install()
    })
    await settle()
    expect(installs).toHaveLength(1)
    expect(result.current.installing).toBe(true)

    rerender({ id: otherId })
    await settle()
    expect(
      result.current.installing,
      'the other tier is not the one with a request in flight',
    ).toBe(false)

    act(() => {
      result.current.install()
    })
    await settle()
    expect(installs).toHaveLength(2)
    expect(installs[1]).toContain(otherId)
  })
})

describe('useModelInstallation and a model_weights_missing job', () => {
  it('records the refusal and re-reads the model', async () => {
    const fetchMock = stubModelReads([
      modelInstalling({ state: 'installed' }),
      sampleInstallableModel,
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    expect(reads(fetchMock)).toBe(1)

    act(() => {
      result.current.noteWeightsMissing('The weights are not installed.')
    })
    expect(result.current.weightsMissingMessage).toBe(
      'The weights are not installed.',
    )

    await settle()
    expect(reads(fetchMock), 'the refusal triggered a re-read').toBe(2)
    // The re-read agrees, so the message stays: it explains the refused start.
    expect(result.current.weightsMissingMessage).toBe(
      'The weights are not installed.',
    )
    expect(result.current.model?.installation?.state).toBe('available')
  })

  it('drops the refusal as soon as a download supersedes it', async () => {
    stubModelReads(
      [sampleInstallableModel],
      jsonResponse(
        modelInstalling({ state: 'downloading', progress: 0.1 }),
        202,
      ),
    )
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()
    act(() => {
      result.current.noteWeightsMissing('The weights are not installed.')
    })
    await settle()

    act(() => {
      result.current.install()
    })
    await settle()

    expect(
      result.current.weightsMissingMessage,
      'a running download is newer news than the job that was refused',
    ).toBeNull()
    expect(result.current.model?.installation?.state).toBe('downloading')
  })

  it('drops the refusal when a read reports the weights installed', async () => {
    stubModelReads([
      modelInstalling({ state: 'installed' }),
      modelInstalling({ state: 'installed' }),
    ])
    const { result } = renderHook(() => useModelInstallation(MODEL_ID))
    await settle()

    act(() => {
      result.current.noteWeightsMissing('The weights are not installed.')
    })
    await settle()

    expect(result.current.weightsMissingMessage).toBeNull()
    expect(startBlockedReason(result.current)).toBeNull()
  })
})
