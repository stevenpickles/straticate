import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import {
  createStemAudioEngine,
  DEFAULT_LOOKAHEAD_SECONDS,
  DEFAULT_SCRUB_FADE_SECONDS,
  StemAudioEngine,
  type StemPlayerEngine,
  type StemSource,
} from './engine'
import {
  FakeAudioContext,
  stemBytes,
  stemBytesWithSamples,
  type FakeSourceNode,
} from '../test/fakeAudioContext'

/** Lookahead used throughout, so scheduled start times are predictable. */
const LOOKAHEAD = 0.05

/**
 * Stem sources for a job, named and lengthened by the caller. Nothing here
 * hardcodes a stem name or a stem count: the two- and four-stem cases are
 * the same call with a different table.
 */
function sources(durations: Record<string, number>): StemSource[] {
  return Object.keys(durations).map((name) => ({
    name,
    url: `/api/v1/jobs/JOB/stems/${name}`,
  }))
}

/** Loader signature the engine calls: a URL plus a cancellation signal. */
type StemLoader = (url: string, signal?: AbortSignal) => Promise<ArrayBuffer>

/** A loader that answers each stem URL with bytes of that stem's duration. */
function loaderFor(durations: Record<string, number>): StemLoader {
  return (url: string) => {
    const name = url.slice(url.lastIndexOf('/') + 1)
    const duration = durations[name]
    if (duration === undefined) {
      return Promise.reject(
        new ApiError(404, {
          code: 'stem_file_missing',
          message: 'The stem file is gone.',
        }),
      )
    }
    return Promise.resolve(stemBytes(duration))
  }
}

/** A promise plus its resolver, for holding a stem download in flight. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

const twoStems = { vocals: 10, instrumental: 10 }
const fourStems = { vocals: 10, drums: 10, bass: 10, other: 10 }

let context: FakeAudioContext
let engine: StemPlayerEngine

function makeEngine(
  durations: Record<string, number>,
  options: { state?: string; load?: StemLoader } = {},
): StemPlayerEngine {
  context = new FakeAudioContext(options.state ?? 'running')
  return createStemAudioEngine({
    createContext: () => context,
    loadStemAudio: options.load ?? loaderFor(durations),
    lookaheadSeconds: LOOKAHEAD,
  })
}

async function loadEngine(
  durations: Record<string, number>,
  options: { state?: string; load?: StemLoader } = {},
): Promise<StemPlayerEngine> {
  engine = makeEngine(durations, options)
  await engine.load(sources(durations))
  return engine
}

/** The distinct `when` values of a set of scheduled sources. */
function startTimes(nodes: readonly FakeSourceNode[]): unknown[] {
  return [...new Set(nodes.map((node) => node.started?.when))]
}

/** The distinct `offset` values of a set of scheduled sources. */
function startOffsets(nodes: readonly FakeSourceNode[]): unknown[] {
  return [...new Set(nodes.map((node) => node.started?.offset))]
}

/** The gain value the engine set for `name`. */
function gainOf(name: string, order: readonly string[]): number {
  const index = order.indexOf(name)
  const gain = context.gains[index]
  if (gain === undefined) {
    throw new Error(`no gain node for ${name}`)
  }
  return gain.gain.value
}

afterEach(() => {
  engine.dispose()
})

describe('StemAudioEngine loading', () => {
  it('fetches, decodes and wires one gain node per stem', async () => {
    await loadEngine(twoStems)

    expect(context.decoded).toHaveLength(2)
    expect(context.gains).toHaveLength(2)
    for (const gain of context.gains) {
      expect(gain.connections).toEqual([context.destination])
    }
    const snapshot = engine.getSnapshot()
    expect(snapshot.status).toBe('ready')
    expect(snapshot.stems.map((stem) => stem.name)).toEqual([
      'vocals',
      'instrumental',
    ])
    expect(snapshot.stems.every((stem) => stem.status === 'loaded')).toBe(true)
    expect(snapshot.error).toBeNull()
  })

  it('loads a four-stem job with exactly the same code path', async () => {
    await loadEngine(fourStems)

    expect(context.gains).toHaveLength(4)
    expect(engine.getSnapshot().stems.map((stem) => stem.name)).toEqual([
      'vocals',
      'drums',
      'bass',
      'other',
    ])
    expect(engine.getSnapshot().status).toBe('ready')
  })

  it('reports the longest stem as the transport duration', async () => {
    await loadEngine({ vocals: 10, instrumental: 12.5 })
    expect(engine.getSnapshot().durationSeconds).toBe(12.5)
  })

  it('records a per-stem failure and still plays the stems that loaded', async () => {
    await loadEngine(twoStems, { load: loaderFor({ vocals: 10 }) })

    const snapshot = engine.getSnapshot()
    expect(snapshot.status).toBe('ready')
    const failed = snapshot.stems.find((stem) => stem.name === 'instrumental')
    expect(failed?.status).toBe('error')
    expect((failed?.error as ApiError).code).toBe('stem_file_missing')
    expect((snapshot.error as ApiError).code).toBe('stem_file_missing')
  })

  it('is in the error state when no stem loads at all', async () => {
    await loadEngine(twoStems, { load: loaderFor({}) })

    const snapshot = engine.getSnapshot()
    expect(snapshot.status).toBe('error')
    expect((snapshot.error as ApiError).code).toBe('stem_file_missing')
    expect(snapshot.durationSeconds).toBe(0)
  })

  it('does not construct a context before anything is loaded', () => {
    const createContext = vi.fn(() => new FakeAudioContext())
    engine = createStemAudioEngine({ createContext })
    expect(createContext).not.toHaveBeenCalled()
  })

  it('reports an unusable Web Audio API as an error rather than throwing', async () => {
    engine = createStemAudioEngine({
      createContext: () => {
        throw new TypeError('AudioContext is not defined')
      },
      loadStemAudio: loaderFor(twoStems),
    })

    await expect(engine.load(sources(twoStems))).resolves.toBeUndefined()
    expect(engine.getSnapshot().status).toBe('error')
    expect(engine.getSnapshot().error).toBeInstanceOf(TypeError)
  })
})

describe('StemAudioEngine synchronised playback', () => {
  it('starts every stem at one shared time and offset', async () => {
    await loadEngine(fourStems)
    context.currentTime = 3

    await engine.play()

    expect(context.sources).toHaveLength(4)
    expect(startTimes(context.sources)).toEqual([3 + LOOKAHEAD])
    expect(startOffsets(context.sources)).toEqual([0])
    expect(engine.getSnapshot().playing).toBe(true)
  })

  it('routes each source through its own stem gain node', async () => {
    await loadEngine(twoStems)
    await engine.play()

    expect(context.sources[0]?.connections).toEqual([context.gains[0]])
    expect(context.sources[1]?.connections).toEqual([context.gains[1]])
  })

  it('resumes a suspended context on the first play', async () => {
    await loadEngine(twoStems, { state: 'suspended' })
    expect(context.resumeCount).toBe(0)

    await engine.play()

    expect(context.resumeCount).toBe(1)
    expect(engine.getSnapshot().playing).toBe(true)
  })

  it('does not resume a context that is already running', async () => {
    await loadEngine(twoStems)
    await engine.play()
    expect(context.resumeCount).toBe(0)
  })

  it('ignores play before the stems are ready', async () => {
    engine = makeEngine(twoStems)
    await engine.play()
    expect(context.sources).toHaveLength(0)
  })

  it('ignores a second play while already playing', async () => {
    await loadEngine(twoStems)
    await engine.play()
    await engine.play()
    expect(context.sources).toHaveLength(2)
  })
})

