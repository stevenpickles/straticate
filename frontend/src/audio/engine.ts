/**
 * Web Audio playback engine for a completed job's separated stems.
 *
 * Deliberately free of React: it is a plain object with a
 * subscribe/getSnapshot pair, so a component can read it through
 * `useSyncExternalStore` while the engine itself stays unit-testable and
 * reusable by any later feature.
 *
 * **Synchronisation.** Every stem is decoded into its own `AudioBuffer` and
 * played through its own `AudioBufferSourceNode`, but all of them are
 * scheduled with **one** `start(when, offset)` call sharing a single `when`
 * and a single `offset`, both computed once per transport change. Reading
 * `currentTime` again for each stem — or starting them in a loop "at about
 * the same time" — would smear them apart by however long the loop took;
 * scheduling them against one clock reading is what makes the mix
 * sample-accurate.
 *
 * **Seeking.** An `AudioBufferSourceNode` is single-use, so a seek stops the
 * current sources and creates a fresh set at the new offset, again with one
 * shared start time. The playhead is always derived from the context clock
 * (`startOffset + (context.currentTime - scheduledStart)`), never from an
 * interval counter that would drift away from what is actually audible.
 *
 * **Testability.** The `AudioContext` is a constructor parameter defaulting
 * to `new AudioContext()`, and so is the loader that turns a stem URL into
 * bytes. jsdom implements neither `AudioContext` nor `decodeAudioData`, so
 * this injection is what makes the engine testable at all: tests pass a fake
 * context that records scheduled start times, offsets, gain values and node
 * connections, and assert on what was *scheduled* rather than on sound.
 */

import { fetchStemAudio } from '../api/stems'

/** A stem to load: its contract name and the URL its audio is served from. */
export interface StemSource {
  /** The stem's name, exactly as `SeparationResult.stems[].name` gives it. */
  readonly name: string
  /** URL the stem's audio bytes are fetched from. */
  readonly url: string
}

/** The subset of `AudioParam` the engine uses. */
export interface AudioEngineParam {
  value: number
}

/** The subset of `AudioNode` the engine uses. */
export interface AudioEngineNode {
  connect(destination: AudioEngineNode): void
  disconnect(): void
}

/** The subset of `GainNode` the engine uses (one per stem: mute/solo/level). */
export interface AudioEngineGainNode extends AudioEngineNode {
  readonly gain: AudioEngineParam
}

/** The subset of `AudioBuffer` the engine uses. */
export interface AudioEngineBuffer {
  readonly duration: number
}

/** The subset of `AudioBufferSourceNode` the engine uses. */
export interface AudioEngineSourceNode extends AudioEngineNode {
  buffer: AudioEngineBuffer | null
  onended: ((event: Event) => void) | null
  start(when?: number, offset?: number): void
  stop(when?: number): void
}

/**
 * The subset of `AudioContext` the engine uses. A real `AudioContext`
 * satisfies it; so does a test fake, which is the point.
 */
export interface AudioEngineContext {
  /** The context's monotonic audio clock, in seconds. */
  readonly currentTime: number
  /** `suspended` until a user gesture resumes it (autoplay policy). */
  readonly state: string
  /** Where every stem's gain node connects. */
  readonly destination: AudioEngineNode
  createGain(): AudioEngineGainNode
  createBufferSource(): AudioEngineSourceNode
  decodeAudioData(data: ArrayBuffer): Promise<AudioEngineBuffer>
  resume(): Promise<void>
  close(): Promise<void>
}

/** Load state of one stem's audio. */
export type StemLoadStatus = 'loading' | 'loaded' | 'error'

/**
 * Overall engine state: `idle` before anything is loaded, `loading` while
 * stems decode, `ready` once at least one stem is playable, `error` when
 * none is.
 */
export type StemEngineStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Per-stem state exposed to the UI. */
export interface StemState {
  /** The stem's contract name. */
  readonly name: string
  /** Whether this stem's audio loaded, is still loading, or failed. */
  readonly status: StemLoadStatus
  /** The rejection that failed this stem's load, or `null`. */
  readonly error: unknown
  /** Whether the user muted this stem. */
  readonly muted: boolean
  /** Whether the user soloed this stem. */
  readonly soloed: boolean
  /** Whether this stem is currently heard, after mute/solo resolution. */
  readonly audible: boolean
  /** Playback level in `0..1` (independent of mute/solo). */
  readonly level: number
  /** Decoded duration in seconds, or `0` before the stem loads. */
  readonly durationSeconds: number
}

