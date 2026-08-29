import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StemPlayer } from './StemPlayer'
import { ApiError } from '../api/client'
import {
  AppStateProvider,
  initialAppState,
  useAppState,
  type AppState,
} from '../state/appState'
import {
  JobStateProvider,
  initialJobState,
  useJobDispatch,
  useJobState,
  type JobStateValue,
} from '../state/jobState'
import type { Job, SeparationResult, Stem } from '../api/types'
import {
  createStemAudioEngine,
  type LoopRegion,
  type StemEngineSnapshot,
  type StemPlayerEngine,
  type StemSource,
} from '../audio/engine'
import {
  FakeAudioBuffer,
  FakeAudioContext,
  stemBytes,
  stemBytesWithSamples,
} from '../test/fakeAudioContext'
import { installFakeCanvas } from '../test/fakeCanvasContext'
import { sampleAudioFile, sampleJob, sampleJobId } from '../test/fixtures'

/** Every stem in these fixtures is this long, so the readout is predictable. */
const STEM_SECONDS = 60

function stem(name: string): Stem {
  return {
    name,
    duration_seconds: STEM_SECONDS,
    sample_rate_hz: 44100,
    channels: 2,
  }
}

/** A result over exactly the stem names given — two of them or four. */
function resultOver(names: readonly string[]): SeparationResult {
  return {
    job_id: sampleJobId,
    model_id: 'vocals-hq-001',
    stems: names.map(stem),
    metrics: { processing_seconds: 8, realtime_factor: 7.5 },
  }
}

const twoStemNames = ['vocals', 'instrumental']
const fourStemNames = ['vocals', 'drums', 'bass', 'other']

const completedJob: Job = {
  ...sampleJob,
  state: 'completed',
  progress: 1,
  result: resultOver(twoStemNames),
}

// ---------------------------------------------------------------------------
// Animation frames
//
// The playhead readout repaints on animation frames (its *value* comes from
// the audio clock). jsdom would run them on a real timer, so tests drive them
// by hand: nothing here ever waits on wall-clock time.
// ---------------------------------------------------------------------------

let frames: Map<number, FrameRequestCallback>
let nextFrameId: number

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

/** Run every pending animation frame callback once. */
function flushFrame(): void {
  const pending = [...frames.values()]
  frames.clear()
  act(() => {
    for (const callback of pending) {
      callback(0)
    }
  })
}

// ---------------------------------------------------------------------------
// Layout
//
// The timeline maps pixels to seconds, so jsdom — which lays nothing out and
// reports every box as 0×0 — has to be told how wide the strip is. One fixed
// rect for every element makes `x → seconds` exact arithmetic, and the
// `ResizeObserver` stub stands in for the API jsdom does not implement.
// ---------------------------------------------------------------------------

/** Width of the timeline strip in every test here. */
const TIMELINE_WIDTH = 400

/** A resize observer whose callbacks a test delivers by hand. */
class FakeResizeObserver {
  static readonly instances: FakeResizeObserver[] = []
  readonly targets: Element[] = []
  private readonly callback: ResizeObserverCallback

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeResizeObserver.instances.push(this)
  }

  observe(target: Element): void {
    this.targets.push(target)
  }

  unobserve(): void {
    // Nothing here observes twice.
  }

  disconnect(): void {
    this.targets.length = 0
  }

  /** Report a new width to every live observer, as a real resize would. */
  static resizeTo(width: number): void {
    act(() => {
      for (const observer of FakeResizeObserver.instances) {
        for (const target of observer.targets) {
          observer.callback(
            [
              {
                target,
                contentRect: { width, height: 200 },
              } as unknown as ResizeObserverEntry,
            ],
            observer as unknown as ResizeObserver,
          )
        }
      }
    })
  }
}

