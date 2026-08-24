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

/** A loader that answers each stem URL with bytes of that stem's duration. */
function loaderFor(
  durations: Record<string, number>,
): (url: string) => Promise<ArrayBuffer> {
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

const twoStems = { vocals: 10, instrumental: 10 }
const fourStems = { vocals: 10, drums: 10, bass: 10, other: 10 }

let context: FakeAudioContext
let engine: StemPlayerEngine

function makeEngine(
  durations: Record<string, number>,
  options: {
    state?: string
    load?: (url: string) => Promise<ArrayBuffer>
  } = {},
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
  options: {
    state?: string
    load?: (url: string) => Promise<ArrayBuffer>
  } = {},
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

describe('createStemAudioEngine', () => {
  it('builds a StemAudioEngine', () => {
    engine = createStemAudioEngine({
      createContext: () => new FakeAudioContext(),
    })
    expect(engine).toBeInstanceOf(StemAudioEngine)
  })
})