/** Immutable view of the engine, safe to render from. */
export interface StemEngineSnapshot {
  /** Overall engine state. */
  readonly status: StemEngineStatus
  /** Per-stem state, in the order the stems were loaded. */
  readonly stems: readonly StemState[]
  /** Whether sources are currently scheduled and running. */
  readonly playing: boolean
  /** Longest stem duration in seconds — the transport's full extent. */
  readonly durationSeconds: number
  /** The first load failure, or `null` when every stem loaded. */
  readonly error: unknown
}

/**
 * The playback surface a UI needs. `StemAudioEngine` implements it; a test
 * can substitute any other object with the same shape.
 */
export interface StemPlayerEngine {
  /** Fetch and decode every stem. Never rejects: failures land in the snapshot. */
  load(sources: readonly StemSource[]): Promise<void>
  /** Resume the context if suspended, then start every stem together. */
  play(): Promise<void>
  /** Stop playback, keeping the playhead where it is. */
  pause(): void
  /** Move the playhead, restarting every stem together when playing. */
  seek(seconds: number): void
  /** Mute or unmute one stem. */
  setMuted(name: string, muted: boolean): void
  /** Flip one stem's mute. */
  toggleMute(name: string): void
  /** Solo or unsolo one stem; solos are additive. */
  setSoloed(name: string, soloed: boolean): void
  /** Flip one stem's solo. */
  toggleSolo(name: string): void
  /** Set one stem's playback level (`0..1`). */
  setLevel(name: string, level: number): void
  /** The playhead in seconds, derived from the audio clock. */
  currentTime(): number
  /** The current immutable snapshot (stable between changes). */
  getSnapshot(): StemEngineSnapshot
  /** Subscribe to snapshot changes; returns an unsubscribe function. */
  subscribe(listener: () => void): () => void
  /** Stop sources, disconnect nodes, and close the context. Idempotent. */
  dispose(): void
}

/** Options for {@link createStemAudioEngine} / {@link StemAudioEngine}. */
export interface StemAudioEngineOptions {
  /**
   * Builds the `AudioContext`. Defaults to `new AudioContext()`; tests pass
   * a fake, because jsdom has no Web Audio API.
   */
  readonly createContext?: () => AudioEngineContext
  /** Fetches one stem's bytes. Defaults to the typed stem client. */
  readonly loadStemAudio?: (url: string) => Promise<ArrayBuffer>
  /**
   * How far ahead of `currentTime` playback is scheduled, in seconds. Small
   * enough to feel instant, large enough that every source is created before
   * the shared start time arrives.
   */
  readonly lookaheadSeconds?: number
}

/** Default scheduling lookahead, in seconds. */
const DEFAULT_LOOKAHEAD_SECONDS = 0.05

/** Mutable bookkeeping for one stem. */
interface StemEntry {
  readonly name: string
  readonly url: string
  status: StemLoadStatus
  error: unknown
  buffer: AudioEngineBuffer | null
  gain: AudioEngineGainNode | null
  source: AudioEngineSourceNode | null
  muted: boolean
  soloed: boolean
  audible: boolean
  level: number
}

/** Snapshot of an engine that has not loaded anything. */
const IDLE_SNAPSHOT: StemEngineSnapshot = {
  status: 'idle',
  stems: [],
  playing: false,
  durationSeconds: 0,
  error: null,
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) {
    return min
  }
  return Math.min(Math.max(value, min), max)
}

/**
 * Synchronised multi-stem playback over the Web Audio API.
 *
 * See the module docstring for the synchronisation, seeking and testability
 * rules this class exists to enforce.
 */
export class StemAudioEngine implements StemPlayerEngine {
  private readonly createContext: () => AudioEngineContext
  private readonly loadStemAudio: (url: string) => Promise<ArrayBuffer>
  private readonly lookaheadSeconds: number

  private context: AudioEngineContext | null = null
  private entries: StemEntry[] = []
  private readonly listeners = new Set<() => void>()

  private status: StemEngineStatus = 'idle'
  private error: unknown = null
  private durationSeconds = 0
  private playing = false
  private disposed = false

  /** Context time the running sources were scheduled to start at. */
  private scheduledStart = 0
  /** Buffer offset the running sources were started from. */
  private startOffset = 0
  /** The playhead while stopped, in seconds. */
  private position = 0

  private snapshot: StemEngineSnapshot = IDLE_SNAPSHOT

