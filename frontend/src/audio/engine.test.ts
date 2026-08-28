import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import {
  createStemAudioEngine,
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

describe('createStemAudioEngine', () => {
  it('builds a StemAudioEngine', () => {
    engine = createStemAudioEngine({
      createContext: () => new FakeAudioContext(),
    })
    expect(engine).toBeInstanceOf(StemAudioEngine)
  })
})
