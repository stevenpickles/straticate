/**
 * Feature 065: the stem session belongs to the tracked job, not to the screen.
 *
 * Everything the player *does* with a session is pinned in
 * `components/StemPlayer.test.tsx`. What is pinned here is the session's
 * **lifetime**: what survives the Inspect UI being unmounted, and what — and
 * only what — takes it down.
 *
 * Feature 066 adds the view-persistence describe block near the bottom:
 * what gets written to `sessionStorage` at each commit, and what a *fresh*
 * provider — the unit-test stand-in for a page reload, since a reload tears
 * down every bit of JS state and leaves only what is on disk — restores from
 * it once its engine reaches `ready`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { StemSessionProvider, useStemSession } from './stemSession'
import { AppStateProvider, initialAppState } from './appState'
import { JobStateProvider, initialJobState, useJobDispatch } from './jobState'
import { readSessionSnapshot, writeViewSnapshot } from './persistence'
import { StemPlayer } from '../components/StemPlayer'
import type { Job, SeparationResult } from '../api/types'
import type {
  StemEngineSnapshot,
  StemPlayerEngine,
  StemSource,
} from '../audio/engine'
import { FakeAudioBuffer, stemBytesWithSamples } from '../test/fakeAudioContext'
import { installFakeCanvas } from '../test/fakeCanvasContext'
import { sampleAudioFile, sampleJob, sampleJobId } from '../test/fixtures'

/** Every stem in these fixtures is this long, so the readout is predictable. */
const STEM_SECONDS = 60

/** Width of the timeline strip, so `x → seconds` is exact arithmetic. */
const TIMELINE_WIDTH = 400

const stemNames = ['vocals', 'instrumental']

function resultFor(jobId: string): SeparationResult {
  return {
    job_id: jobId,
    model_id: 'vocals-hq-001',
    stems: stemNames.map((name) => ({
      name,
      duration_seconds: STEM_SECONDS,
      sample_rate_hz: 44100,
      channels: 2,
    })),
    metrics: { processing_seconds: 8, realtime_factor: 7.5 },
  }
}

const completedJob: Job = {
  ...sampleJob,
  state: 'completed',
  progress: 1,
  result: resultFor(sampleJobId),
}

/** A second completed job, for "a new job is a new session". */
const otherJobId = '11111111-2222-3333-4444-555555555555'
const otherJob: Job = {
  ...completedJob,
  id: otherJobId,
  result: resultFor(otherJobId),
}

/**
 * A short synthetic stem, authored in multiples of 1/128 so it survives the
 * fake codec's round trip: enough for the lanes to have something to draw.
 */
const SYNTHETIC_SAMPLES = Array.from(
  { length: 240 },
  (_, index) => Math.round(Math.sin((index / 240) * Math.PI * 2) * 96) / 128,
)

// ---------------------------------------------------------------------------
// A recording engine, counting the two things this suite is about: how often
// it was loaded, and whether it was ever disposed.
// ---------------------------------------------------------------------------

class SessionEngine implements StemPlayerEngine {
  loaded: StemSource[] = []
  loadCount = 0
  disposeCount = 0
  retryCalls = 0
  time = 0
  /** Every `seek()` call, in order — feature 066's "exactly one" restore. */
  seekCalls: number[] = []
  /** Every `setLoopRegion()` call, in order, for the same reason. */
  setLoopRegionCalls: Array<{ start: number; end: number }> = []

  private readonly listeners = new Set<() => void>()
  private readonly buffers = new Map<string, FakeAudioBuffer>()
  private snapshot: StemEngineSnapshot = {
    status: 'loading',
    stems: [],
    playing: false,
    durationSeconds: 0,
    loopRegion: null,
    scrubbing: false,
    error: null,
  }

  load = (sources: readonly StemSource[]): Promise<void> => {
    this.loadCount += 1
    this.loaded = [...sources]
    return Promise.resolve()
  }

  retryFailedStems = (): Promise<void> => {
    this.retryCalls += 1
    return Promise.resolve()
  }

  play = (): Promise<void> => {
    this.update({ playing: true })
    return Promise.resolve()
  }