  constructor(options: StemAudioEngineOptions = {}) {
    this.createContext = options.createContext ?? (() => new AudioContext())
    this.loadStemAudio = options.loadStemAudio ?? fetchStemAudio
    this.lookaheadSeconds =
      options.lookaheadSeconds ?? DEFAULT_LOOKAHEAD_SECONDS
  }

  load = async (sources: readonly StemSource[]): Promise<void> => {
    if (this.disposed) {
      return
    }
    this.entries = sources.map((source) => ({
      name: source.name,
      url: source.url,
      status: 'loading',
      error: null,
      buffer: null,
      gain: null,
      source: null,
      muted: false,
      soloed: false,
      audible: true,
      level: 1,
    }))
    this.status = this.entries.length === 0 ? 'error' : 'loading'
    this.error =
      this.entries.length === 0 ? new Error('No stems to play') : null
    this.durationSeconds = 0
    this.position = 0
    this.playing = false
    this.notify()
    if (this.entries.length === 0) {
      return
    }

    let context: AudioEngineContext
    try {
      context = this.ensureContext()
    } catch (reason) {
      // A browser (or jsdom) with no Web Audio API. Nothing is playable.
      this.status = 'error'
      this.error = reason
      this.notify()
      return
    }

    // Fetch and decode concurrently…
    const buffers = await Promise.all(
      this.entries.map(async (entry) => {
        try {
          const bytes = await this.loadStemAudio(entry.url)
          return await context.decodeAudioData(bytes)
        } catch (reason) {
          entry.status = 'error'
          entry.error = reason
          return null
        }
      }),
    )
    if (this.disposed) {
      return
    }
    // …but wire the graph in stem order, so the mixer's node order is the
    // result's stem order however the network happened to interleave.
    this.entries.forEach((entry, index) => {
      const buffer = buffers[index]
      if (buffer === undefined || buffer === null) {
        return
      }
      const gain = context.createGain()
      gain.connect(context.destination)
      entry.buffer = buffer
      entry.gain = gain
      entry.status = 'loaded'
    })

    const loaded = this.entries.filter((entry) => entry.status === 'loaded')
    // One missing stem must not silence the rest, but it must still be
    // reported: the player shows the remaining stems *and* the failure.
    this.error =
      this.entries.find((entry) => entry.error !== null)?.error ?? null
    this.status = loaded.length > 0 ? 'ready' : 'error'
    this.durationSeconds = loaded.reduce(
      (longest, entry) => Math.max(longest, entry.buffer?.duration ?? 0),
      0,
    )
    this.applyGains()
    this.notify()
  }

  play = async (): Promise<void> => {
    if (this.disposed || this.playing || this.status !== 'ready') {
      return
    }
    const context = this.ensureContext()
    if (context.state === 'suspended') {
      // Autoplay policy: the context only starts from a user gesture, and
      // `play()` is always reached from one.
      try {
        await context.resume()
      } catch (reason) {
        this.error = reason
        this.notify()
        return
      }
    }
    if (this.disposed || this.playing) {
      return
    }
    const offset = this.position >= this.durationSeconds ? 0 : this.position
    this.startSources(offset)
    this.notify()
  }

  pause = (): void => {
    if (!this.playing) {
      return
    }
    const at = this.currentTime()
    this.stopSources()
    this.playing = false
    this.position = at
    this.notify()
  }

  seek = (seconds: number): void => {
    if (this.disposed) {
      return
    }
    const target = clamp(seconds, 0, this.durationSeconds)
    if (this.playing) {
      // An AudioBufferSourceNode is single-use: seeking means new sources,
      // started together at one new time with one new offset.
      this.startSources(target)
    } else {
      this.position = target
    }
    this.notify()
  }

  setMuted = (name: string, muted: boolean): void => {
    const entry = this.entry(name)
    if (entry === undefined || entry.muted === muted) {
      return
    }
    entry.muted = muted
    this.applyGains()
    this.notify()
  }

  toggleMute = (name: string): void => {
    const entry = this.entry(name)
    if (entry !== undefined) {
      this.setMuted(name, !entry.muted)
    }
  }

  setSoloed = (name: string, soloed: boolean): void => {
    const entry = this.entry(name)
    if (entry === undefined || entry.soloed === soloed) {
      return
    }
    entry.soloed = soloed
    this.applyGains()
    this.notify()
  }

  toggleSolo = (name: string): void => {
    const entry = this.entry(name)
    if (entry !== undefined) {
      this.setSoloed(name, !entry.soloed)
    }
  }

  setLevel = (name: string, level: number): void => {
    const entry = this.entry(name)
    if (entry === undefined) {
      return
    }
    entry.level = clamp(level, 0, 1)
    this.applyGains()
    this.notify()
  }