function stubLayout(): void {
  FakeResizeObserver.instances.length = 0
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
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

/**
 * Ceiling for the two waits that depend on a promise rather than on React's
 * own queue: the result fetch, and the engine reaching `ready`. Still a
 * condition, not a sleep — a passing run never spends any of it. RTL's 1 s
 * default is the one thing here that a loaded machine can genuinely exceed
 * (Playwright's config raises its own budget to 20 s for the same reason).
 */
const SETTLE = { timeout: 10_000 }

/** Every lane header's stem name, in render order. */
function laneNames(): string[] {
  return [...document.querySelectorAll('.stem-player-stem-name')].map(
    (element) => element.textContent ?? '',
  )
}

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

/** Snapshot of a stem in whatever state the test needs. */
function stemState(
  name: string,
  overrides: Partial<StemEngineSnapshot['stems'][number]> = {},
) {
  return {
    name,
    status: 'loaded' as const,
    error: null,
    muted: false,
    soloed: false,
    audible: true,
    level: 1,
    durationSeconds: STEM_SECONDS,
    ...overrides,
  }
}

/**
 * A short synthetic stem: one cycle of a sine, authored in multiples of
 * 1/128 so it survives the fake codec's round trip. Enough for the peak
 * computation to produce a real envelope, small enough to cost nothing.
 */
const SYNTHETIC_SAMPLES = Array.from(
  { length: 240 },
  (_, index) => Math.round(Math.sin((index / 240) * Math.PI * 2) * 96) / 128,
)

/**
 * A recording stand-in for the audio engine. The component must not know how
 * playback works, so most tests assert only that the user's intent reached
 * the engine; the mute/solo *semantics* are exercised against the real engine
 * further down, and in `audio/engine.test.ts`.
 */
class FakeEngine implements StemPlayerEngine {
  loaded: StemSource[] = []
  playCount = 0
  pauseCount = 0
  readonly seeks: number[] = []
  /**
   * Every *real* transport move, in the order it happened, whichever call
   * made it: a discrete `seek`, or a drag committing through
   * `endScrubPreview`. The one-move-per-gesture invariant is about this list,
   * not about either call on its own.
   */
  readonly moves: number[] = []
  /** How many preview sessions were opened (feature 052). */
  scrubStarts = 0
  /** Every position auditioned, in order. */
  readonly scrubPreviews: number[] = []
  /** Every session end, in order: the committed position or `undefined`. */
  readonly scrubEnds: (number | undefined)[] = []
  readonly muteToggles: string[] = []
  readonly soloToggles: string[] = []
  readonly levelSets: { name: string; value: number }[] = []
  /** Every loop commit, in order: a region, or `null` for a clear. */
  readonly loopRegions: (LoopRegion | null)[] = []
  disposeCount = 0
  time = 0

  private readonly listeners = new Set<() => void>()
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
    this.loaded = [...sources]
    return Promise.resolve()
  }

  play = (): Promise<void> => {
    this.playCount += 1
    this.update({ playing: true })
    return Promise.resolve()
  }

  pause = (): void => {
    this.pauseCount += 1
    this.update({ playing: false })
  }

  seek = (seconds: number): void => {
    this.seeks.push(seconds)
    this.moves.push(seconds)
    this.time = seconds
  }

  setMuted = (name: string): void => {
    this.muteToggles.push(name)
  }

  toggleMute = (name: string): void => {
    this.muteToggles.push(name)
  }

  setSoloed = (name: string): void => {
    this.soloToggles.push(name)
  }

  toggleSolo = (name: string): void => {
    this.soloToggles.push(name)
  }

  setLevel = (name: string, value: number): void => {
    this.levelSets.push({ name, value })
  }

  /**
   * Records the commit *and* publishes it, exactly as the real engine's
   * synchronous notify does — the badge and the overlay both read the region
   * back out of the snapshot rather than from the call.
   */
  setLoopRegion = (startSeconds: number, endSeconds: number): void => {
    const region = { start: startSeconds, end: endSeconds }
    this.loopRegions.push(region)
    this.update({ loopRegion: region })
  }

  clearLoopRegion = (): void => {
    this.loopRegions.push(null)
    this.update({ loopRegion: null })
  }

  beginScrubPreview = (): void => {
    this.scrubStarts += 1
    this.update({ scrubbing: true })
  }

  scrubPreview = (seconds: number): void => {
    this.scrubPreviews.push(seconds)
  }

  /**
   * Records the end *and*, when a position was committed, moves the playhead
   * — because `endScrubPreview(commit)` **is** the gesture's seek, not a
   * bookkeeping call alongside one.
   */
  endScrubPreview = (commitSeconds?: number): void => {
    this.scrubEnds.push(commitSeconds)
    if (commitSeconds !== undefined) {
      this.moves.push(commitSeconds)
      this.time = commitSeconds
    }
    this.update({ scrubbing: false })
  }

  currentTime = (): number => this.time

  /**
   * Decoded audio per stem, so the waveform lanes have something real to
   * reduce. Filled by {@link FakeEngine.becomeReady}, exactly as the real
   * engine fills its entries when a decode finishes.
   */
  private readonly buffers = new Map<string, FakeAudioBuffer>()

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

  /** Push a new snapshot and notify, as the real engine does. */
  update(partial: Partial<StemEngineSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...partial }
    for (const listener of [...this.listeners]) {
      listener()
    }
  }

  /** Settle into "every stem decoded and playable". */
  becomeReady(names: readonly string[]): void {
    for (const name of names) {
      this.buffers.set(
        name,
        new FakeAudioBuffer(stemBytesWithSamples(SYNTHETIC_SAMPLES)),
      )
    }
    act(() => {
      this.update({
        status: 'ready',
        durationSeconds: STEM_SECONDS,
        stems: names.map((name) => stemState(name)),
      })
    })
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/** Stub `fetch` so `GET /jobs/{id}/result` answers with `response`. */
function stubResultFetch(response: Response | Promise<Response>): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (String(url).endsWith('/result')) {
        return Promise.resolve(response)
      }
      throw new Error(`unexpected fetch: ${String(url)}`)
    }),
  )
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function errorResponse(
  status: number,
  code: string,
  message: string,
  detail?: unknown,
): Response {
  return jsonResponse({ error: { code, message, detail } }, status)
}

/**
 * Stub `fetch` so successive calls to `GET /jobs/{id}/result` answer with
 * different outcomes in order — e.g. a rejection (dropped request) followed
 * by a resolved response, which is what a "Try again" click needs to
 * recover from. The last responder repeats for any further calls.
 */
function stubResultFetchQueue(
  first: () => Promise<Response>,
  ...rest: readonly (() => Promise<Response>)[]
): void {
  const responders = [first, ...rest]
  let call = 0
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (String(url).endsWith('/result')) {
        const index = Math.min(call, responders.length - 1)
        call += 1
        // `first` is a safe fallback: `index` is always in bounds, so the
        // lookup never actually falls through to it at runtime.
        return (responders[index] ?? first)()
      }
      throw new Error(`unexpected fetch: ${String(url)}`)
    }),
  )
}

/** A promise plus the function that settles it, for controlling fetch timing. */
function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
} {
  let resolve: (value: T) => void = () => undefined
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

/** Surfaces the workflow phase and the tracked job, for the route-out tests. */
function WorkflowState() {
  const { phase } = useAppState()
  const { job } = useJobState()
  return (
    <>
      <p data-testid="workflow-phase">{phase}</p>
      <p data-testid="tracked-job">{job?.id ?? 'none'}</p>
    </>
  )
}

/** Application state as it stands in the `inspect` phase. */
function inspectingState(overrides: Partial<AppState> = {}): AppState {
  return {
    ...initialAppState,
    phase: 'inspect',
    upload: { status: 'uploaded', file: sampleAudioFile },
    ...overrides,
  }
}

function renderPlayer(
  engine: StemPlayerEngine,
  jobState: Partial<JobStateValue> = { job: completedJob },
  appState: AppState = inspectingState(),
) {
  const createEngine = () => engine
  return render(
    <AppStateProvider initialState={appState}>
      <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
        <StemPlayer createEngine={createEngine} />
        <WorkflowState />
      </JobStateProvider>
    </AppStateProvider>,
  )
}

/**
 * Render with a result already served and every stem decoded, waiting until
 * the transport is live. The explicit flush is what lets the engine's mount
 * effect (create, load, subscribe) settle before the test drives it — no
 * timers, just React's own work queue.
 */
async function renderReady(
  names: readonly string[],
): Promise<{ engine: FakeEngine }> {
  stubResultFetch(jsonResponse(resultOver(names)))
  const engine = new FakeEngine()
  renderPlayer(engine)
  await screen.findByRole(
    'button',
    { name: `Mute ${String(names[0])}` },
    SETTLE,
  )
  await act(async () => {})
  engine.becomeReady(names)
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
  }, SETTLE)
  // Let the peak computations settle: they are chunked and therefore async,
  // and a lane that paints after the test has finished is a stray act warning.
  await act(async () => {})
  return { engine }
}

/**
 * The fake context behind {@link renderReal}. Module-scoped so the tests that
 * assert on *what was scheduled* can reach it wherever they live.
 */
let context: FakeAudioContext

/** Gain values in stem order, which is the engine's node order. */
function gains(): number[] {
  return context.gains.map((gain) => gain.gain.value)
}

/**
 * Render over the **real** engine with a fake `AudioContext` underneath, so a
 * test exercises the audio graph the user actually gets rather than a double.
 */