  pause = (): void => {
    this.update({ playing: false })
  }

  seek = (seconds: number): void => {
    this.seekCalls.push(seconds)
    this.time = seconds
  }

  setMuted = (): void => undefined
  toggleMute = (): void => undefined
  setSoloed = (): void => undefined
  toggleSolo = (): void => undefined
  setLevel = (): void => undefined

  setLoopRegion = (startSeconds: number, endSeconds: number): void => {
    this.setLoopRegionCalls.push({ start: startSeconds, end: endSeconds })
    this.update({ loopRegion: { start: startSeconds, end: endSeconds } })
  }

  clearLoopRegion = (): void => {
    this.update({ loopRegion: null })
  }

  beginScrubPreview = (): void => {
    this.update({ scrubbing: true })
  }

  scrubPreview = (): void => undefined

  endScrubPreview = (commitSeconds?: number): void => {
    if (commitSeconds !== undefined) {
      this.time = commitSeconds
    }
    this.update({ scrubbing: false })
  }

  currentTime = (): number => this.time

  getStemBuffer = (name: string): FakeAudioBuffer | null =>
    this.buffers.get(name) ?? null

  getSnapshot = (): StemEngineSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  dispose = (): void => {
    this.disposeCount += 1
  }

  update(partial: Partial<StemEngineSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...partial }
    for (const listener of [...this.listeners]) {
      listener()
    }
  }

  /** Settle into "every stem decoded and playable". */
  becomeReady(): void {
    for (const name of stemNames) {
      this.buffers.set(
        name,
        new FakeAudioBuffer(stemBytesWithSamples(SYNTHETIC_SAMPLES)),
      )
    }
    act(() => {
      this.update({
        status: 'ready',
        durationSeconds: STEM_SECONDS,
        stems: stemNames.map((name) => ({
          name,
          status: 'loaded' as const,
          error: null,
          muted: false,
          soloed: false,
          audible: true,
          level: 1,
          durationSeconds: STEM_SECONDS,
        })),
      })
    })
  }

  /** Publish "one stem's audio failed, the rest is playable". */
  failOneStem(error: unknown): void {
    act(() => {
      this.update({
        status: 'ready',
        error,
        stems: [
          {
            name: 'vocals',
            status: 'loaded' as const,
            error: null,
            muted: false,
            soloed: false,
            audible: true,
            level: 1,
            durationSeconds: STEM_SECONDS,
          },
          {
            name: 'instrumental',
            status: 'error' as const,
            error,
            muted: false,
            soloed: false,
            audible: true,
            level: 1,
            durationSeconds: 0,
          },
        ],
      })
    })
  }
}

/** Hands out a fresh {@link SessionEngine} per call, keeping every one. */
function engineFactory(): {
  create: () => StemPlayerEngine
  engines: SessionEngine[]
} {
  const engines: SessionEngine[] = []
  return {
    create: () => {
      const engine = new SessionEngine()
      engines.push(engine)
      return engine
    },
    engines,
  }
}

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

let frames: Map<number, FrameRequestCallback>
let nextFrameId: number
let resultFetches: string[]

function stubAnimationFrames(): void {
  frames = new Map()
  nextFrameId = 0
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    nextFrameId += 1
    frames.set(nextFrameId, callback)
    return nextFrameId
  })
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    frames.delete(handle)
  })
}

/** A resize observer that never reports: the attach-time measure is enough. */
class SilentResizeObserver {
  observe(): void {
    // Nothing here resizes.
  }
  unobserve(): void {
    // Nothing here resizes.
  }
  disconnect(): void {
    // Nothing here resizes.
  }
}

function stubLayout(): void {
  vi.stubGlobal('ResizeObserver', SilentResizeObserver)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    width: TIMELINE_WIDTH,
    height: 200,
    top: 0,
    left: 0,
    right: TIMELINE_WIDTH,
    bottom: 200,
    toJSON: () => ({}),
  })
}

/**
 * Stub `fetch` so `GET /jobs/{id}/result` answers for whichever job is asked
 * about, recording every call — the re-download this feature exists to prevent
 * is counted here.
 */