describe('StemAudioEngine playhead', () => {
  it('derives the current time from the audio clock', async () => {
    await loadEngine(twoStems)
    context.currentTime = 1
    await engine.play()

    context.currentTime = 4
    expect(engine.currentTime()).toBeCloseTo(3 - LOOKAHEAD, 5)
  })

  it('stays at the start offset during the scheduling lookahead', async () => {
    await loadEngine(twoStems)
    await engine.play()
    expect(engine.currentTime()).toBe(0)
  })

  it('never runs past the duration', async () => {
    await loadEngine(twoStems)
    await engine.play()
    context.currentTime = 10_000
    expect(engine.currentTime()).toBe(10)
  })

  it('keeps an accurate time while paused', async () => {
    await loadEngine(twoStems)
    await engine.play()
    context.currentTime = 4 + LOOKAHEAD

    engine.pause()

    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.currentTime()).toBeCloseTo(4, 5)
    // The clock keeps running; a paused playhead must not follow it.
    context.currentTime = 40
    expect(engine.currentTime()).toBeCloseTo(4, 5)
  })

  it('resumes from where it was paused, with one shared start time', async () => {
    await loadEngine(twoStems)
    await engine.play()
    context.currentTime = 2 + LOOKAHEAD
    engine.pause()

    context.currentTime = 30
    await engine.play()

    const resumed = context.sourcesFrom(2)
    expect(resumed).toHaveLength(2)
    expect(startTimes(resumed)).toEqual([30 + LOOKAHEAD])
    const offsets = startOffsets(resumed)
    expect(offsets).toHaveLength(1)
    expect(offsets[0] as number).toBeCloseTo(2, 5)
  })

  it('stops every source when paused', async () => {
    await loadEngine(fourStems)
    await engine.play()

    engine.pause()

    expect(context.sources.map((source) => source.stopCount)).toEqual([
      1, 1, 1, 1,
    ])
    expect(
      context.sources.every((source) => source.disconnectCount === 1),
    ).toBe(true)
  })

  it('ignores pause when nothing is playing', async () => {
    await loadEngine(twoStems)
    engine.pause()
    expect(engine.getSnapshot().playing).toBe(false)
  })

  it('settles at the end of the mix when the longest stem ends', async () => {
    await loadEngine({ vocals: 10, instrumental: 12 })
    await engine.play()

    // Only the longest stem's source carries the end handler.
    const withHandler = context.sources.filter(
      (source) => source.onended !== null,
    )
    expect(withHandler).toHaveLength(1)
    withHandler[0]?.onended?.(new Event('ended'))

    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.currentTime()).toBe(12)
  })

  it('replays from the start after reaching the end', async () => {
    await loadEngine(twoStems)
    await engine.play()
    context.sources
      .find((source) => source.onended !== null)
      ?.onended?.(new Event('ended'))

    context.currentTime = 50
    await engine.play()

    expect(startOffsets(context.sourcesFrom(2))).toEqual([0])
  })
})

describe('StemAudioEngine seeking', () => {
  it('recreates every source at the new offset with one new start time', async () => {
    await loadEngine(fourStems)
    await engine.play()
    context.currentTime = 6

    engine.seek(7.5)

    const before = context.sources.slice(0, 4)
    expect(before.every((source) => source.stopCount === 1)).toBe(true)
    const after = context.sourcesFrom(4)
    expect(after).toHaveLength(4)
    expect(startTimes(after)).toEqual([6 + LOOKAHEAD])
    expect(startOffsets(after)).toEqual([7.5])
    expect(engine.getSnapshot().playing).toBe(true)
    expect(engine.currentTime()).toBe(7.5)
  })

  it('moves the playhead without starting anything while paused', async () => {
    await loadEngine(twoStems)

    engine.seek(4)

    expect(context.sources).toHaveLength(0)
    expect(engine.currentTime()).toBe(4)
    expect(engine.getSnapshot().playing).toBe(false)
  })

  it('plays from a seek made while paused', async () => {
    await loadEngine(twoStems)
    engine.seek(4)
    context.currentTime = 9

    await engine.play()

    expect(startOffsets(context.sources)).toEqual([4])
    expect(startTimes(context.sources)).toEqual([9 + LOOKAHEAD])
  })

  it('clamps a seek to the mix duration', async () => {
    await loadEngine(twoStems)
    engine.seek(-5)
    expect(engine.currentTime()).toBe(0)
    engine.seek(9999)
    expect(engine.currentTime()).toBe(10)
  })
})

describe('StemAudioEngine mute and solo', () => {
  const order = ['vocals', 'drums', 'bass', 'other']

  beforeEach(async () => {
    await loadEngine(fourStems)
  })

  it('starts with every stem audible at full level', () => {
    expect(context.gains.map((gain) => gain.gain.value)).toEqual([1, 1, 1, 1])
    expect(engine.getSnapshot().stems.every((stem) => stem.audible)).toBe(true)
  })

  it('silences a muted stem and restores it on unmute', () => {
    engine.toggleMute('drums')
    expect(gainOf('drums', order)).toBe(0)
    expect(gainOf('vocals', order)).toBe(1)

    engine.toggleMute('drums')
    expect(gainOf('drums', order)).toBe(1)
  })

  it('silences every non-soloed stem', () => {
    engine.toggleSolo('vocals')

    expect(gainOf('vocals', order)).toBe(1)
    expect(gainOf('drums', order)).toBe(0)
    expect(gainOf('bass', order)).toBe(0)
    expect(gainOf('other', order)).toBe(0)
  })

  it('treats multiple solos as additive', () => {
    engine.toggleSolo('vocals')
    engine.toggleSolo('bass')

    expect(gainOf('vocals', order)).toBe(1)
    expect(gainOf('bass', order)).toBe(1)
    expect(gainOf('drums', order)).toBe(0)
    expect(gainOf('other', order)).toBe(0)
  })

  it('restores the previous mutes when the last solo is cleared', () => {
    engine.toggleMute('other')
    engine.toggleSolo('vocals')
    engine.toggleSolo('bass')
    expect(gainOf('other', order)).toBe(0)

    engine.toggleSolo('vocals')
    // One solo still stands, so nothing is restored yet.
    expect(gainOf('bass', order)).toBe(1)
    expect(gainOf('drums', order)).toBe(0)

    engine.toggleSolo('bass')
    expect(gainOf('vocals', order)).toBe(1)
    expect(gainOf('drums', order)).toBe(1)
    expect(gainOf('bass', order)).toBe(1)
    expect(gainOf('other', order)).toBe(0)
  })

  it('lets a soloed stem be heard even when it is also muted', () => {
    engine.toggleMute('vocals')
    engine.toggleSolo('vocals')

    expect(gainOf('vocals', order)).toBe(1)
    expect(engine.getSnapshot().stems[0]?.muted).toBe(true)
  })

  it('reflects mute and solo in the snapshot', () => {
    engine.toggleMute('drums')
    engine.toggleSolo('bass')

    const stems = engine.getSnapshot().stems
    expect(stems.map((stem) => stem.muted)).toEqual([false, true, false, false])
    expect(stems.map((stem) => stem.soloed)).toEqual([
      false,
      false,
      true,
      false,
    ])
    expect(stems.map((stem) => stem.audible)).toEqual([
      false,
      false,
      true,
      false,
    ])
  })

  it('scales an audible stem by its level', () => {
    engine.setLevel('bass', 0.25)
    expect(gainOf('bass', order)).toBe(0.25)

    engine.toggleMute('bass')
    expect(gainOf('bass', order)).toBe(0)

    engine.toggleMute('bass')
    expect(gainOf('bass', order)).toBe(0.25)
  })

  it('ignores mute, solo and level for an unknown stem', () => {
    engine.toggleMute('nope')
    engine.setSoloed('nope', true)
    engine.setLevel('nope', 0)
    expect(context.gains.map((gain) => gain.gain.value)).toEqual([1, 1, 1, 1])
  })
})