async function renderReal(names: readonly string[]): Promise<StemPlayerEngine> {
  context = new FakeAudioContext()
  const engine = createStemAudioEngine({
    createContext: () => context,
    loadStemAudio: () => Promise.resolve(stemBytes(STEM_SECONDS)),
    lookaheadSeconds: 0,
  })
  stubResultFetch(jsonResponse(resultOver(names)))
  renderPlayer(engine)
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
  }, SETTLE)
  await act(async () => {})
  return engine
}

/** The recording 2D context every lane in a test draws through. */
let canvas: ReturnType<typeof installFakeCanvas>

beforeEach(() => {
  stubAnimationFrames()
  stubLayout()
  canvas = installFakeCanvas()
})

afterEach(() => {
  vi.unstubAllGlobals()
  // `stubLayout` and `installFakeCanvas` are `vi.spyOn`, which
  // `unstubAllGlobals` does not touch (feature 049, note 9).
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------

describe('StemPlayer stem lanes', () => {
  it('renders one lane per stem of a two-stem result', async () => {
    await renderReady(twoStemNames)

    expect(laneNames()).toEqual(twoStemNames)
    expect(document.querySelectorAll('canvas')).toHaveLength(2)
    for (const name of twoStemNames) {
      expect(
        screen.getByRole('button', { name: `Mute ${name}` }),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: `Solo ${name}` }),
      ).toBeInTheDocument()
    }
  })

  it('renders one lane per stem of a four-stem result', async () => {
    await renderReady(fourStemNames)

    expect(laneNames()).toEqual(fourStemNames)
    expect(document.querySelectorAll('canvas')).toHaveLength(4)
    for (const name of fourStemNames) {
      expect(
        screen.getByRole('button', { name: `Solo ${name}` }),
      ).toBeInTheDocument()
    }
  })

  it('draws every lane from the engine’s decoded audio', async () => {
    await renderReady(twoStemNames)

    // Coarse by design: that a waveform was painted in the accent colour, not
    // which pixels it covered. `timelineGeometry.test.ts` pins the geometry.
    expect(canvas.fillRects.length).toBeGreaterThan(0)
    expect(canvas.fillRects.every((rect) => rect.fillStyle === '#7aa2f7')).toBe(
      true,
    )
  })

  it('redraws a silenced lane in the muted colour', async () => {
    const { engine } = await renderReady(twoStemNames)
    canvas.reset()

    act(() => {
      engine.update({
        stems: [
          stemState('vocals'),
          stemState('instrumental', { muted: true, audible: false }),
        ],
      })
    })

    const silenced = canvas.fillRects.filter(
      (rect) => rect.fillStyle === '#9a9aa5',
    )
    expect(silenced.length).toBeGreaterThan(0)
  })

  it('gives a stem whose audio failed a placeholder instead of a canvas', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        stems: [
          stemState('vocals'),
          stemState('instrumental', { status: 'error' }),
        ],
      })
    })

    expect(
      document.querySelector('.stem-timeline-lane-placeholder'),
    ).toHaveTextContent('Unavailable')
    // One lane fewer than stems: the failed one has nothing to draw.
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
  })

  it('repaints the lanes when the strip is resized, not on every frame', async () => {
    await renderReady(twoStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    canvas.reset()

    flushFrame()
    flushFrame()
    expect(canvas.fillRects).toHaveLength(0)

    FakeResizeObserver.resizeTo(200)

    expect(canvas.fillRects.length).toBeGreaterThan(0)
  })

  it('loads every stem from its own streaming URL', async () => {
    const { engine } = await renderReady(fourStemNames)

    expect(engine.loaded).toEqual(
      fourStemNames.map((name) => ({
        name,
        url: `/api/v1/jobs/${sampleJobId}/stems/${name}`,
      })),
    )
  })

  it('shows each stem as unavailable when its audio failed to load', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        stems: [
          stemState('vocals'),
          stemState('instrumental', { status: 'error' }),
        ],
      })
    })

    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Mute instrumental' }),
    ).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Mute vocals' })).toBeEnabled()
  })
})

describe('StemPlayer loading and error states', () => {
  it('says so while the result is still being fetched', () => {
    stubResultFetch(new Promise<Response>(() => undefined))
    renderPlayer(new FakeEngine())

    expect(
      screen.getByText('Loading the separation result…'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Play' })).toBeNull()
  })

  it('says so while the stems are decoding, with the transport disabled', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(new FakeEngine())

    expect(await screen.findByText('Decoding stems…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Play' })).toBeDisabled()
    // The timeline is a div, so it says so the way a div can.
    expect(timeline()).toHaveAttribute('aria-disabled', 'true')
  })

  it('ignores a pointer gesture until the stems are ready', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    const engine = new FakeEngine()
    renderPlayer(engine)
    await screen.findByText('Decoding stems…')

    dragTimeline(10, 20)

    expect(engine.seeks).toEqual([])
  })

  it('explains a 409 for a job that is still separating', async () => {
    stubResultFetch(
      errorResponse(409, 'result_not_available', 'No result yet.', {
        job_id: sampleJobId,
        state: 'separating',
      }),
    )
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The stems are not ready yet — this job is separating.',
    )
  })

  it('explains a 409 for a cancelled job', async () => {
    stubResultFetch(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'cancelled',
      }),
    )
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This separation was cancelled, so there are no stems to play.',
    )
  })

  it('explains a 409 for a failed job', async () => {
    stubResultFetch(
      errorResponse(409, 'result_not_available', 'No result.', {
        job_id: sampleJobId,
        state: 'failed',
      }),
    )
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This separation failed, so there are no stems to play.',
    )
  })

  it('explains a 409 that carries no state', async () => {
    stubResultFetch(errorResponse(409, 'result_not_available', 'No result.'))
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The stems are not ready yet.',
    )
  })

  it('explains a 404 for a job the backend has forgotten', async () => {
    stubResultFetch(errorResponse(404, 'job_not_found', 'No such job.'))
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The backend no longer knows about this job. Run the separation again.',
    )
  })

  it('falls back to the envelope message for an unexpected code', async () => {
    stubResultFetch(errorResponse(503, 'service_unavailable', 'Shutting down.'))
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent('Shutting down.')
  })

  it('explains a 404 stem_file_missing raised while loading the audio', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        status: 'error',
        error: new ApiError(404, {
          code: 'stem_file_missing',
          message: 'The stem file is missing.',
        }),
      })
    })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'The audio for this job is gone from disk. Run the separation again to recreate the stems.',
    )
  })

  it('falls back to a generic message for a rejection that is not an ApiError', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({ status: 'error', error: new TypeError('no Web Audio') })
    })

    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong')
  })

  it('says nothing is being tracked when there is no job', () => {
    renderPlayer(new FakeEngine(), {})

    expect(
      screen.getByText('No separation job is being tracked.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('slider')).toBeNull()
  })
})