function stubResultFetch(): void {
  resultFetches = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const path = String(url)
      const match = /\/jobs\/([^/]+)\/result$/u.exec(path)
      if (match?.[1] !== undefined) {
        resultFetches.push(match[1])
        return Promise.resolve(
          new Response(JSON.stringify(resultFor(match[1])), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        )
      }
      throw new Error(`unexpected fetch: ${path}`)
    }),
  )
}

beforeEach(() => {
  stubAnimationFrames()
  stubLayout()
  installFakeCanvas()
  stubResultFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
  // `stubLayout` and `installFakeCanvas` are `vi.spyOn`, which
  // `unstubAllGlobals` does not touch (feature 049, note 9).
  vi.restoreAllMocks()
  // Feature 066 writes to `sessionStorage`; without this a later test's
  // "fresh provider" would read the previous test's leftovers.
  sessionStorage.clear()
})

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/** The timeline, which is also the seek control. */
function timeline(): HTMLElement {
  return screen.getByRole('slider', { name: 'Seek' })
}

/** The x offset on the strip that means `seconds`. */
function xFor(seconds: number): number {
  return (seconds / STEM_SECONDS) * TIMELINE_WIDTH
}

/** One pointer gesture: press at `from`, drag through `path`, release. */
function dragTimeline(from: number, ...path: number[]): void {
  const surface = timeline()
  fireEvent.pointerDown(surface, { clientX: xFor(from), pointerId: 1 })
  for (const seconds of path) {
    fireEvent.pointerMove(surface, { clientX: xFor(seconds), pointerId: 1 })
  }
  fireEvent.pointerUp(surface, { pointerId: 1 })
}

/** The `{ zoom, scrollSeconds }` the strip is currently showing. */
function shownWindow(): { zoom: string | null; scroll: string | null } {
  const tracks = document.querySelector('.stem-timeline-tracks')
  return {
    zoom: tracks?.getAttribute('data-zoom') ?? null,
    scroll: tracks?.getAttribute('data-scroll-seconds') ?? null,
  }
}

/** Whether the Inspect UI is on screen, and the control that flips it. */
function InspectToggle() {
  const [showing, setShowing] = useState(true)
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setShowing((current) => !current)
        }}
      >
        {showing ? 'Leave inspect' : 'Enter inspect'}
      </button>
      {showing && <StemPlayer />}
    </>
  )
}

/**
 * Asks the session to fetch the result again for the same job — the session
 * side of feature 048's "Try again", reachable here without having to arrange
 * a failure first.
 */
function ResultRefetcher() {
  const { retryResult } = useStemSession()
  return (
    <button type="button" onClick={retryResult}>
      Refetch the result
    </button>
  )
}

/** Re-tracks a job, as `POST /jobs` or a WS reconnect might. */
function Retracker({ job, label }: { job: Job; label: string }) {
  const dispatch = useJobDispatch()
  return (
    <button
      type="button"
      onClick={() => {
        dispatch({ type: 'job/track', job })
      }}
    >
      {label}
    </button>
  )
}

const SETTLE = { timeout: 10_000 }

/**
 * Mount a session with the player on screen and every stem decoded. `children`
 * is rendered inside the session, beside the player.
 */
async function renderSession(
  create: () => StemPlayerEngine,
  children?: React.ReactNode,
  job: Job = completedJob,
) {
  const view = render(
    <AppStateProvider
      initialState={{
        ...initialAppState,
        phase: 'inspect',
        upload: { status: 'uploaded', file: sampleAudioFile },
      }}
    >
      <JobStateProvider initialState={{ ...initialJobState, job }}>
        <StemSessionProvider createEngine={create}>
          <InspectToggle />
          {children}
        </StemSessionProvider>
      </JobStateProvider>
    </AppStateProvider>,
  )
  await screen.findByRole('button', { name: 'Mute vocals' }, SETTLE)
  await act(async () => {})
  return view
}

/** Let the decoded stems arrive and the transport go live. */
async function becomeReady(engine: SessionEngine): Promise<void> {
  engine.becomeReady()
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
  }, SETTLE)
  // The peak computations are chunked and therefore async; without this the
  // lanes paint after the test has finished.
  await act(async () => {})
}