describe('StemAudioEngine subscription', () => {
  it('notifies subscribers when the snapshot changes', async () => {
    await loadEngine(twoStems)
    const listener = vi.fn()
    engine.subscribe(listener)

    engine.toggleMute('vocals')

    expect(listener).toHaveBeenCalledTimes(1)
    expect(engine.getSnapshot().stems[0]?.muted).toBe(true)
  })

  it('returns a stable snapshot between changes', async () => {
    await loadEngine(twoStems)
    expect(engine.getSnapshot()).toBe(engine.getSnapshot())

    const before = engine.getSnapshot()
    engine.toggleSolo('vocals')
    expect(engine.getSnapshot()).not.toBe(before)
  })

  it('stops notifying after unsubscribe', async () => {
    await loadEngine(twoStems)
    const listener = vi.fn()
    const unsubscribe = engine.subscribe(listener)

    unsubscribe()
    engine.toggleMute('vocals')

    expect(listener).not.toHaveBeenCalled()
  })
})

describe('StemAudioEngine disposal', () => {
  it('stops sources, disconnects nodes and closes the context', async () => {
    await loadEngine(fourStems)
    await engine.play()

    engine.dispose()

    expect(context.sources.every((source) => source.stopCount === 1)).toBe(true)
    expect(
      context.sources.every((source) => source.disconnectCount === 1),
    ).toBe(true)
    expect(context.gains.every((gain) => gain.disconnectCount === 1)).toBe(true)
    expect(context.closeCount).toBe(1)
    expect(engine.getSnapshot().playing).toBe(false)
  })

  it('is idempotent', async () => {
    await loadEngine(twoStems)
    engine.dispose()
    engine.dispose()
    expect(context.closeCount).toBe(1)
  })

  it('ignores transport commands after disposal', async () => {
    await loadEngine(twoStems)
    engine.dispose()

    await engine.play()
    engine.seek(5)

    expect(context.sources).toHaveLength(0)
    expect(engine.getSnapshot().status).toBe('idle')
  })

  it('never closes a context it never built', () => {
    const createContext = vi.fn(() => new FakeAudioContext())
    engine = createStemAudioEngine({ createContext })
    engine.dispose()
    expect(createContext).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Regressions from the PR #26 review.
// ---------------------------------------------------------------------------

describe('StemAudioEngine transport failures (review finding 3)', () => {
  /** A context whose first `resume()` rejects, as the autoplay policy does. */
  class RefusingContext extends FakeAudioContext {
    refuse = true

    override resume(): Promise<void> {
      this.resumeCount += 1
      if (this.refuse) {
        return Promise.reject(new Error('The user gesture was not accepted.'))
      }
      this.state = 'running'
      return Promise.resolve()
    }
  }

  it('reports a refused resume without rejecting', async () => {
    const refusing = new RefusingContext('suspended')
    engine = createStemAudioEngine({
      createContext: () => refusing,
      loadStemAudio: loaderFor(twoStems),
      lookaheadSeconds: LOOKAHEAD,
    })
    await engine.load(sources(twoStems))

    await expect(engine.play()).resolves.toBeUndefined()

    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.getSnapshot().error).toBeInstanceOf(Error)
  })

  it('clears the failure once a later play succeeds', async () => {
    const refusing = new RefusingContext('suspended')
    engine = createStemAudioEngine({
      createContext: () => refusing,
      loadStemAudio: loaderFor(twoStems),
      lookaheadSeconds: LOOKAHEAD,
    })
    await engine.load(sources(twoStems))
    await engine.play()
    expect(engine.getSnapshot().error).not.toBeNull()

    // The user clicks Play again, and this time the gesture is accepted.
    refusing.refuse = false
    await engine.play()

    expect(engine.getSnapshot().error).toBeNull()
    expect(engine.getSnapshot().playing).toBe(true)
  })

  it('does not let a transport failure erase a load failure', async () => {
    const refusing = new RefusingContext('suspended')
    engine = createStemAudioEngine({
      createContext: () => refusing,
      // One stem's file is gone; the other plays.
      loadStemAudio: loaderFor({ vocals: 10 }),
      lookaheadSeconds: LOOKAHEAD,
    })
    await engine.load(sources(twoStems))
    expect((engine.getSnapshot().error as ApiError).code).toBe(
      'stem_file_missing',
    )

    await engine.play()
    expect(engine.getSnapshot().error).toBeInstanceOf(Error)

    refusing.refuse = false
    await engine.play()

    // The missing stem has no remedy, so its failure outlives the transient.
    expect((engine.getSnapshot().error as ApiError).code).toBe(
      'stem_file_missing',
    )
  })
})

describe('StemAudioEngine never rejects (review finding 4)', () => {
  /** A context that dies the way a discarded tab's does. */
  class ClosingContext extends FakeAudioContext {
    override createGain(): never {
      throw new Error('AudioContext is closed')
    }
  }

  it('turns a context that dies mid-load into an error state', async () => {
    engine = createStemAudioEngine({
      createContext: () => new ClosingContext(),
      loadStemAudio: loaderFor(twoStems),
    })

    await expect(engine.load(sources(twoStems))).resolves.toBeUndefined()

    expect(engine.getSnapshot().status).toBe('error')
    expect(engine.getSnapshot().error).toBeInstanceOf(Error)
  })

  it('turns a context that dies at play time into an error state', async () => {
    class DyingContext extends FakeAudioContext {
      dying = false

      override createBufferSource(): FakeSourceNode {
        if (this.dying) {
          throw new Error('AudioContext is closed')
        }
        return super.createBufferSource()
      }
    }
    const dying = new DyingContext()
    engine = createStemAudioEngine({
      createContext: () => dying,
      loadStemAudio: loaderFor(twoStems),
    })
    await engine.load(sources(twoStems))
    dying.dying = true

    await expect(engine.play()).resolves.toBeUndefined()

    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.getSnapshot().error).toBeInstanceOf(Error)
  })

  it('turns a context that dies at seek time into an error state', async () => {
    class DyingContext extends FakeAudioContext {
      dying = false

      override createBufferSource(): FakeSourceNode {
        if (this.dying) {
          throw new Error('AudioContext is closed')
        }
        return super.createBufferSource()
      }
    }
    const dying = new DyingContext()
    engine = createStemAudioEngine({
      createContext: () => dying,
      loadStemAudio: loaderFor(twoStems),
    })
    await engine.load(sources(twoStems))
    await engine.play()
    dying.dying = true

    expect(() => {
      engine.seek(4)
    }).not.toThrow()
    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.getSnapshot().error).toBeInstanceOf(Error)
  })
})

describe('StemAudioEngine cancels downloads (review finding 5)', () => {
  it('aborts every in-flight stem download on dispose', async () => {
    const signals: (AbortSignal | undefined)[] = []
    const held = deferred<ArrayBuffer>()
    context = new FakeAudioContext()
    engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: (_url, signal) => {
        signals.push(signal)
        return held.promise
      },
    })
    const loading = engine.load(sources(fourStems))

    expect(signals).toHaveLength(4)
    expect(signals.every((signal) => signal?.aborted === false)).toBe(true)

    engine.dispose()

    expect(signals.every((signal) => signal?.aborted === true)).toBe(true)
    held.resolve(stemBytes(10))
    await expect(loading).resolves.toBeUndefined()
  })

  it('aborts the previous downloads when a new load supersedes them', async () => {
    const signals: (AbortSignal | undefined)[] = []
    context = new FakeAudioContext()
    engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: (url, signal) => {
        signals.push(signal)
        return loaderFor(twoStems)(url)
      },
    })

    const first = engine.load(sources(twoStems))
    const firstSignals = [...signals]
    const second = engine.load(sources(twoStems))

    expect(firstSignals.every((signal) => signal?.aborted === true)).toBe(true)
    await Promise.all([first, second])
    expect(engine.getSnapshot().status).toBe('ready')
  })
})