  currentTime = (): number => {
    if (!this.playing || this.context === null) {
      return clamp(this.position, 0, this.durationSeconds)
    }
    // Derived from the audio clock, never from a counter: this is the only
    // number that agrees with what is actually coming out of the speakers.
    const elapsed = Math.max(0, this.context.currentTime - this.scheduledStart)
    return Math.min(this.startOffset + elapsed, this.durationSeconds)
  }

  getSnapshot = (): StemEngineSnapshot => this.snapshot

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  dispose = (): void => {
    if (this.disposed) {
      return
    }
    this.disposed = true
    this.stopSources()
    for (const entry of this.entries) {
      entry.gain?.disconnect()
      entry.gain = null
      entry.buffer = null
    }
    this.playing = false
    this.status = 'idle'
    this.snapshot = IDLE_SNAPSHOT
    this.listeners.clear()
    const context = this.context
    this.context = null
    if (context !== null) {
      void Promise.resolve(context.close()).catch(() => {
        // Closing an already-closed context rejects; nothing to do about it.
      })
    }
  }

  private ensureContext(): AudioEngineContext {
    this.context ??= this.createContext()
    return this.context
  }

  private entry(name: string): StemEntry | undefined {
    return this.entries.find((candidate) => candidate.name === name)
  }

  /**
   * Create and schedule one source per loaded stem. The shared `when` is
   * read from the clock **once**, before the loop, which is what keeps the
   * stems sample-aligned however long node creation takes.
   */
  private startSources(offset: number): void {
    const context = this.ensureContext()
    this.stopSources()
    const when = context.currentTime + this.lookaheadSeconds
    const longest = this.longestEntry()
    for (const entry of this.entries) {
      if (entry.buffer === null || entry.gain === null) {
        continue
      }
      const source = context.createBufferSource()
      source.buffer = entry.buffer
      source.connect(entry.gain)
      if (entry === longest) {
        // Only the longest stem decides when the mix has ended; a shorter
        // one finishing early must not stop the others.
        source.onended = this.handleEnded
      }
      source.start(when, offset)
      entry.source = source
    }
    this.scheduledStart = when
    this.startOffset = offset
    this.position = offset
    this.playing = true
  }

  private stopSources(): void {
    for (const entry of this.entries) {
      const source = entry.source
      if (source === null) {
        continue
      }
      // Cleared first: `stop()` also fires `onended`, and a deliberate stop
      // is not the end of the mix.
      source.onended = null
      try {
        source.stop()
      } catch {
        // Stopping a source that never started (or already ended) throws.
      }
      source.disconnect()
      entry.source = null
    }
  }

  private longestEntry(): StemEntry | undefined {
    let longest: StemEntry | undefined
    let longestDuration = -1
    for (const entry of this.entries) {
      const duration = entry.buffer?.duration
      if (duration !== undefined && duration > longestDuration) {
        longestDuration = duration
        longest = entry
      }
    }
    return longest
  }

  private handleEnded = (): void => {
    if (this.disposed || !this.playing) {
      return
    }
    this.stopSources()
    this.playing = false
    this.position = this.durationSeconds
    this.notify()
  }

  /**
   * Resolve mute and solo into gain values. Any solo silences every
   * non-soloed stem; solos are additive; a soloed stem is heard even if it
   * is also muted, so clearing the solos restores exactly the mute state the
   * user had before.
   */
  private applyGains(): void {
    const soloing = this.entries.some((entry) => entry.soloed)
    for (const entry of this.entries) {
      entry.audible = soloing ? entry.soloed : !entry.muted
      if (entry.gain !== null) {
        entry.gain.gain.value = entry.audible ? entry.level : 0
      }
    }
  }

  private notify(): void {
    this.snapshot = {
      status: this.status,
      playing: this.playing,
      durationSeconds: this.durationSeconds,
      error: this.error,
      stems: this.entries.map((entry) => ({
        name: entry.name,
        status: entry.status,
        error: entry.error,
        muted: entry.muted,
        soloed: entry.soloed,
        audible: entry.audible,
        level: entry.level,
        durationSeconds: entry.buffer?.duration ?? 0,
      })),
    }
    for (const listener of [...this.listeners]) {
      listener()
    }
  }
}

/** Create a {@link StemAudioEngine}; the default engine factory for the UI. */
export function createStemAudioEngine(
  options: StemAudioEngineOptions = {},
): StemPlayerEngine {
  return new StemAudioEngine(options)
}