async function leaveInspect(): Promise<void> {
  await userEvent.click(screen.getByRole('button', { name: 'Leave inspect' }))
  expect(screen.queryByRole('region', { name: 'Stem player' })).toBeNull()
}

async function enterInspect(): Promise<void> {
  await userEvent.click(screen.getByRole('button', { name: 'Enter inspect' }))
  await act(async () => {})
}

// ---------------------------------------------------------------------------

describe('StemSessionProvider lazy open', () => {
  it('fetches nothing for a tracked job nobody inspects', async () => {
    const { create, engines } = engineFactory()
    render(
      <AppStateProvider
        initialState={{ ...initialAppState, phase: 'separate' }}
      >
        <JobStateProvider
          initialState={{ ...initialJobState, job: completedJob }}
        >
          <StemSessionProvider createEngine={create}>
            <p>no player here</p>
          </StemSessionProvider>
        </JobStateProvider>
      </AppStateProvider>,
    )
    await act(async () => {})

    // A completed job is tracked, and not one byte has been asked for: the
    // session opens when a view opens it, and nothing else does.
    expect(resultFetches).toEqual([])
    expect(engines).toHaveLength(0)
  })

  it('fetches once the player opens the session', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)

    expect(resultFetches).toEqual([sampleJobId])
    expect(engines).toHaveLength(1)
    expect(engines[0]?.loaded.map((source) => source.name)).toEqual(stemNames)
  })
})

describe('StemSessionProvider survives the Inspect UI', () => {
  it('keeps the engine and the result when the player unmounts', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]
    await becomeReady(engine!)

    await leaveInspect()

    // The headline. Nothing about a view going away is a reason to stop the
    // audio, drop the buffers or close the context.
    expect(engine?.disposeCount).toBe(0)
    expect(resultFetches).toEqual([sampleJobId])

    await enterInspect()

    // Same session, same instance — not a rebuilt one that merely looks alike.
    expect(engines).toHaveLength(1)
    expect(engines[0]).toBe(engine)
    expect(engine?.loadCount).toBe(1)
    expect(resultFetches).toEqual([sampleJobId])
  })

  it('comes back at the playhead it was left at', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]
    await becomeReady(engine!)

    dragTimeline(12)
    expect(screen.getByText('0:12 / 1:00')).toBeInTheDocument()

    await leaveInspect()
    await enterInspect()

    // The readout is seeded from the engine's clock rather than from zero,
    // which is what makes re-entry land where the user left off.
    expect(screen.getByText('0:12 / 1:00')).toBeInTheDocument()
  })

  it('comes back with the loop region still set', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]
    await becomeReady(engine!)

    const ruler = screen.getByTestId('stem-timeline-ruler-row')
    fireEvent.pointerDown(ruler, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: xFor(35), pointerId: 1 })
    fireEvent.pointerUp(ruler, { pointerId: 1 })
    expect(document.querySelector('.stem-player-loop-badge')?.textContent).toBe(
      'Loop 0:10 – 0:35',
    )

    await leaveInspect()
    await enterInspect()

    expect(document.querySelector('.stem-player-loop-badge')?.textContent).toBe(
      'Loop 0:10 – 0:35',
    )
  })

  it('comes back looking at the same window', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)
    expect(shownWindow().zoom).toBe('1')

    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    const zoomed = shownWindow()
    expect(Number(zoomed.zoom)).toBeGreaterThan(1)

    await leaveInspect()
    await enterInspect()

    // The window lives in the session's store, so the hook seeds from it
    // rather than reopening on the whole file.
    expect(shownWindow()).toEqual(zoomed)
  })
})