describe('StemPlayer result-fetch retry (feature 048)', () => {
  it('recovers from a dropped result fetch when the user tries again', async () => {
    stubResultFetchQueue(
      () => Promise.reject(new TypeError('network error')),
      () => Promise.resolve(jsonResponse(resultOver(twoStemNames))),
    )
    const engine = new FakeEngine()
    renderPlayer(engine)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Something went wrong',
    )
    const retry = screen.getByRole('button', { name: 'Try again' })

    await userEvent.click(retry)

    await screen.findByRole('button', { name: 'Mute vocals' })
    await act(async () => {})
    engine.becomeReady(twoStemNames)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('offers "Try again" for a 409 result_not_available while the job is still running', async () => {
    stubResultFetchQueue(() =>
      Promise.resolve(
        errorResponse(409, 'result_not_available', 'No result yet.', {
          state: 'separating',
        }),
      ),
    )
    renderPlayer(new FakeEngine())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'this job is separating',
    )
    expect(
      screen.getByRole('button', { name: 'Try again' }),
    ).toBeInTheDocument()
  })

  it('does not apply a retry fetch that resolves after a newer fetch superseded it', async () => {
    // Three distinct fetches, all held open until this test settles them:
    //   1. the initial fetch — rejects, so "Try again" appears.
    //   2. the retry's fetch (clicking "Try again") — left abandoned.
    //   3. the fetch from re-tracking the same job — the component's
    //      *current* fetch, which is what the assertions below are about.
    // `stubResultFetchQueue` repeats its last responder for further calls,
    // so a genuine race needs a third, distinct one — reusing the retry's
    // responder for the re-track would make it the very thing being awaited
    // and the test would pass for the wrong reason.
    const retryFetch = deferred<Response>()
    const retrackFetch = deferred<Response>()
    stubResultFetchQueue(
      () => Promise.reject(new TypeError('network error')),
      () => retryFetch.promise,
      () => retrackFetch.promise,
    )

    /** Re-tracks the same job, as `POST /jobs` or a WS reconnect might. */
    function Retracker({ job }: { job: Job }) {
      const dispatch = useJobDispatch()
      return (
        <button
          type="button"
          onClick={() => {
            dispatch({ type: 'job/track', job })
          }}
        >
          Retrack
        </button>
      )
    }

    render(
      <AppStateProvider initialState={inspectingState()}>
        <JobStateProvider
          initialState={{ ...initialJobState, job: completedJob }}
        >
          <StemPlayer createEngine={() => new FakeEngine()} />
          <Retracker job={completedJob} />
        </JobStateProvider>
      </AppStateProvider>,
    )

    // (a) The initial fetch fails, so "Try again" is offered.
    await screen.findByRole('alert')

    // (b) Click "Try again": its fetch is the abandoned deferred above.
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await screen.findByText('Loading the separation result…')

    // (c) Clear the job, then re-track the *same* job: the component is back
    // in `loading`, with a fresh, distinct fetch in flight — its own current
    // one.
    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )
    expect(
      screen.getByText('No separation job is being tracked.'),
    ).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retrack' }))
    await screen.findByText('Loading the separation result…')

    // (d) The abandoned retry fetch (b) resolves late, with a real result.
    await act(async () => {
      retryFetch.resolve(jsonResponse(resultOver(twoStemNames)))
      await Promise.resolve()
      await Promise.resolve()
    })

    // (e) The stale result must not render: this component's own current
    // fetch (c) has not answered yet, so it must still show loading.
    expect(
      screen.getByText('Loading the separation result…'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Mute vocals' })).toBeNull()
  })
})

describe('StemPlayer transport', () => {
  it('plays, then pauses, through the engine', async () => {
    const { engine } = await renderReady(twoStemNames)

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    expect(engine.playCount).toBe(1)
    const pause = await screen.findByRole('button', { name: 'Pause' })

    await userEvent.click(pause)

    expect(engine.pauseCount).toBe(1)
    expect(screen.getByRole('button', { name: 'Play' })).toBeInTheDocument()
  })

  it('seeks the engine and moves the readout', async () => {
    const { engine } = await renderReady(twoStemNames)

    dragTimeline(18)

    // A pointer gesture commits through `endScrubPreview`, which *is* its
    // seek (feature 052) — hence `moves` rather than `seeks`.
    expect(engine.moves).toEqual([18])
    expect(screen.getByText('0:18 / 1:00')).toBeInTheDocument()
  })

  it('still commits a drag whose session was closed underneath it', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(surface, { clientX: xFor(18), pointerId: 1 })
    // The engine closes sessions defensively on play/pause/seek — reachable
    // mid-drag through a second pointer or a programmatic caller. The
    // release must not trust its own ref alone: `endScrubPreview` on a
    // closed session is a no-op, and the dragged-to position would silently
    // never commit.
    act(() => {
      engine.update({ scrubbing: false })
    })
    fireEvent.pointerUp(surface, { pointerId: 1 })

    expect(engine.seeks).toEqual([18])
    expect(engine.moves).toEqual([18])
  })

  it('treats a motionless click as the degenerate drag: one seek', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(42), pointerId: 1 })
    fireEvent.pointerUp(surface, { pointerId: 1 })

    expect(engine.moves).toEqual([42])
  })

  it.each([
    ['ArrowRight', {}, 1],
    ['ArrowLeft', {}, 0],
    ['ArrowRight', { shiftKey: true }, 5],
    ['Home', {}, 0],
    ['End', {}, STEM_SECONDS],
  ])('commits one discrete seek for %s', async (key, modifiers, expected) => {
    const { engine } = await renderReady(twoStemNames)

    fireEvent.keyDown(timeline(), { key, ...modifiers })

    expect(engine.seeks).toEqual([expected])
  })

  it('toggles playback with Space on the focused timeline', async () => {
    const { engine } = await renderReady(twoStemNames)

    fireEvent.keyDown(timeline(), { key: ' ' })
    expect(engine.playCount).toBe(1)

    fireEvent.keyDown(timeline(), { key: ' ' })
    expect(engine.pauseCount).toBe(1)
    expect(engine.seeks).toEqual([])
  })

  it('spans the full mix duration, and says where the playhead is', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()
    expect(surface).toHaveAttribute('aria-valuemin', '0')
    expect(surface).toHaveAttribute('aria-valuemax', String(STEM_SECONDS))
    expect(surface).toHaveAttribute('aria-valuenow', '0')
    expect(surface).toHaveAttribute('aria-valuetext', '0:00 of 1:00')

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    engine.time = 24
    flushFrame()

    expect(timeline()).toHaveAttribute('aria-valuenow', '24')
    expect(timeline()).toHaveAttribute('aria-valuetext', '0:24 of 1:00')
  })

  it('moves the playhead with a transform rather than a repaint', async () => {
    const { engine } = await renderReady(twoStemNames)
    const playhead = screen.getByTestId('stem-timeline-playhead')
    expect(playhead).toHaveStyle({ transform: 'translateX(0px)' })

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    engine.time = 30
    flushFrame()

    // Half the file, so half the 400 px strip.
    expect(screen.getByTestId('stem-timeline-playhead')).toHaveStyle({
      transform: 'translateX(200px)',
    })
  })

  it('reads the playhead off the engine on every animation frame', async () => {
    const { engine } = await renderReady(twoStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    engine.time = 12
    flushFrame()
    expect(screen.getByText('0:12 / 1:00')).toBeInTheDocument()

    engine.time = 47.5
    flushFrame()
    expect(screen.getByText('0:48 / 1:00')).toBeInTheDocument()
  })

  it('stops asking for frames once playback is paused', async () => {
    const { engine } = await renderReady(twoStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    flushFrame()
    expect(frames.size).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('button', { name: 'Pause' }))

    expect(frames.size).toBe(0)
    engine.time = 30
    flushFrame()
    expect(screen.getByText('0:00 / 1:00')).toBeInTheDocument()
  })
})