describe('StemAudioEngine load re-entrancy (review finding 6)', () => {
  it('never lets a superseded load publish its buffers', async () => {
    const first = deferred<ArrayBuffer>()
    let call = 0
    context = new FakeAudioContext()
    engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: () => {
        call += 1
        // The first load's two downloads hang; the second load's resolve.
        return call <= 2 ? first.promise : Promise.resolve(stemBytes(30))
      },
    })

    const slow = engine.load(sources({ vocals: 10, instrumental: 10 }))
    const fast = engine.load(sources({ drums: 30, bass: 30, other: 30 }))
    await fast

    expect(engine.getSnapshot().stems.map((stem) => stem.name)).toEqual([
      'drums',
      'bass',
      'other',
    ])
    expect(engine.getSnapshot().durationSeconds).toBe(30)
    const afterFast = context.gains.length

    // The superseded load now finishes. It must change nothing.
    first.resolve(stemBytes(10))
    await slow

    expect(engine.getSnapshot().stems.map((stem) => stem.name)).toEqual([
      'drums',
      'bass',
      'other',
    ])
    expect(engine.getSnapshot().durationSeconds).toBe(30)
    expect(engine.getSnapshot().status).toBe('ready')
    expect(context.gains).toHaveLength(afterFast)
  })

  it('drops the gain nodes of the load it supersedes', async () => {
    await loadEngine(twoStems)
    const firstGains = [...context.gains]

    await engine.load(sources(fourStems))

    expect(firstGains.every((gain) => gain.disconnectCount === 1)).toBe(true)
    expect(engine.getSnapshot().stems).toHaveLength(4)
  })
})

// ---------------------------------------------------------------------------
// Decoded buffers (feature 049): the seam a waveform view reads samples
// through. The fake encoding carries samples in the bytes, so a stem can be
// authored sample by sample and read back the same way.
// ---------------------------------------------------------------------------