describe('StemSessionProvider disposal', () => {
  it('disposes and resets when the job is cleared', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]
    await becomeReady(engine!)

    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )

    // `job/clear` is the end of the session: nothing is tracked, so there is
    // nothing to play and no reason to hold ~340 MB of decoded audio.
    expect(engine?.disposeCount).toBe(1)
    expect(
      screen.getByText('No separation job is being tracked.'),
    ).toBeInTheDocument()
  })

  it('disposes the old session and opens a fresh one for a different job', async () => {
    const { create, engines } = engineFactory()
    await renderSession(
      create,
      <Retracker job={otherJob} label="Track other job" />,
    )
    const first = engines[0]
    await becomeReady(first!)
    dragTimeline(30)
    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(Number(shownWindow().zoom)).toBeGreaterThan(1)

    await userEvent.click(
      screen.getByRole('button', { name: 'Track other job' }),
    )
    await screen.findByRole('button', { name: 'Mute vocals' }, SETTLE)
    await act(async () => {})

    expect(first?.disposeCount).toBe(1)
    expect(engines).toHaveLength(2)
    expect(engines[1]).not.toBe(first)
    expect(resultFetches).toEqual([sampleJobId, otherJobId])
    expect(engines[1]?.loaded.map((source) => source.url)).toEqual(
      stemNames.map((name) => `/api/v1/jobs/${otherJobId}/stems/${name}`),
    )
    // A new job is a new timeline: the window store resets with the session.
    expect(shownWindow().zoom).toBe('1')
    expect(shownWindow().scroll).toBe('0')
  })

  it('disposes when the whole tree goes away', async () => {
    const { create, engines } = engineFactory()
    const view = await renderSession(create)
    await becomeReady(engines[0]!)

    view.unmount()

    expect(engines[0]?.disposeCount).toBe(1)
  })

  it('defers a job change that happens while the player is unmounted', async () => {
    // The exact scenario the `openedFor: string | null` keying exists for
    // (review finding): with a boolean, a job change while the Inspect UI is
    // away would fetch and build eagerly for a job nobody is looking at.
    const { create, engines } = engineFactory()
    await renderSession(
      create,
      <Retracker job={otherJob} label="Track other job" />,
    )
    await becomeReady(engines[0]!)
    await userEvent.click(screen.getByRole('button', { name: 'Leave inspect' }))

    await userEvent.click(
      screen.getByRole('button', { name: 'Track other job' }),
    )
    await act(async () => {})

    // Nothing happens while unmounted: the old engine is disposed with its
    // job, and the new one costs no network and no decode until re-entry.
    expect(engines[0]?.disposeCount).toBe(1)
    expect(engines).toHaveLength(1)
    expect(resultFetches).toEqual([sampleJobId])

    await userEvent.click(screen.getByRole('button', { name: 'Enter inspect' }))
    await screen.findByRole('button', { name: 'Mute vocals' }, SETTLE)
    await act(async () => {})

    expect(resultFetches).toEqual([sampleJobId, otherJobId])
    expect(engines).toHaveLength(2)
  })
})

describe('StemSessionProvider result retry (feature 048) on one engine', () => {
  it('builds the engine only once the result finally arrives', async () => {
    const { create, engines } = engineFactory()
    // The first fetch fails, so the error branch offers "Try again"; the
    // second answers with a real result for the same job.
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (!String(url).endsWith('/result')) {
          throw new Error(`unexpected fetch: ${String(url)}`)
        }
        call += 1
        return call === 1
          ? Promise.reject(new TypeError('network error'))
          : Promise.resolve(
              new Response(JSON.stringify(resultFor(sampleJobId)), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
              }),
            )
      }),
    )

    render(
      <AppStateProvider
        initialState={{
          ...initialAppState,
          phase: 'inspect',
          upload: { status: 'uploaded', file: sampleAudioFile },
        }}
      >
        <JobStateProvider
          initialState={{ ...initialJobState, job: completedJob }}
        >
          <StemSessionProvider createEngine={create}>
            <StemPlayer />
          </StemSessionProvider>
        </JobStateProvider>
      </AppStateProvider>,
    )

    await screen.findByRole('alert')
    // A result that never arrived has no stems to download and no graph to
    // build, so there is no engine to have to dispose either.
    expect(engines).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await screen.findByRole('button', { name: 'Mute vocals' }, SETTLE)
    await act(async () => {})

    expect(engines).toHaveLength(1)
    expect(engines[0]?.loadCount).toBe(1)
    expect(call).toBe(2)
  })

  it('reloads the one instance when the same job answers again', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create, <ResultRefetcher />)
    const engine = engines[0]
    await becomeReady(engine!)
    expect(engine?.loadCount).toBe(1)

    // A refetch of the same job hands the session a *new* result identity —
    // which is what a 048 retry does once it succeeds. That is a reload of the
    // engine that is already here, not a new one.
    await userEvent.click(
      screen.getByRole('button', { name: 'Refetch the result' }),
    )
    await waitFor(() => {
      expect(engine?.loadCount).toBe(2)
    }, SETTLE)
    await act(async () => {})

    expect(resultFetches).toEqual([sampleJobId, sampleJobId])
    expect(engines).toHaveLength(1)
    expect(engines[0]).toBe(engine)
    expect(engine?.disposeCount).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Feature 066: the view survives a reload.