describe('StemPlayer mute and solo', () => {
  it('forwards a mute and a solo to the engine', async () => {
    const { engine } = await renderReady(fourStemNames)

    await userEvent.click(screen.getByRole('button', { name: 'Mute drums' }))
    await userEvent.click(screen.getByRole('button', { name: 'Solo bass' }))

    expect(engine.muteToggles).toEqual(['drums'])
    expect(engine.soloToggles).toEqual(['bass'])
  })

  it('reflects the engine snapshot in the toggles', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        stems: [
          stemState('vocals', { soloed: true }),
          stemState('instrumental', { muted: true, audible: false }),
        ],
      })
    })

    expect(screen.getByRole('button', { name: 'Solo vocals' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(
      screen.getByRole('button', { name: 'Mute instrumental' }),
    ).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Mute vocals' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})

describe('StemPlayer level faders (feature 054)', () => {
  it('calls engine.setLevel with the stem name and value, continuously — not once per gesture', async () => {
    const { engine } = await renderReady(twoStemNames)
    const fader = screen.getByRole('slider', { name: 'vocals level' })

    // Two events from one drag reach the engine as two calls: a level write
    // is a plain `AudioParam` assignment, unlike the seek gesture next door,
    // which batches a whole drag into one `engine.seek` because a seek tears
    // down and rebuilds every source node.
    fireEvent.change(fader, { target: { value: '0.3' } })
    fireEvent.change(fader, { target: { value: '0.9' } })

    expect(engine.levelSets.length).toBeGreaterThanOrEqual(2)
    expect(engine.levelSets).toEqual([
      { name: 'vocals', value: 0.3 },
      { name: 'vocals', value: 0.9 },
    ])
  })

  it('renders one fader per stem for two- and four-stem results', async () => {
    await renderReady(fourStemNames)

    for (const name of fourStemNames) {
      expect(
        screen.getByRole('slider', { name: `${name} level` }),
      ).toBeInTheDocument()
    }
  })

  it("reflects the stem's level from the engine snapshot", async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        stems: [stemState('vocals', { level: 0.4 }), stemState('instrumental')],
      })
    })

    expect(screen.getByRole('slider', { name: 'vocals level' })).toHaveValue(
      '0.4',
    )
  })

  it('is disabled until the stem has loaded', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(new FakeEngine())

    expect(
      await screen.findByRole('slider', { name: 'vocals level' }),
    ).toBeDisabled()
  })

  it('stays enabled and keeps showing the true level while the stem is muted', async () => {
    const { engine } = await renderReady(twoStemNames)

    act(() => {
      engine.update({
        stems: [
          stemState('vocals', { muted: true, audible: false, level: 0.6 }),
          stemState('instrumental'),
        ],
      })
    })

    const fader = screen.getByRole('slider', { name: 'vocals level' })
    expect(fader).toBeEnabled()
    expect(fader).toHaveValue('0.6')
  })
})

