import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StemPlayer } from './StemPlayer'
import { ApiError } from '../api/client'
import {
  JobStateProvider,
  initialJobState,
  type JobStateValue,
} from '../state/jobState'
import type { Job, SeparationResult, Stem } from '../api/types'
import {
  createStemAudioEngine,
  type StemEngineSnapshot,
  type StemPlayerEngine,
  type StemSource,
} from '../audio/engine'
import { FakeAudioContext, stemBytes } from '../test/fakeAudioContext'
import { sampleJob, sampleJobId } from '../test/fixtures'

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
  readonly muteToggles: string[] = []
  readonly soloToggles: string[] = []
  disposeCount = 0
  time = 0

  private readonly listeners = new Set<() => void>()
  private snapshot: StemEngineSnapshot = {
    status: 'loading',
    stems: [],
    playing: false,
    durationSeconds: 0,
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

  setLevel = (): void => {
    // Not driven from this UI.
  }

  currentTime = (): number => this.time

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

function renderPlayer(
  engine: StemPlayerEngine,
  jobState: Partial<JobStateValue> = { job: completedJob },
) {
  const createEngine = () => engine
  return render(
    <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
      <StemPlayer createEngine={createEngine} />
    </JobStateProvider>,
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
  await screen.findByRole('button', { name: `Mute ${String(names[0])}` })
  await act(async () => {})
  engine.becomeReady(names)
  await waitFor(() => {
    expect(screen.getByRole('button', { name: 'Play' })).toBeEnabled()
  })
  return { engine }
}

beforeEach(() => {
  stubAnimationFrames()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ---------------------------------------------------------------------------

describe('StemPlayer stem rows', () => {
  it('renders one row per stem of a two-stem result', async () => {
    await renderReady(twoStemNames)

    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    for (const name of twoStemNames) {
      expect(screen.getByText(name)).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: `Mute ${name}` }),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: `Solo ${name}` }),
      ).toBeInTheDocument()
    }
  })

  it('renders one row per stem of a four-stem result', async () => {
    await renderReady(fourStemNames)

    expect(screen.getAllByRole('listitem')).toHaveLength(4)
    for (const name of fourStemNames) {
      expect(
        screen.getByRole('button', { name: `Solo ${name}` }),
      ).toBeInTheDocument()
    }
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
    expect(screen.getByRole('slider', { name: 'Seek' })).toBeDisabled()
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

    fireEvent.change(screen.getByRole('slider', { name: 'Seek' }), {
      target: { value: '18' },
    })

    expect(engine.seeks).toEqual([18])
    expect(screen.getByText('0:18 / 1:00')).toBeInTheDocument()
  })

  it('spans the full mix duration', async () => {
    await renderReady(twoStemNames)

    const slider = screen.getByRole('slider', { name: 'Seek' })
    expect(slider).toHaveAttribute('min', '0')
    expect(slider).toHaveAttribute('max', String(STEM_SECONDS))
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

// ---------------------------------------------------------------------------
// Against the real engine, with a fake AudioContext underneath: the mute and
// solo *semantics* the user actually experiences, end to end through the UI.
// ---------------------------------------------------------------------------

describe('StemPlayer over the real engine', () => {
  let context: FakeAudioContext

  /** Gain values in stem order, which is the engine's node order. */
  function gains(): number[] {
    return context.gains.map((gain) => gain.gain.value)
  }

  async function renderReal(
    names: readonly string[],
  ): Promise<StemPlayerEngine> {
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
    })
    return engine
  }

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

    fireEvent.change(screen.getByRole('slider', { name: 'Seek' }), {
      target: { value: '30' },
    })

    const restarted = context.sourcesFrom(4)
    expect(restarted).toHaveLength(4)
    expect(new Set(restarted.map((s) => s.started?.when)).size).toBe(1)
    expect(new Set(restarted.map((s) => s.started?.offset))).toEqual(
      new Set([30]),
    )
    expect(screen.getByText('0:30 / 1:00')).toBeInTheDocument()
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
    })
    expect(context.resumeCount).toBe(0)

    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    await waitFor(() => {
      expect(context.resumeCount).toBe(1)
    })
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
    })
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Mute instrumental' }),
    ).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Mute vocals' })).toBeEnabled()
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
    })
    await userEvent.click(screen.getByRole('button', { name: 'Play' }))

    view.unmount()

    expect(context.sources.every((source) => source.stopCount === 1)).toBe(true)
    expect(context.gains.every((gain) => gain.disconnectCount === 1)).toBe(true)
    expect(context.closeCount).toBe(1)
  })
})