//
// A page reload tears down every bit of JS state and starts a fresh provider
// over whatever `sessionStorage` holds — which is exactly what mounting a
// second, independent session over the same (real, jsdom) storage simulates.
// Nothing here reuses a provider or an engine instance across the "reload":
// doing so would test something a reload can never actually do.
// ---------------------------------------------------------------------------

describe('StemSessionProvider view restore (feature 066)', () => {
  it('restores the playhead and loop region exactly once, once the engine is ready', async () => {
    writeViewSnapshot({
      jobId: sampleJobId,
      positionSeconds: 24,
      loopStart: 10,
      loopEnd: 30,
      zoom: 1,
      scrollSeconds: 0,
    })

    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]!
    await becomeReady(engine)

    expect(engine.seekCalls).toEqual([24])
    expect(engine.setLoopRegionCalls).toEqual([{ start: 10, end: 30 }])
    expect(screen.getByText('0:24 / 1:00')).toBeInTheDocument()
    expect(document.querySelector('.stem-player-loop-badge')?.textContent).toBe(
      'Loop 0:10 – 0:30',
    )

    // A later snapshot notification — Play toggles `playing`, which every
    // stem's mute/solo does too — must not repeat the restore: 023's "one
    // seek" invariant extends to "one restore per page load".
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    expect(engine.seekCalls).toEqual([24])
    expect(engine.setLoopRegionCalls).toEqual([{ start: 10, end: 30 }])
  })

  it('restores the playhead alone when the view carried no loop region', async () => {
    writeViewSnapshot({
      jobId: sampleJobId,
      positionSeconds: 24,
      loopStart: null,
      loopEnd: null,
      zoom: 1,
      scrollSeconds: 0,
    })

    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    expect(engines[0]?.seekCalls).toEqual([24])
    expect(engines[0]?.setLoopRegionCalls).toEqual([])
  })

  it('restores nothing when nothing was persisted', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    expect(engines[0]?.seekCalls).toEqual([])
    expect(engines[0]?.setLoopRegionCalls).toEqual([])
    expect(screen.getByText('0:00 / 1:00')).toBeInTheDocument()
    expect(document.querySelector('.stem-player-loop-badge')).toBeNull()
  })

  it('drops a view recorded for a different job', async () => {
    writeViewSnapshot({
      jobId: otherJobId,
      positionSeconds: 24,
      loopStart: 10,
      loopEnd: 30,
      zoom: 1,
      scrollSeconds: 0,
    })

    const { create, engines } = engineFactory()
    // The persisted view names `otherJobId`; the session opens for
    // `sampleJobId` (the default `renderSession` job) — a mismatch exactly
    // as stale as an unknown job id, dropped the same way.
    await renderSession(create)
    await becomeReady(engines[0]!)

    expect(engines[0]?.seekCalls).toEqual([])
    expect(engines[0]?.setLoopRegionCalls).toEqual([])
  })

  it('seeds the window store before the first StemTimeline mount', async () => {
    writeViewSnapshot({
      jobId: sampleJobId,
      positionSeconds: 0,
      loopStart: null,
      loopEnd: null,
      zoom: 2.5,
      scrollSeconds: 5,
    })

    const { create } = engineFactory()
    // Deliberately no `becomeReady()`: the window has nothing to do with the
    // engine, and this is already on the strip once `renderSession` settles.
    await renderSession(create)

    expect(shownWindow()).toEqual({ zoom: '2.5', scroll: '5' })
  })
})