describe('StemPlayer loop region (feature 053)', () => {
  /** The three transport loop controls. */
  function loopButton(name: string): HTMLElement {
    return screen.getByRole('button', { name })
  }

  /** The badge's text, or `null` when no region is set. */
  function badge(): string | null {
    return (
      document.querySelector('.stem-player-loop-badge')?.textContent ?? null
    )
  }

  it('renders the region from the snapshot, formatted', async () => {
    const { engine } = await renderReady(twoStemNames)
    expect(badge()).toBeNull()

    act(() => {
      engine.update({ loopRegion: { start: 12, end: 34 } })
    })

    expect(badge()).toBe('Loop 0:12 – 0:34')
    // A live region, so a screen reader hears the loop it did not watch being
    // drawn — and it is mounted whether or not there is a region to announce.
    expect(document.querySelector('.stem-player-loop-status')).toHaveAttribute(
      'aria-live',
      'polite',
    )
  })

  it('marks the loop start at the playhead, running to the end of the mix', async () => {
    const { engine } = await renderReady(twoStemNames)
    dragTimeline(20)

    await userEvent.click(loopButton('Loop start'))

    expect(engine.loopRegions).toHaveLength(1)
    expect(engine.loopRegions[0]?.start).toBeCloseTo(20, 6)
    expect(engine.loopRegions[0]?.end).toBe(STEM_SECONDS)
    expect(badge()).toBe('Loop 0:20 – 1:00')
  })

  it('marks the loop end at the playhead, keeping a start that is behind it', async () => {
    const { engine } = await renderReady(twoStemNames)
    dragTimeline(20)
    await userEvent.click(loopButton('Loop start'))
    dragTimeline(40)

    await userEvent.click(loopButton('Loop end'))

    expect(engine.loopRegions).toHaveLength(2)
    expect(engine.loopRegions[1]?.start).toBeCloseTo(20, 6)
    expect(engine.loopRegions[1]?.end).toBeCloseTo(40, 6)
    expect(badge()).toBe('Loop 0:20 – 0:40')
  })

  it('falls back to the start of the mix for a loop end with no region', async () => {
    const { engine } = await renderReady(twoStemNames)
    dragTimeline(30)

    await userEvent.click(loopButton('Loop end'))

    expect(engine.loopRegions[0]?.start).toBe(0)
    expect(engine.loopRegions[0]?.end).toBeCloseTo(30, 6)
  })

  it('widens rather than swapping when the edges would cross', async () => {
    const { engine } = await renderReady(twoStemNames)
    dragTimeline(20)
    await userEvent.click(loopButton('Loop start'))
    // The playhead moves *past* the region's end, and the user asks for a new
    // start there: the end the region had is no longer usable, so the region
    // runs to the end of the mix rather than quietly moving the edge the user
    // did not touch.
    dragTimeline(50)
    act(() => {
      engine.update({ loopRegion: { start: 20, end: 40 } })
    })

    await userEvent.click(loopButton('Loop start'))

    expect(engine.loopRegions[1]?.start).toBeCloseTo(50, 6)
    expect(engine.loopRegions[1]?.end).toBe(STEM_SECONDS)
  })

  it('offers Clear loop only when there is one, and clears it', async () => {
    const { engine } = await renderReady(twoStemNames)
    expect(loopButton('Clear loop')).toBeDisabled()

    await userEvent.click(loopButton('Loop start'))
    expect(loopButton('Clear loop')).toBeEnabled()

    await userEvent.click(loopButton('Clear loop'))

    expect(engine.loopRegions.at(-1)).toBeNull()
    expect(badge()).toBeNull()
    expect(loopButton('Clear loop')).toBeDisabled()
  })

  it('leaves the loop controls disabled until the stems are ready', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(new FakeEngine())
    await screen.findByText('Decoding stems…')

    expect(screen.getByRole('button', { name: 'Loop start' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Loop end' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Clear loop' })).toBeDisabled()
  })

  it('drags a region on the ruler, straight through to the engine', async () => {
    const { engine } = await renderReady(twoStemNames)
    const ruler = screen.getByTestId('stem-timeline-ruler-row')

    fireEvent.pointerDown(ruler, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: xFor(35), pointerId: 1 })
    fireEvent.pointerUp(ruler, { pointerId: 1 })

    expect(engine.loopRegions).toHaveLength(1)
    expect(engine.loopRegions[0]?.start).toBeCloseTo(10, 6)
    expect(engine.loopRegions[0]?.end).toBeCloseTo(35, 6)
    expect(badge()).toBe('Loop 0:10 – 0:35')
    // The drag committed a region, not a seek.
    expect(engine.seeks).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Against the real engine, with a fake AudioContext underneath: the mute,
// solo and loop *semantics* the user actually experiences, end to end through
// the UI.
// ---------------------------------------------------------------------------

describe('StemPlayer over the real engine', () => {
  it('silences a muted stem and restores it', async () => {
    await renderReal(fourStemNames)

    await userEvent.click(screen.getByRole('button', { name: 'Mute drums' }))
    expect(gains()).toEqual([1, 0, 1, 1])
    expect(screen.getByRole('button', { name: 'Mute drums' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    await userEvent.click(screen.getByRole('button', { name: 'Mute drums' }))
    expect(gains()).toEqual([1, 1, 1, 1])
  })

  it('reaches the gain node value through a fader change (feature 054)', async () => {
    await renderReal(fourStemNames)

    fireEvent.change(screen.getByRole('slider', { name: 'drums level' }), {
      target: { value: '0.25' },
    })

    expect(gains()).toEqual([1, 0.25, 1, 1])

    // The controlled input round-trips through the snapshot: the engine's
    // synchronous notify is what feeds the value back to the fader.
    expect(screen.getByRole('slider', { name: 'drums level' })).toHaveValue(
      '0.25',
    )

    // Mute silences it regardless of the level just set — `level` and
    // `audible` compose the way the engine documents, not the fader alone.
    await userEvent.click(screen.getByRole('button', { name: 'Mute drums' }))
    expect(gains()).toEqual([1, 0, 1, 1])
  })

  it('adds solos together and restores the mutes when they are cleared', async () => {
    await renderReal(fourStemNames)

    await userEvent.click(screen.getByRole('button', { name: 'Mute other' }))
    await userEvent.click(screen.getByRole('button', { name: 'Solo vocals' }))
    expect(gains()).toEqual([1, 0, 0, 0])

    await userEvent.click(screen.getByRole('button', { name: 'Solo bass' }))
    expect(gains()).toEqual([1, 0, 1, 0])

    await userEvent.click(screen.getByRole('button', { name: 'Solo vocals' }))
    await userEvent.click(screen.getByRole('button', { name: 'Solo bass' }))
    // Back to exactly the mute state from before the first solo.
    expect(gains()).toEqual([1, 1, 1, 0])
    expect(screen.getByRole('button', { name: 'Mute other' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('starts every stem together and tracks the audio clock', async () => {
    await renderReal(twoStemNames)

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    expect(context.sources).toHaveLength(2)
    expect(new Set(context.sources.map((s) => s.started?.when)).size).toBe(1)
    expect(new Set(context.sources.map((s) => s.started?.offset))).toEqual(
      new Set([0]),
    )

    context.currentTime = 25
    flushFrame()
    expect(screen.getByText('0:25 / 1:00')).toBeInTheDocument()
  })

  it('restarts every stem together at the seeked offset', async () => {
    await renderReal(fourStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    dragTimeline(30)

    // The last four are the transport's new generation; the ones before them
    // are the press's preview grains (feature 052), which never touch it.
    const restarted = context.sources.slice(-4)
    expect(restarted).toHaveLength(4)
    expect(new Set(restarted.map((s) => s.started?.when)).size).toBe(1)
    expect(new Set(restarted.map((s) => s.started?.offset))).toEqual(
      new Set([30]),
    )
    expect(screen.getByText('0:30 / 1:00')).toBeInTheDocument()
  })

  it('loops every stem on the region a ruler drag draws (feature 053)', async () => {
    await renderReal(fourStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    const ruler = screen.getByTestId('stem-timeline-ruler-row')

    fireEvent.pointerDown(ruler, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: xFor(30), pointerId: 1 })
    fireEvent.pointerUp(ruler, { pointerId: 1 })

    // One rebuilt generation, every stem carrying the same loop boundaries
    // and one shared start time: that is what "sample-accurate across every
    // stem" means at the level this test can see.
    const looping = context.sourcesFrom(4)
    expect(looping).toHaveLength(4)
    expect(looping.every((source) => source.loop)).toBe(true)
    expect(new Set(looping.map((source) => source.started?.when)).size).toBe(1)
    expect(new Set(looping.map((source) => source.loopStart)).size).toBe(1)
    expect(new Set(looping.map((source) => source.loopEnd)).size).toBe(1)
    expect(looping[0]?.loopStart).toBeCloseTo(10, 6)
    expect(looping[0]?.loopEnd).toBeCloseTo(30, 6)
    expect(document.querySelector('.stem-player-loop-badge')?.textContent).toBe(
      'Loop 0:10 – 0:30',
    )

    // …and the playhead wraps with them: 25 s of raw material into a 10–30 s
    // loop entered at 0:10 is 0:15.
    context.currentTime = 35
    flushFrame()
    expect(screen.getByText('0:15 / 1:00')).toBeInTheDocument()
  })

  it('resumes a suspended context from the play click', async () => {
    context = new FakeAudioContext('suspended')
    const engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: () => Promise.resolve(stemBytes(STEM_SECONDS)),
      lookaheadSeconds: 0,
    })
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(engine)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
    }, SETTLE)
    expect(context.resumeCount).toBe(0)

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    await waitFor(() => {
      expect(context.resumeCount).toBe(1)
    })
    await act(async () => {})
    expect(context.sources).toHaveLength(2)
  })

  it('reports a stem whose file is gone, and still plays the rest', async () => {
    context = new FakeAudioContext()
    const engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: (url: string) =>
        url.endsWith('/instrumental')
          ? Promise.reject(
              new ApiError(404, {
                code: 'stem_file_missing',
                message: 'The stem file is missing.',
              }),
            )
          : Promise.resolve(stemBytes(STEM_SECONDS)),
    })
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(engine)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
    }, SETTLE)
    await act(async () => {})
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Mute instrumental' }),
    ).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Mute vocals' })).toBeEnabled()
  })
})

// ---------------------------------------------------------------------------
// Regressions from the PR #26 review.
// ---------------------------------------------------------------------------

describe('StemPlayer scrubbing (review finding 1)', () => {
  /** The positions a drag across the strip really passes through. */
  const dragPath = [6, 13, 21, 28, 34, 41, 47]

  it('commits one seek for a whole drag, not one per pointer event', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(6), pointerId: 1 })
    for (const seconds of dragPath) {
      fireEvent.pointerMove(surface, { clientX: xFor(seconds), pointerId: 1 })
    }
    expect(engine.moves).toEqual([])

    fireEvent.pointerUp(surface, { pointerId: 1 })

    // One move, at the position the pointer was released from. (Approximate
    // because the value is `x / pxPerSecond` and 400 px does not divide a
    // minute; the *count* is the finding this test exists for.)
    expect(engine.moves).toHaveLength(1)
    expect(engine.moves[0]).toBeCloseTo(47, 6)
  })

  it('follows the drag on screen before the seek is committed', async () => {
    await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(21), pointerId: 1 })

    expect(screen.getByText('0:21 / 1:00')).toBeInTheDocument()
    expect(timeline()).toHaveAttribute('aria-valuenow', '21')
    expect(screen.getByTestId('stem-timeline-playhead')).toHaveStyle({
      transform: 'translateX(140px)',
    })
  })

  it('rebuilds the source graph once per drag, not once per pointer event', async () => {
    await renderReal(fourStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    // One source per stem for the initial play.
    expect(context.sources).toHaveLength(4)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(6), pointerId: 1 })
    for (const seconds of dragPath) {
      fireEvent.pointerMove(surface, { clientX: xFor(seconds), pointerId: 1 })
    }

    // Eight moves, but the clock never advanced, so the retrigger throttle
    // allowed exactly one grain per stem — and none of them is a transport
    // rebuild, which is the finding this test exists for: a rebuild per event
    // would be eight silent lookaheads.
    const grains = context.sourcesFrom(4)
    expect(grains).toHaveLength(4)
    expect(grains.every((source) => !source.loop)).toBe(true)

    fireEvent.pointerUp(surface, { pointerId: 1 })

    const restarted = context.sourcesFrom(8)
    expect(restarted).toHaveLength(4)
    const offsets = restarted.map((source) => source.started?.offset ?? -1)
    expect(new Set(offsets).size).toBe(1)
    expect(offsets[0]).toBeCloseTo(47, 6)
    expect(new Set(restarted.map((source) => source.started?.when)).size).toBe(
      1,
    )
  })

  it('ignores a second release of the same gesture', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(12), pointerId: 1 })
    fireEvent.pointerUp(surface, { pointerId: 1 })
    fireEvent.pointerUp(surface, { pointerId: 1 })

    expect(engine.moves).toEqual([12])
    // …and the session was closed once, not once per release.
    expect(engine.scrubEnds).toEqual([12])
  })

  it('lets a keyboard commit end a drag instead of doubling it', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(surface, { clientX: xFor(12), pointerId: 1 })
    fireEvent.keyDown(surface, { key: 'ArrowRight' })

    // The arrow key committed the move; the ref must be cleared with it, or
    // the release below would fire a second, stale one over this one. It also
    // has to end the preview session the drag opened (feature 052), which is
    // why the commit goes through `endScrubPreview` rather than `seek`.
    expect(engine.moves).toHaveLength(1)
    expect(engine.scrubEnds).toHaveLength(1)
    expect(engine.getSnapshot().scrubbing).toBe(false)

    fireEvent.pointerUp(surface, { pointerId: 1 })

    expect(engine.moves).toHaveLength(1)
    expect(engine.scrubEnds).toHaveLength(1)
  })

  it('commits nothing when the gesture is cancelled', async () => {
    const { engine } = await renderReady(twoStemNames)
    engine.time = 7
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(40), pointerId: 1 })
    fireEvent.pointerMove(surface, { clientX: xFor(44), pointerId: 1 })
    expect(screen.getByText('0:44 / 1:00')).toBeInTheDocument()

    fireEvent.pointerCancel(surface, { pointerId: 1 })

    expect(engine.moves).toEqual([])
    // Snapped back to wherever the audio clock actually is.
    expect(screen.getByText('0:07 / 1:00')).toBeInTheDocument()
  })

  it('lets the audio clock drive the readout again after the drag', async () => {
    const { engine } = await renderReady(twoStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(30), pointerId: 1 })
    engine.time = 9
    flushFrame()
    // Still showing the drag, not the clock.
    expect(screen.getByText('0:30 / 1:00')).toBeInTheDocument()

    fireEvent.pointerUp(surface, { pointerId: 1 })
    engine.time = 31
    flushFrame()

    expect(screen.getByText('0:31 / 1:00')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// Feature 052: the audible scrub preview
//
// The player is where a gesture is turned into engine calls, so this is where
// "one real transport move per gesture, with sound during the drag" is pinned.
// ---------------------------------------------------------------------------

describe('StemPlayer audible scrub preview', () => {
  const dragPath = [13, 21, 34, 47]

  it('opens one session, previews every move, and commits once', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(6), pointerId: 1 })
    for (const seconds of dragPath) {
      fireEvent.pointerMove(surface, { clientX: xFor(seconds), pointerId: 1 })
    }

    expect(engine.scrubStarts).toBe(1)
    // The press and every move, in order — the engine throttles these against
    // its own clock, so the player is free to forward all of them.
    expect(engine.scrubPreviews).toHaveLength(1 + dragPath.length)
    for (const [index, seconds] of [6, ...dragPath].entries()) {
      expect(engine.scrubPreviews[index]).toBeCloseTo(seconds, 6)
    }
    expect(engine.scrubEnds).toEqual([])

    fireEvent.pointerUp(surface, { pointerId: 1 })

    // One session end carrying the commit — and no `seek` beside it, because
    // `endScrubPreview(commit)` *is* this gesture's seek.
    expect(engine.scrubEnds).toHaveLength(1)
    expect(engine.scrubEnds[0]).toBeCloseTo(47, 6)
    expect(engine.seeks).toEqual([])
    expect(engine.moves).toHaveLength(1)
  })

  it('ends the session with no commit when the gesture is cancelled', async () => {
    const { engine } = await renderReady(twoStemNames)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(40), pointerId: 1 })
    fireEvent.pointerMove(surface, { clientX: xFor(44), pointerId: 1 })
    fireEvent.pointerCancel(surface, { pointerId: 1 })

    expect(engine.scrubEnds).toEqual([undefined])
    expect(engine.moves).toEqual([])
  })

  it('leaves a keyboard seek with no drag under way a plain seek', async () => {
    const { engine } = await renderReady(twoStemNames)

    fireEvent.keyDown(timeline(), { key: 'ArrowRight' })

    // Discrete, so there is nothing to audition and no session to close.
    expect(engine.seeks).toHaveLength(1)
    expect(engine.scrubStarts).toBe(0)
    expect(engine.scrubEnds).toEqual([])
  })

  it('previews nothing for a loop-region gesture', async () => {
    const { engine } = await renderReady(twoStemNames)
    const ruler = screen.getByTestId('stem-timeline-ruler-row')

    fireEvent.pointerDown(ruler, { clientX: xFor(10), pointerId: 1 })
    fireEvent.pointerMove(ruler, { clientX: xFor(30), pointerId: 1 })
    fireEvent.pointerUp(ruler, { pointerId: 1 })

    expect(engine.scrubStarts).toBe(0)
    expect(engine.scrubPreviews).toEqual([])
    expect(engine.loopRegions).toHaveLength(1)
  })

  it('silences the transport under the pointer and sounds grains instead', async () => {
    await renderReal(fourStemNames)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))
    const playing = context.sourcesFrom(0)
    const surface = timeline()

    fireEvent.pointerDown(surface, { clientX: xFor(21), pointerId: 1 })

    // The mix stops — Audacity pauses while you scrub — and one grain per
    // stem is scheduled at the position under the pointer instead.
    for (const source of playing) {
      expect(source.stopCount).toBe(1)
    }
    expect(screen.getByRole('button', { name: 'Play' })).toBeInTheDocument()
    const grains = context.sourcesFrom(4)
    expect(grains).toHaveLength(4)
    expect(new Set(grains.map((grain) => grain.started?.when)).size).toBe(1)
    for (const grain of grains) {
      expect(grain.started?.offset).toBeCloseTo(21, 6)
      expect(grain.loop).toBe(false)
    }

    fireEvent.pointerUp(surface, { pointerId: 1 })

    // …and the release resumes the mix, once, where it was let go.
    const resumed = context.sourcesFrom(8)
    expect(resumed).toHaveLength(4)
    expect(resumed[0]?.started?.offset).toBeCloseTo(21, 6)
    expect(screen.getByRole('button', { name: 'Pause' })).toBeInTheDocument()
  })
})