describe('StemAudioEngine stem buffers', () => {
  /** Amplitudes chosen to survive the fake's 1/128 quantisation exactly. */
  const authored = [0, 0.5, -0.5, -1, 0.25]

  /** A loader that gives `vocals` authored samples and `drums` silence. */
  const sampleLoader: StemLoader = (url: string) =>
    url.endsWith('/vocals')
      ? Promise.resolve(stemBytesWithSamples(authored))
      : Promise.resolve(stemBytes(1))

  it('hands back the decoded samples of a loaded stem', async () => {
    await loadEngine({ vocals: 0.05, drums: 1 }, { load: sampleLoader })

    const buffer = engine.getStemBuffer('vocals')
    expect(buffer?.numberOfChannels).toBe(1)
    expect(buffer?.sampleRate).toBe(100)
    expect(buffer?.length).toBe(authored.length)
    expect([...(buffer?.getChannelData(0) ?? [])]).toEqual(authored)
    expect(buffer?.duration).toBeCloseTo(authored.length / 100, 10)
  })

  it('is null for an unknown stem name', async () => {
    await loadEngine(twoStems)
    expect(engine.getStemBuffer('trombone')).toBeNull()
  })

  it('is null before the stem has loaded', () => {
    engine = makeEngine(twoStems)
    expect(engine.getStemBuffer('vocals')).toBeNull()
  })

  it('is null for a stem whose audio failed to load', async () => {
    await loadEngine(twoStems, { load: loaderFor({ vocals: 10 }) })

    expect(engine.getStemBuffer('vocals')).not.toBeNull()
    expect(engine.getStemBuffer('instrumental')).toBeNull()
  })

  it('is null after disposal', async () => {
    await loadEngine(twoStems)
    expect(engine.getStemBuffer('vocals')).not.toBeNull()

    engine.dispose()

    expect(engine.getStemBuffer('vocals')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Loop regions (feature 053). The loop is the platform's own — `loop`,
// `loopStart` and `loopEnd` set on every source before the one shared
// `start()` — so these tests assert on the flags the engine wrote and on the
// playhead arithmetic that has to agree with them.
// ---------------------------------------------------------------------------

describe('StemAudioEngine loop regions', () => {
  /** Long enough that a 10 s region sits well inside the mix. */
  const longStems = { vocals: 30, instrumental: 30 }

  /** The loop flags of a set of sources, as `[loop, start, end]` triples. */
  function loopFlags(
    nodes: readonly FakeSourceNode[],
  ): [boolean, number, number][] {
    return nodes.map((node) => [node.loop, node.loopStart, node.loopEnd])
  }

  it('loops every stem natively, on one shared start time', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    context.currentTime = 2

    await engine.play()

    expect(context.sources).toHaveLength(2)
    expect(loopFlags(context.sources)).toEqual([
      [true, 10, 20],
      [true, 10, 20],
    ])
    // The whole point: one `when` for the mix, so every stem wraps on the
    // same sample rather than each on its own boundary.
    expect(startTimes(context.sources)).toEqual([2 + LOOKAHEAD])
    expect(startOffsets(context.sources)).toEqual([0])
    expect(engine.getSnapshot().loopRegion).toEqual({ start: 10, end: 20 })
  })

  it('wraps the playhead back into the region, however many passes on', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()

    // Raw 25 s of material is five seconds into the second pass.
    context.currentTime = 25 + LOOKAHEAD
    expect(engine.currentTime()).toBeCloseTo(15, 6)

    // …and 45 s is five seconds into the fourth, at the same place.
    context.currentTime = 45 + LOOKAHEAD
    expect(engine.currentTime()).toBeCloseTo(15, 6)

    // Before the region, playback simply runs towards it: no wrap.
    context.currentTime = 4 + LOOKAHEAD
    expect(engine.currentTime()).toBeCloseTo(4, 6)
  })

  it('is a trap, not a fence: a seek past the end runs straight through', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()

    engine.seek(25)

    // The sources still carry the region — nothing was cleared — but they
    // started past it, so the platform never re-enters it and neither does
    // the playhead.
    const after = context.sourcesFrom(2)
    expect(loopFlags(after)).toEqual([
      [true, 10, 20],
      [true, 10, 20],
    ])
    context.currentTime = 3 + LOOKAHEAD
    expect(engine.currentTime()).toBeCloseTo(28, 6)

    // And the mix still ends the way it always did.
    after
      .find((source) => source.onended !== null)
      ?.onended?.(new Event('ended'))
    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.currentTime()).toBe(30)
  })

  it('rebuilds at the wrapped position when the region is cleared mid-play', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()
    context.currentTime = 25 + LOOKAHEAD

    engine.clearLoopRegion()

    const after = context.sourcesFrom(2)
    expect(after).toHaveLength(2)
    expect(loopFlags(after)).toEqual([
      [false, 0, 0],
      [false, 0, 0],
    ])
    // Resumed from where the audio actually was — inside the region, not at
    // the 25 s the raw clock would have suggested.
    const offsets = startOffsets(after)
    expect(offsets).toHaveLength(1)
    expect(offsets[0] as number).toBeCloseTo(15, 6)
    expect(engine.getSnapshot().loopRegion).toBeNull()
  })

  it('rebuilds at the current position when a region is set mid-play', async () => {
    await loadEngine(longStems)
    await engine.play()
    context.currentTime = 6 + LOOKAHEAD

    engine.setLoopRegion(2, 8)

    const after = context.sourcesFrom(2)
    expect(loopFlags(after)).toEqual([
      [true, 2, 8],
      [true, 2, 8],
    ])
    const offsets = startOffsets(after)
    expect(offsets[0] as number).toBeCloseTo(6, 6)
  })

  it('leaves the sources alone while paused, and loops from the next play', async () => {
    await loadEngine(longStems)

    engine.setLoopRegion(10, 20)

    expect(context.sources).toHaveLength(0)
    expect(engine.getSnapshot().loopRegion).toEqual({ start: 10, end: 20 })

    await engine.play()
    expect(loopFlags(context.sources)).toEqual([
      [true, 10, 20],
      [true, 10, 20],
    ])
  })

  it('survives a pause and a resume', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()
    context.currentTime = 12 + LOOKAHEAD

    engine.pause()
    expect(engine.getSnapshot().loopRegion).toEqual({ start: 10, end: 20 })
    await engine.play()

    expect(loopFlags(context.sourcesFrom(2))).toEqual([
      [true, 10, 20],
      [true, 10, 20],
    ])
  })

  it('pauses at the wrapped position and resumes from it', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()
    // Two full passes plus five seconds: audibly at 0:15, raw clock at 0:35.
    context.currentTime = 35 + LOOKAHEAD

    engine.pause()

    // The stored position is the wrapped one — what the user hears — not the
    // raw elapsed time, so the readout holds at 0:15 while paused…
    expect(engine.currentTime()).toBeCloseTo(15, 6)

    await engine.play()

    // …and resume starts every source from inside the region, still looping.
    const resumed = context.sourcesFrom(2)
    expect(loopFlags(resumed)).toEqual([
      [true, 10, 20],
      [true, 10, 20],
    ])
    for (const source of resumed) {
      expect(source.started?.offset).toBeCloseTo(15, 6)
    }
  })

  it('does not rebuild the graph for the region it already has', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()

    engine.setLoopRegion(10, 20)

    // A handle dragged back to where it started must not cost a teardown and
    // a fresh lookahead.
    expect(context.sources).toHaveLength(2)
  })

  it.each([
    ['an empty region', 5, 5],
    ['an inverted one', 20, 10],
    ['one shorter than the minimum', 5, 5.005],
    ['one entirely past the end of the mix', 40, 50],
  ])('clears rather than setting %s', async (_label, start, end) => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    expect(engine.getSnapshot().loopRegion).not.toBeNull()

    engine.setLoopRegion(start, end)

    expect(engine.getSnapshot().loopRegion).toBeNull()
  })

  it('clamps a region that overhangs the mix', async () => {
    await loadEngine(longStems)

    engine.setLoopRegion(-5, 45)

    expect(engine.getSnapshot().loopRegion).toEqual({ start: 0, end: 30 })
  })

  it('never asks a stem to wrap past its own end', async () => {
    // A short stem and a stem that is over before the region even begins.
    await loadEngine({ vocals: 30, instrumental: 15, drums: 5 })
    engine.setLoopRegion(10, 20)

    await engine.play()

    expect(loopFlags(context.sources)).toEqual([
      [true, 10, 20],
      // Clamped to its own duration rather than told to read past it.
      [true, 10, 15],
      // Nothing of this stem is inside the region, so it runs straight
      // through — there is nothing to repeat.
      [false, 0, 0],
    ])
  })

  it('forgets the region when a new job is loaded', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)

    await engine.load(sources(fourStems))

    expect(engine.getSnapshot().loopRegion).toBeNull()
    await engine.play()
    expect(context.sources.every((source) => !source.loop)).toBe(true)
  })

  it('notifies subscribers when the region changes, and only then', async () => {
    await loadEngine(longStems)
    const listener = vi.fn()
    engine.subscribe(listener)

    engine.setLoopRegion(10, 20)
    expect(listener).toHaveBeenCalledTimes(1)

    // Already cleared: nothing changed, so nothing is published.
    engine.clearLoopRegion()
    engine.clearLoopRegion()
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('ignores loop commands after disposal', async () => {
    await loadEngine(longStems)
    engine.dispose()

    engine.setLoopRegion(10, 20)

    expect(engine.getSnapshot().loopRegion).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Feature 052: the audible scrub preview
//
// Sound cannot be asserted, so every case here asserts on what was
// *scheduled*: throwaway grain nodes with one shared start time, an envelope
// with a real shape, and a routing that proves mute/solo/level apply to them
// without any of it being recomputed. The throttle is asserted against the
// fake clock — the engine reads `context.currentTime` and nothing else, so a
// test moves time by assignment and never by waiting.
// ---------------------------------------------------------------------------

describe('StemAudioEngine scrub preview', () => {
  /** Long enough to scrub around in. */
  const longStems = { vocals: 30, instrumental: 30 }

  /** The documented defaults, which are part of this feature's contract. */
  const GRAIN = 0.09
  const RETRIGGER = 0.06
  const FADE = 0.008

  /** The envelope events one grain's gain node should have received. */
  function grainEnvelope(when: number) {
    return [
      { type: 'setValueAtTime', value: 0, time: when },
      { type: 'linearRamp', value: 1, time: when + FADE },
      { type: 'setValueAtTime', value: 1, time: when + GRAIN - FADE },
      { type: 'linearRamp', value: 0, time: when + GRAIN },
    ]
  }

  it('sounds a grain of every stem at the scrubbed position', async () => {
    await loadEngine(longStems)
    context.currentTime = 3

    engine.beginScrubPreview()
    engine.scrubPreview(12)

    const grains = context.sourcesFrom(0)
    expect(grains).toHaveLength(2)
    // One clock reading for the set, exactly as a transport generation gets:
    // the two stems of a grain have to be aligned with each other too.
    expect(startTimes(grains)).toEqual([3 + LOOKAHEAD])
    expect(startOffsets(grains)).toEqual([12])
    for (const grain of grains) {
      expect(grain.buffer).not.toBeNull()
      expect(grain.stops).toEqual([3 + LOOKAHEAD + GRAIN])
    }
  })

  it('shapes every grain with a four-point envelope', async () => {
    await loadEngine(longStems)

    engine.beginScrubPreview()
    engine.scrubPreview(12)

    // The stem gains came from the load; the envelopes are the two after them.
    const envelopes = context.gains.slice(2)
    expect(envelopes).toHaveLength(2)
    for (const envelope of envelopes) {
      expect(envelope.gain.events).toEqual(grainEnvelope(LOOKAHEAD))
    }
  })

  it('routes each grain through its own stem gain node', async () => {
    await loadEngine(longStems)

    engine.beginScrubPreview()
    engine.scrubPreview(12)

    const [vocalsGain, instrumentalGain, vocalsEnv, instrumentalEnv] =
      context.gains
    const [vocalsGrain, instrumentalGrain] = context.sources
    // source → envelope → the stem's own gain: mute, solo and level are
    // inherited by construction rather than recomputed for the preview.
    expect(vocalsGrain?.connections).toEqual([vocalsEnv])
    expect(vocalsEnv?.connections).toEqual([vocalsGain])
    expect(instrumentalGrain?.connections).toEqual([instrumentalEnv])
    expect(instrumentalEnv?.connections).toEqual([instrumentalGain])
  })

  it('auditions a muted stem through the gain that silences it', async () => {
    await loadEngine(longStems)
    engine.setMuted('vocals', true)

    engine.beginScrubPreview()
    engine.scrubPreview(12)

    // Not a second mute check — the routing above *is* the mechanism, so this
    // asserts the grain lands in the node whose gain is already zero. Nothing
    // in the preview path resolves mute, solo or level a second time.
    expect(context.gains[2]?.connections).toEqual([context.gains[0]])
    expect(gainOf('vocals', ['vocals', 'instrumental'])).toBe(0)
    expect(gainOf('instrumental', ['vocals', 'instrumental'])).toBe(1)
  })

  it('drops previews inside the retrigger window, and takes the next one', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()

    engine.scrubPreview(12)
    engine.scrubPreview(13)
    engine.scrubPreview(14)

    // Three pointer moves inside one retrigger window are one grain set: the
    // extra positions are dropped, not queued, because by the time a queued
    // grain played the pointer would be somewhere else.
    expect(context.sources).toHaveLength(2)
    expect(startOffsets(context.sources)).toEqual([12])

    // The window is the audio clock's, so moving the clock past it — and
    // nothing else — is what admits the next grain.
    context.currentTime = LOOKAHEAD + RETRIGGER
    engine.scrubPreview(20)

    const second = context.sourcesFrom(2)
    expect(second).toHaveLength(2)
    expect(startOffsets(second)).toEqual([20])
  })

  it('silences the transport at begin and remembers that it was playing', async () => {
    await loadEngine(longStems)
    await engine.play()
    context.currentTime = 5 + LOOKAHEAD

    engine.beginScrubPreview()

    // Audacity pauses while you scrub: what is heard is the preview.
    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.getSnapshot().scrubbing).toBe(true)
    expect(engine.currentTime()).toBeCloseTo(5, 6)
    for (const source of context.sourcesFrom(0)) {
      expect(source.stopCount).toBe(1)
    }
  })

  it('commits the release as the gesture’s one transport move', async () => {
    await loadEngine(longStems)
    await engine.play()
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    const before = context.sources.length

    engine.endScrubPreview(22)

    const resumed = context.sourcesFrom(before)
    expect(resumed).toHaveLength(2)
    expect(startOffsets(resumed)).toEqual([22])
    expect(startTimes(resumed)).toHaveLength(1)
    expect(engine.getSnapshot().playing).toBe(true)
    expect(engine.getSnapshot().scrubbing).toBe(false)
    expect(engine.currentTime()).toBeCloseTo(22, 6)
  })

  it('reapplies the loop region to the generation the commit starts', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()
    engine.beginScrubPreview()
    const before = context.sources.length

    engine.endScrubPreview(12)

    // Nothing here reapplies anything: `startSources` is the one place a
    // generation is built, and the region lives there (feature 053).
    for (const source of context.sourcesFrom(before)) {
      expect([source.loop, source.loopStart, source.loopEnd]).toEqual([
        true,
        10,
        20,
      ])
    }
  })

  it('never loops a grain, even inside a loop region', async () => {
    await loadEngine(longStems)
    engine.setLoopRegion(10, 20)
    await engine.play()

    engine.beginScrubPreview()
    engine.scrubPreview(15)

    // A grain auditions one position and stops; a looping one would repeat a
    // fragment under the pointer for as long as it was held (053, note 10).
    // It ignores the region entirely, which is also why its offset may sit
    // outside one.
    for (const grain of context.sourcesFrom(2)) {
      expect(grain.loop).toBe(false)
      expect(grain.stops).toEqual([LOOKAHEAD + GRAIN])
    }
  })

  it('fades the grains out and disconnects them when the session ends', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    // Mid-grain: the grain runs to LOOKAHEAD + GRAIN, so at 0.1 it is still
    // sounding and the release must ramp it down rather than cut it.
    context.currentTime = 0.1

    engine.endScrubPreview(12)

    for (const envelope of context.gains.slice(2)) {
      expect(envelope.gain.events.slice(-2)).toEqual([
        { type: 'cancel', time: 0.1 },
        { type: 'linearRamp', value: 0, time: 0.1 + FADE },
      ])
      // Not yet: a disconnect now would sever the graph before the ramp
      // finishes, so the teardown waits for the node to report it ended.
      expect(envelope.disconnectCount).toBe(0)
    }
    for (const grain of context.sources) {
      expect(grain.stops.at(-1)).toBe(0.1 + FADE)
      expect(grain.disconnectCount).toBe(0)
      grain.onended?.(new Event('ended'))
      expect(grain.disconnectCount).toBe(1)
    }
    for (const envelope of context.gains.slice(2)) {
      expect(envelope.disconnectCount).toBe(1)
    }
  })

  it('disconnects a grain that already played out without waiting for it', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    // Past LOOKAHEAD + GRAIN: the grain ended on its own schedule, so its
    // `onended` has already fired and would never fire again — a deferred
    // disconnect would leak the envelope until the next load. It is silent,
    // so the immediate cut costs nothing.
    context.currentTime = 0.2

    engine.endScrubPreview(12)

    for (const grain of context.sources) {
      expect(grain.disconnectCount).toBe(1)
    }
    for (const envelope of context.gains.slice(2)) {
      expect(envelope.disconnectCount).toBe(1)
    }
  })

  it('stays paused when the transport was paused at begin', async () => {
    await loadEngine(longStems)

    engine.beginScrubPreview()
    engine.scrubPreview(12)
    engine.endScrubPreview(22)

    // The two sources are the grain; nothing was started for the transport.
    expect(context.sources).toHaveLength(2)
    expect(engine.getSnapshot().playing).toBe(false)
    expect(engine.currentTime()).toBeCloseTo(22, 6)
  })

  it('leaves the playhead alone when a session ends with no commit', async () => {
    await loadEngine(longStems)
    engine.seek(8)

    engine.beginScrubPreview()
    engine.scrubPreview(25)
    engine.endScrubPreview()

    // A cancelled gesture (pointercancel) commits nothing: auditioning 0:25
    // never moved the playhead off 0:08.
    expect(engine.currentTime()).toBeCloseTo(8, 6)
    expect(engine.getSnapshot().scrubbing).toBe(false)
  })

  it('clamps the auditioned position to the mix', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()

    engine.scrubPreview(1000)

    expect(startOffsets(context.sources)).toEqual([30])
  })

  it('ignores previews and ends with no session open', async () => {
    await loadEngine(longStems)

    engine.scrubPreview(12)
    engine.endScrubPreview(22)

    expect(context.sources).toHaveLength(0)
    expect(engine.currentTime()).toBe(0)
  })

  it('is idempotent while a session is open', async () => {
    await loadEngine(longStems)
    await engine.play()
    context.currentTime = 5 + LOOKAHEAD
    engine.beginScrubPreview()

    // A second begin must not decide, again, that the transport was playing —
    // by now it is not, and the release would never resume.
    engine.beginScrubPreview()
    engine.endScrubPreview(22)

    expect(engine.getSnapshot().playing).toBe(true)
  })

  it.each([
    ['play', (target: StemPlayerEngine) => void target.play()],
    [
      'pause',
      (target: StemPlayerEngine) => {
        target.pause()
      },
    ],
    [
      'seek',
      (target: StemPlayerEngine) => {
        target.seek(25)
      },
    ],
  ])('closes an open session on %s', async (_label, command) => {
    await loadEngine(longStems)
    engine.beginScrubPreview()
    engine.scrubPreview(12)

    command(engine)
    await Promise.resolve()

    expect(engine.getSnapshot().scrubbing).toBe(false)
    for (const grain of context.sourcesFrom(0).slice(0, 2)) {
      expect(grain.stopCount).toBeGreaterThan(0)
      // Disconnection is deferred to the node's own end, past the fade.
      grain.onended?.(new Event('ended'))
      expect(grain.disconnectCount).toBe(1)
    }
  })

  it('folds a mid-drag seek into one generation rather than two', async () => {
    await loadEngine(longStems)
    await engine.play()
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    const before = context.sources.length

    // This is the keyboard commit that lands while a pointer drag is still in
    // flight: the session closes without resuming, so the seek's own
    // generation is the only one built.
    engine.seek(25)

    const after = context.sourcesFrom(before)
    expect(after).toHaveLength(2)
    expect(startOffsets(after)).toEqual([25])
    expect(engine.getSnapshot().playing).toBe(true)
  })

  it('stops every grain when a new job is loaded', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    const grains = context.sourcesFrom(0)

    await engine.load(sources(fourStems))

    for (const grain of grains) {
      // Two stops: the one the grain was scheduled with, and the teardown's.
      expect(grain.stops).toEqual([LOOKAHEAD + GRAIN, undefined])
      expect(grain.disconnectCount).toBe(1)
    }
    expect(engine.getSnapshot().scrubbing).toBe(false)
  })

  it('stops every grain on disposal, and ignores everything after it', async () => {
    await loadEngine(longStems)
    engine.beginScrubPreview()
    engine.scrubPreview(12)
    const grains = context.sourcesFrom(0)

    engine.dispose()

    for (const grain of grains) {
      // Two stops: the one the grain was scheduled with, and the teardown's.
      expect(grain.stops).toEqual([LOOKAHEAD + GRAIN, undefined])
      expect(grain.disconnectCount).toBe(1)
    }
    engine.beginScrubPreview()
    engine.scrubPreview(20)
    engine.endScrubPreview(20)
    expect(context.sources).toHaveLength(2)
    expect(engine.getSnapshot().scrubbing).toBe(false)
  })

  it('refuses a session until the stems are ready', async () => {
    engine = makeEngine(longStems)

    engine.beginScrubPreview()
    engine.scrubPreview(12)

    expect(engine.getSnapshot().scrubbing).toBe(false)
    expect(context.sources).toHaveLength(0)
  })

  it('publishes the session in the snapshot, and only on a change', async () => {
    await loadEngine(longStems)
    const listener = vi.fn()
    engine.subscribe(listener)

    engine.beginScrubPreview()
    expect(engine.getSnapshot().scrubbing).toBe(true)
    expect(listener).toHaveBeenCalledTimes(1)

    // Previews are not state: a grain a second does not publish a snapshot.
    engine.scrubPreview(12)
    expect(listener).toHaveBeenCalledTimes(1)

    engine.endScrubPreview(12)
    expect(engine.getSnapshot().scrubbing).toBe(false)
    expect(listener).toHaveBeenCalledTimes(2)

    engine.endScrubPreview(20)
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('honours configured grain, retrigger and fade lengths', async () => {
    context = new FakeAudioContext()
    engine = createStemAudioEngine({
      createContext: () => context,
      loadStemAudio: loaderFor(longStems),
      lookaheadSeconds: LOOKAHEAD,
      scrubGrainSeconds: 0.2,
      scrubRetriggerSeconds: 0.15,
      scrubFadeSeconds: 0.02,
    })
    await engine.load(sources(longStems))

    engine.beginScrubPreview()
    engine.scrubPreview(12)
    // Inside the configured window rather than the default one.
    context.currentTime = LOOKAHEAD + 0.1
    engine.scrubPreview(13)

    expect(context.sources).toHaveLength(2)
    expect(context.sources[0]?.stops).toEqual([LOOKAHEAD + 0.2])
    expect(context.gains[2]?.gain.events[1]).toEqual({
      type: 'linearRamp',
      value: 1,
      time: LOOKAHEAD + 0.02,
    })
  })
})