describe('StemSessionProvider view commits (feature 066)', () => {
  it('persists a seek commit, keyed by the tracked job', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    dragTimeline(12)

    expect(readSessionSnapshot().view).toMatchObject({
      jobId: sampleJobId,
      positionSeconds: 12,
      loopStart: null,
      loopEnd: null,
    })
  })

  it('persists a loop set and a loop clear', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    const ruler = screen.getByTestId('stem-timeline-ruler-row')
    fireEvent.pointerDown(ruler, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: xFor(35), pointerId: 1 })
    fireEvent.pointerUp(ruler, { pointerId: 1 })

    // `xToTime` pixel math, not the exact arithmetic `xFor` used to place the
    // drag — a fraction of a second of slack either way.
    expect(readSessionSnapshot().view?.loopStart).toBeCloseTo(10, 5)
    expect(readSessionSnapshot().view?.loopEnd).toBeCloseTo(35, 5)

    await userEvent.click(screen.getByRole('button', { name: 'Clear loop' }))

    expect(readSessionSnapshot().view).toMatchObject({
      loopStart: null,
      loopEnd: null,
    })
  })

  it('writes nothing to sessionStorage on a viewport move alone (post-review should-fix)', async () => {
    // `windowStore.set` used to call `persistView()` unconditionally, and it
    // is reached by every pan/zoom/thumb-drag/auto-follow event — on the
    // order of 100/s on a trackpad wheel. "Zoom in" is this suite's
    // stand-in for one of those events; a click is one call into the same
    // `set`, so it pins the fix regardless of how many times a real gesture
    // would call it.
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    const setItemSpy = vi.spyOn(window.sessionStorage, 'setItem')

    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(Number(shownWindow().zoom)).toBeGreaterThan(1)

    // Fail-first: restore the write-through in `windowStore.set` and this is
    // what fails — `setItemSpy` gets called and `view` stops being `null`.
    expect(setItemSpy).not.toHaveBeenCalled()
    expect(readSessionSnapshot().view).toBeNull()

    setItemSpy.mockRestore()
  })

  it('captures the current window on the pagehide flush, with no viewport commit of its own', async () => {
    // The window moves without ever persisting on its own (previous test);
    // this is the write path that catches it up before a reload — the same
    // `pagehide` flush that already covers "reload while playing" also
    // covers "reload right after zooming/panning".
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    const zoomed = shownWindow()
    expect(readSessionSnapshot().view).toBeNull()

    window.dispatchEvent(new Event('pagehide'))

    const persisted = readSessionSnapshot().view
    expect(persisted?.jobId).toBe(sampleJobId)
    expect(String(persisted?.zoom)).toBe(zoomed.zoom)
    expect(String(persisted?.scrollSeconds)).toBe(zoomed.scroll)
  })

  it('persists a pause', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    expect(readSessionSnapshot().view).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(readSessionSnapshot().view).not.toBeNull()
  })

  it('flushes the live position on pagehide, ahead of the last discrete commit', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    const engine = engines[0]!
    await becomeReady(engine)

    dragTimeline(10)
    expect(readSessionSnapshot().view?.positionSeconds).toBeCloseTo(10, 5)

    // Playback carries the clock on without a discrete commit — the "reload
    // while playing" case the flush exists for. The fake's `currentTime()`
    // just returns `time`, so setting it directly is the fake's stand-in for
    // the audio clock having moved on its own.
    engine.time = 45
    window.dispatchEvent(new Event('pagehide'))

    expect(readSessionSnapshot().view?.positionSeconds).toBe(45)
  })

  it('wipes the persisted view when the job is cleared', async () => {
    const { create, engines } = engineFactory()
    await renderSession(create)
    await becomeReady(engines[0]!)

    dragTimeline(12)
    expect(readSessionSnapshot().view).not.toBeNull()

    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )

    expect(readSessionSnapshot().view).toBeNull()
  })
})