describe('StemPlayer route out of the inspect phase (review finding 2)', () => {
  it('offers a way to start another separation', async () => {
    await renderReady(twoStemNames)

    expect(
      screen.getByRole('button', { name: 'Start another separation' }),
    ).toBeInTheDocument()
  })

  it('clears the tracked job and returns to configure', async () => {
    await renderReady(twoStemNames)
    expect(screen.getByTestId('workflow-phase')).toHaveTextContent('inspect')

    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )

    expect(screen.getByTestId('workflow-phase')).toHaveTextContent('configure')
    expect(screen.getByTestId('tracked-job')).toHaveTextContent('none')
  })

  it.each([
    [
      'a 409 for a cancelled job',
      errorResponse(409, 'result_not_available', 'No result.', {
        state: 'cancelled',
      }),
    ],
    [
      'a 404 for a forgotten job',
      errorResponse(404, 'job_not_found', 'No such job.'),
    ],
  ])(
    'survives %s, so an error is never a dead end',
    async (_label, response) => {
      stubResultFetch(response)
      renderPlayer(new FakeEngine())

      await screen.findByRole('alert')

      expect(
        screen.getByRole('button', { name: 'Start another separation' }),
      ).toBeInTheDocument()
    },
  )

  it('falls back to file selection when the upload is gone', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    renderPlayer(
      new FakeEngine(),
      { job: completedJob },
      {
        ...initialAppState,
        phase: 'inspect',
      },
    )
    await screen.findByRole('button', { name: 'Mute vocals' })

    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )

    // Both stores always move together: never a cleared job on a stale phase.
    expect(screen.getByTestId('workflow-phase')).toHaveTextContent('select')
    expect(screen.getByTestId('tracked-job')).toHaveTextContent('none')
  })
})

describe('StemPlayer cleanup', () => {
  it('disposes the engine on unmount', async () => {
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    const engine = new FakeEngine()
    const view = renderPlayer(engine)
    await screen.findByRole('button', { name: 'Mute vocals' })

    view.unmount()

    expect(engine.disposeCount).toBe(1)
  })

  it('stops the sources and closes the context on unmount', async () => {
    const context = new FakeAudioContext()
    const engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: () => Promise.resolve(stemBytes(STEM_SECONDS)),
      lookaheadSeconds: 0,
    })
    stubResultFetch(jsonResponse(resultOver(twoStemNames)))
    const view = renderPlayer(engine)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
    }, SETTLE)
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    view.unmount()

    expect(context.sources.every((source) => source.stopCount === 1)).toBe(true)
    expect(context.gains.every((gain) => gain.disconnectCount === 1)).toBe(true)
    expect(context.closeCount).toBe(1)
  })
})