// ---------------------------------------------------------------------------
// Retrying a failed stem (feature 064). The seam is the loader: a stem is
// "gone" while its name is absent from the durations table and "back" once a
// test puts it there, so a recovery needs no second engine and no reload.
// ---------------------------------------------------------------------------

describe('StemAudioEngine retrying a failed stem', () => {
  /** A loader that also records which stem names it was asked for. */
  function countingLoader(durations: Record<string, number>): {
    load: StemLoader
    calls: string[]
  } {
    const calls: string[] = []
    const inner = loaderFor(durations)
    return {
      calls,
      load: (url, signal) => {
        calls.push(url.slice(url.lastIndexOf('/') + 1))
        return inner(url, signal)
      },
    }
  }

  it('recovers a stem whose audio failed, and recomputes the mix', async () => {
    // `instrumental` is absent from the table, so its fetch 404s.
    const available: Record<string, number> = { vocals: 10 }
    await loadEngine(twoStems, { load: loaderFor(available) })
    expect(engine.getSnapshot().stems[1]?.status).toBe('error')
    expect(engine.getSnapshot().error).not.toBeNull()

    // The file is there this time — and longer than the one that loaded, so
    // the recomputed duration cannot be mistaken for the old one.
    available.instrumental = 12.5
    await engine.retryFailedStems()

    const snapshot = engine.getSnapshot()
    expect(snapshot.stems.every((stem) => stem.status === 'loaded')).toBe(true)
    expect(snapshot.stems.every((stem) => stem.error === null)).toBe(true)
    expect(snapshot.status).toBe('ready')
    expect(snapshot.durationSeconds).toBe(12.5)
    // The load failure has an answer now, so it must not stand.
    expect(snapshot.error).toBeNull()
    expect(engine.getStemBuffer('instrumental')?.duration).toBe(12.5)
  })

  it('leaves every stem that already loaded exactly as it was', async () => {
    const available: Record<string, number> = { vocals: 10 }
    await loadEngine(twoStems, { load: loaderFor(available) })
    const gain = context.gains[0]
    const buffer = engine.getStemBuffer('vocals')

    available.instrumental = 10
    await engine.retryFailedStems()

    // Same buffer object, same gain node, never disconnected: recovering one
    // stem must not cost the stems the user is already listening to.
    expect(engine.getStemBuffer('vocals')).toBe(buffer)
    expect(context.gains[0]).toBe(gain)
    expect(gain?.disconnectCount).toBe(0)
    // One new decode, not a whole set: the loaded stem was never re-fetched.
    expect(context.decoded).toHaveLength(2)
    expect(context.gains).toHaveLength(2)
  })

  it('does nothing at all when no stem failed', async () => {
    const loader = countingLoader(twoStems)
    await loadEngine(twoStems, { load: loader.load })
    expect(loader.calls).toEqual(['vocals', 'instrumental'])
    const before = engine.getSnapshot()

    await engine.retryFailedStems()

    // Not one request, and not one new snapshot: a transport failure (a
    // refused `resume()`) has nothing here to retry, and `play()` is its
    // remedy.
    expect(loader.calls).toEqual(['vocals', 'instrumental'])
    expect(engine.getSnapshot()).toBe(before)
    expect(context.gains).toHaveLength(2)
  })

  it('asks only for the stems that failed', async () => {
    const available: Record<string, number> = { vocals: 10 }
    const loader = countingLoader(available)
    await loadEngine(fourStems, { load: loader.load })
    loader.calls.length = 0

    available.drums = 10
    await engine.retryFailedStems()

    expect(loader.calls).toEqual(['drums', 'bass', 'other'])
  })

  it('re-reports a stem that fails again, without rejecting', async () => {
    await loadEngine(twoStems, { load: loaderFor({ vocals: 10 }) })

    await expect(engine.retryFailedStems()).resolves.toBeUndefined()

    const snapshot = engine.getSnapshot()
    const failed = snapshot.stems.find((stem) => stem.name === 'instrumental')
    expect(failed?.status).toBe('error')
    expect((failed?.error as ApiError).code).toBe('stem_file_missing')
    expect((snapshot.error as ApiError).code).toBe('stem_file_missing')
    // …and the stem that did load is still playable.
    expect(snapshot.status).toBe('ready')
    expect(snapshot.durationSeconds).toBe(10)
  })

  it('joins a recovered stem to a mix that is already playing', async () => {
    const available: Record<string, number> = { vocals: 10 }
    await loadEngine(twoStems, { load: loaderFor(available) })
    await engine.play()
    // Only the stem that loaded is running.
    expect(context.sources).toHaveLength(1)
    context.currentTime = 4

    available.instrumental = 10
    await engine.retryFailedStems()

    // One new generation, both stems in it, one shared `when` and one shared
    // offset — the position the mix had actually reached.
    const rebuilt = context.sourcesFrom(1)
    expect(rebuilt).toHaveLength(2)
    expect(startTimes(rebuilt)).toEqual([4 + LOOKAHEAD])
    expect(startOffsets(rebuilt)).toEqual([4 - LOOKAHEAD])
    expect(rebuilt[1]?.connections).toEqual([context.gains[1]])
    expect(engine.getSnapshot().playing).toBe(true)
  })

  it('joins at the wrapped position when a loop region is running', async () => {
    const longStems = { vocals: 30, instrumental: 30 }
    const available: Record<string, number> = { vocals: 30 }
    await loadEngine(longStems, { load: loaderFor(available) })
    engine.setLoopRegion(10, 20)
    await engine.play()
    // 25 s of raw material into a 10–20 s region entered at 0 wraps to 14.95
    // (the lookahead is still owed at the front).
    context.currentTime = 25
    expect(engine.currentTime()).toBeCloseTo(14.95, 6)

    available.instrumental = 30
    await engine.retryFailedStems()

    const rebuilt = context.sourcesFrom(1)
    expect(rebuilt).toHaveLength(2)
    expect(startTimes(rebuilt)).toEqual([25 + LOOKAHEAD])
    expect(rebuilt[0]?.started?.offset).toBeCloseTo(14.95, 6)
    expect(startOffsets(rebuilt)).toHaveLength(1)
    // The region survives the rebuild: `startSources` is where the flags are
    // set, so the recovered stem wraps with the rest from its first sample.
    expect(rebuilt.every((source) => source.loop)).toBe(true)
    expect(rebuilt.every((source) => source.loopEnd === 20)).toBe(true)
  })

  it('publishes nothing when a load supersedes the retry', async () => {
    const held = deferred<ArrayBuffer>()
    let call = 0
    context = new FakeAudioContext()
    engine = createStemAudioEngine({
      createContext: () => context,
      lookaheadSeconds: LOOKAHEAD,
      loadStemAudio: (url) => {
        call += 1
        // Calls 1–2 are the initial load (`instrumental` 404s); call 3 is the
        // retry's fetch, held open; the rest belong to the new job.
        if (call <= 2) {
          return loaderFor({ vocals: 10 })(url)
        }
        return call === 3 ? held.promise : Promise.resolve(stemBytes(30))
      },
    })
    await engine.load(sources(twoStems))
    expect(engine.getSnapshot().stems[1]?.status).toBe('error')

    const retry = engine.retryFailedStems()
    await engine.load(sources(fourStems))
    const afterLoad = context.gains.length

    // The orphaned retry now finishes. It must change nothing.
    held.resolve(stemBytes(10))
    await expect(retry).resolves.toBeUndefined()

    expect(engine.getSnapshot().stems.map((stem) => stem.name)).toEqual([
      'vocals',
      'drums',
      'bass',
      'other',
    ])
    expect(engine.getSnapshot().durationSeconds).toBe(30)
    expect(engine.getSnapshot().status).toBe('ready')
    expect(context.gains).toHaveLength(afterLoad)
  })

  it('ignores a retry after disposal', async () => {
    const loader = countingLoader({ vocals: 10 })
    await loadEngine(twoStems, { load: loader.load })
    engine.dispose()

    await expect(engine.retryFailedStems()).resolves.toBeUndefined()

    expect(loader.calls).toEqual(['vocals', 'instrumental'])
  })
})

describe('StemAudioEngine scheduling defaults', () => {
  it('keeps the scrub fade shorter than the scheduling lookahead', () => {
    // Feature 052's known limitation, pinned. A motionless click opens a
    // preview session and closes it in the same tick: the release schedules
    // `stop(now + fade)` while the grain is scheduled to start at
    // `now + lookahead`, so the click is silent only while the stop lands
    // first. Nothing else in the suite notices if one default moves.
    expect(DEFAULT_SCRUB_FADE_SECONDS).toBeLessThan(DEFAULT_LOOKAHEAD_SECONDS)
  })
})

describe('createStemAudioEngine', () => {
  it('builds a StemAudioEngine', () => {
    engine = createStemAudioEngine({
      createContext: () => new FakeAudioContext(),
    })
    expect(engine).toBeInstanceOf(StemAudioEngine)
  })
})
