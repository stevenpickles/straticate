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
 * **Looping (feature 053).** A loop region is applied to the *source nodes*,
 * not to a timer: `loop`, `loopStart` and `loopEnd` are set on every stem's
 * source before the one shared `start(when, offset)`, so the browser wraps
 * all of them on the same sample for as long as they run. Nothing has to be
 * rescheduled per pass, and there is no drift to accumulate. Two consequences
 * are deliberate:
 *
 * - **A region is a trap, not a fence.** The engine never seeks to enter one.
 *   Playback that starts before the region runs forward into it and then
 *   wraps; playback that starts at or after `loopEnd` never re-enters it
 *   (that is the platform's behaviour, not a rule invented here) and plays to
 *   the end of the mix. Playing a region "from the top" is `seek(start)`,
 *   which is a UI decision rather than engine magic.
 * - **A looping mix does not end.** `onended` is still attached to the
 *   longest stem, and a looping source simply never fires it — so a mix with
 *   a region under way never settles until the region is cleared or the
 *   transport is moved past it.
 *
 * `currentTime()` wraps to match, but only when the transport can actually be
 * looping: a generation started at or after `loopEnd` reports the raw
 * position, because its sources are running straight through.
 *
 * **Scrub preview (feature 052).** Dragging the playhead is audible: a session
 * is opened (`beginScrubPreview`), every pointer move sounds a short **grain**
 * of every stem at the cursor (`scrubPreview`), and the release closes the
 * session and moves the transport (`endScrubPreview`). Three rules make that
 * safe:
 *
 * - **A grain never touches the transport graph.** It is a throwaway
 *   source → envelope pair connected into the stem's existing gain node, so
 *   mute, solo and level apply to it for free and the running generation is
 *   never torn down. The session pauses the transport at `begin` and starts
 *   exactly one new generation at `end` — so the one-real-seek-per-gesture
 *   contract survives, now with sound during the drag.
 * - **Grains must not set `loop`.** A grain auditions one position and then
 *   stops; a looping grain would repeat a fragment under the pointer for as
 *   long as the user held it. The region belongs to the transport, not to a
 *   preview — so a scrub inside a loop region simply ignores the region.
 * - **The retrigger throttle is the audio clock, never a timer.** A call that
 *   arrives before `previewBusyUntil` is *dropped*, not queued: the next
 *   pointer move supersedes it anyway, and a queue would play positions the
 *   pointer has already left.
 *
 * **Testability.** The `AudioContext` is a constructor parameter defaulting
 * to `new AudioContext()`, and so is the loader that turns a stem URL into
 * bytes. jsdom implements neither `AudioContext` nor `decodeAudioData`, so
 * this injection is what makes the engine testable at all: tests pass a fake
 * context that records scheduled start times, offsets, gain values and node
 * connections, and assert on what was *scheduled* rather than on sound.
 *
 * **Failure and teardown.** `load()` and `play()` never reject — every
 * failure, including a context the browser closed underneath us, lands in the
 * snapshot — so callers may `void` them. `dispose()` aborts any stem download
 * still in flight rather than letting whole-file transfers outlive the graph,
 * and `load()` is generation-guarded so two overlapping calls cannot cross
 * their buffers.
 *
 * **Recovering one stem (feature 064).** A failed stem-audio download is not
 * permanent: `retryFailedStems()` re-fetches **only** the entries that failed,
 * through the same loader and decoder, under the same generation guard. Two
 * rules make it safe to call while the user is listening:
 *
 * - **Loaded stems are never touched.** No buffer is dropped, no gain node is
 *   rebuilt and nothing is re-decoded — a retry is additive, so recovering one
 *   stem cannot cost the others their state.
 * - **A recovered stem joins mid-flight, once.** If the transport is playing,
 *   the retry ends in exactly one `startSources(currentTime())` — the same
 *   one-generation rebuild `setLoopRegion` makes while playing, at the
 *   position the mix has actually reached (the wrapped one under a region).
 *   Anything less and the new stem would run behind the rest for good.
 */

import { fetchStemAudio } from '../api/stems'

/** A stem to load: its contract name and the URL its audio is served from. */
export interface StemSource {
  /** The stem's name, exactly as `SeparationResult.stems[].name` gives it. */
  readonly name: string
  /** URL the stem's audio bytes are fetched from. */
  readonly url: string
}

/**
 * The subset of `AudioParam` the engine uses. A real `AudioParam` satisfies
 * it — its scheduling methods return the param itself, which is assignable to
 * a `void` return.
 *
 * `value` is what mute/solo/level are written through ({@link
 * StemAudioEngine.applyGains}); the three scheduling methods are used **only**
 * by feature 052's grain envelopes, which need a ramp rather than a step so a
 * ninety-millisecond burst does not click at both ends.
 */
export interface AudioEngineParam {
  value: number
  /** Hold `value` from `startTime` onwards. */
  setValueAtTime(value: number, startTime: number): void
  /** Ramp linearly from the previous event to `value` by `endTime`. */
  linearRampToValueAtTime(value: number, endTime: number): void
  /** Drop every event scheduled at or after `cancelTime`. */
  cancelScheduledValues(cancelTime: number): void
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

/**
 * The subset of `AudioBuffer` the engine uses — and hands out through
 * {@link StemPlayerEngine.getStemBuffer}, which is why it covers the sample
 * data as well as the duration. Every member is one a real `AudioBuffer`
 * already has, so a browser-decoded buffer satisfies it with no cast.
 */
export interface AudioEngineBuffer {
  /** Length in seconds (`length / sampleRate`). */
  readonly duration: number
  /** Length in sample frames. */
  readonly length: number
  /** How many channels {@link AudioEngineBuffer.getChannelData} accepts. */
  readonly numberOfChannels: number
  /** Sample frames per second. */
  readonly sampleRate: number
  /** The PCM samples of one channel, in `-1..1`. */
  getChannelData(channel: number): Float32Array
}

/**
 * The subset of `AudioBufferSourceNode` the engine uses.
 *
 * The three `loop*` members are how feature 053 loops a region: they are set
 * on the node **before** `start()`, so the wrap is the browser's own — one
 * sample-accurate boundary applied to every stem's source at once, rather
 * than a timer that would have to reschedule the whole mix on every pass.
 */
export interface AudioEngineSourceNode extends AudioEngineNode {
  buffer: AudioEngineBuffer | null
  onended: ((event: Event) => void) | null
  /** Whether playback wraps from `loopEnd` back to `loopStart`. */
  loop: boolean
  /** Where a wrap lands, in seconds into the buffer. */
  loopStart: number
  /** Where playback wraps, in seconds into the buffer. */
  loopEnd: number
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

/**
 * A loop region: the half-open interval `[start, end)` of the mix playback
 * wraps within. Always legal — clamped to the mix and never degenerate — so a
 * renderer can use it without re-checking.
 */
export interface LoopRegion {
  /** Where a wrap lands, in seconds. */
  readonly start: number
  /** Where playback wraps, in seconds. Always `> start`. */
  readonly end: number
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
  /**
   * The loop region playback wraps within, or `null` when there is none.
   * This is the single source of truth for the committed region: a UI drawing
   * a live drag holds that in its own state and renders this once it commits.
   */
  readonly loopRegion: LoopRegion | null
  /**
   * Whether a scrub-preview session is open — the transport is silenced and
   * grains are being auditioned under the pointer (feature 052).
   */
  readonly scrubbing: boolean
  /**
   * The failure worth showing, or `null`. A transport failure (a rejected
   * `resume()`) shadows a load failure while it stands and is cleared by the
   * next `play()`; a load failure survives until the next `load()`.
   */
  readonly error: unknown
}

/**
 * The playback surface a UI needs. `StemAudioEngine` implements it; a test
 * can substitute any other object with the same shape.
 */
export interface StemPlayerEngine {
  /**
   * Fetch and decode every stem. **Never rejects** — every failure, including
   * a context the browser closed underneath us, lands in the snapshot — so a
   * caller may `void` the result.
   */
  load(sources: readonly StemSource[]): Promise<void>
  /**
   * Fetch and decode the stems whose audio **failed**, leaving every stem
   * that already loaded exactly as it is — same buffers, same gain nodes,
   * same running sources. A no-op when nothing failed (no request is made)
   * and after {@link StemPlayerEngine.dispose}.
   *
   * **Never rejects**, like {@link StemPlayerEngine.load}: a stem that fails
   * again lands back on its own entry and in the snapshot's `error`. It is
   * guarded by the same generation counter, so a `load()` or a `dispose()`
   * that arrives mid-retry orphans it silently.
   *
   * A recovered stem joins a mix that is already playing: the transport
   * rebuilds once, at the position it has actually reached (the wrapped one
   * under a loop region), so every stem stays on the one shared clock.
   */
  retryFailedStems(): Promise<void>
  /**
   * Resume the context if suspended, then start every stem together.
   * **Never rejects**, for the same reason {@link StemPlayerEngine.load}
   * does not.
   */
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
  /**
   * Loop `[startSeconds, endSeconds)`, clamped to the mix. A degenerate
   * region — shorter than {@link MIN_LOOP_SECONDS} once clamped, which
   * includes an inverted one — clears instead of setting.
   *
   * **The region is a trap, not a fence** (see the module docstring): the
   * engine never moves the playhead to enter it. Playback started before the
   * region runs into it and then wraps; playback started at or after
   * `endSeconds` never enters it at all and plays to the end of the mix.
   * "Play the region from the top" is `seek(startSeconds)` — a UI decision.
   */
  setLoopRegion(startSeconds: number, endSeconds: number): void
  /** Drop the loop region; playback carries on from where it is. */
  clearLoopRegion(): void
  /**
   * Begin a scrub session: silence the transport — remembering whether it was
   * playing, because that decision cannot be made once it has stopped — and
   * accept previews. Idempotent while a session is open, and a no-op unless
   * the engine is `ready`.
   */
  beginScrubPreview(): void
  /**
   * Sound a short grain of every stem at `seconds`, respecting mute, solo and
   * level (the grains run through the same per-stem gain nodes the transport
   * does). Throttled against the **audio clock**: a call that arrives inside
   * the previous grain's retrigger window is dropped, not queued — the next
   * pointer move supersedes it. A no-op with no session open.
   */
  scrubPreview(seconds: number): void
  /**
   * End the session: fade the grains out, move the playhead to
   * `commitSeconds` when one is given, and resume the transport if it was
   * playing when the session began.
   *
   * **`endScrubPreview(commit)` *is* the gesture's seek** — the one real
   * transport move a pointer drag is allowed. A UI that also called
   * {@link StemPlayerEngine.seek} would rebuild every source node twice. With
   * no session open it does nothing at all.
   */
  endScrubPreview(commitSeconds?: number): void
  /** The playhead in seconds, derived from the audio clock. */
  currentTime(): number
  /**
   * The decoded buffer of one stem, or `null` when there is none — an
   * unknown name, a stem still loading or failed, or an engine that has been
   * disposed. The samples are what a waveform view reads; the engine itself
   * neither analyses them nor puts anything derived from them in the
   * snapshot, so a repaint costs nothing here.
   *
   * The buffer belongs to the engine and stops being valid once the next
   * `load()` or `dispose()` drops it: read it, do not retain it.
   */
  getStemBuffer(name: string): AudioEngineBuffer | null
  /** The current immutable snapshot (stable between changes). */
  getSnapshot(): StemEngineSnapshot
  /** Subscribe to snapshot changes; returns an unsubscribe function. */
  subscribe(listener: () => void): () => void
  /**
   * Stop sources, disconnect nodes, abort any in-flight stem download, and
   * close the context. Idempotent.
   */
  dispose(): void
}

/** Options for {@link createStemAudioEngine} / {@link StemAudioEngine}. */
export interface StemAudioEngineOptions {
  /**
   * Builds the `AudioContext`. Defaults to `new AudioContext()`; tests pass
   * a fake, because jsdom has no Web Audio API.
   */
  readonly createContext?: () => AudioEngineContext
  /**
   * Fetches one stem's bytes. Defaults to the typed stem client. The
   * `signal` is aborted by {@link StemAudioEngine.dispose} and by a
   * superseding {@link StemAudioEngine.load}, so teardown does not leave
   * whole-stem downloads running.
   */
  readonly loadStemAudio?: (
    url: string,
    signal?: AbortSignal,
  ) => Promise<ArrayBuffer>
  /**
   * How far ahead of `currentTime` playback is scheduled, in seconds. Small
   * enough to feel instant, large enough that every source is created before
   * the shared start time arrives.
   */
  readonly lookaheadSeconds?: number
  /**
   * How long one scrub-preview grain sounds, in seconds. Long enough to carry
   * pitch, short enough that the pointer has not moved far by the time it
   * ends.
   */
  readonly scrubGrainSeconds?: number
  /**
   * The shortest gap between two grains, in seconds — the audio-clock
   * throttle. Slightly under the grain length, so consecutive grains overlap
   * rather than leaving a gap.
   */
  readonly scrubRetriggerSeconds?: number
  /**
   * The grain envelope's attack and release, in seconds. Without it a grain
   * starts and stops on a discontinuity, which is an audible click.
   */
  readonly scrubFadeSeconds?: number
}

/**
 * Default scheduling lookahead, in seconds.
 *
 * Exported alongside {@link DEFAULT_SCRUB_FADE_SECONDS} because the pair has
 * an invariant a test pins: **the fade must be shorter than the lookahead.**
 * A motionless click opens a session, sounds one grain and closes it in the
 * same tick, so the release's `stop(now + fade)` has to land *before* the
 * grain's `start(now + lookahead)` for the click to stay silent. Change
 * either number without the other and plain clicks click again.
 */
export const DEFAULT_LOOKAHEAD_SECONDS = 0.05

/** Default length of one scrub-preview grain, in seconds. */
const DEFAULT_SCRUB_GRAIN_SECONDS = 0.09

/** Default gap between two grains, in seconds: a ~30 ms overlap. */
const DEFAULT_SCRUB_RETRIGGER_SECONDS = 0.06

/**
 * Default grain attack/release, in seconds. Must stay below
 * {@link DEFAULT_LOOKAHEAD_SECONDS} — see the note there.
 */
export const DEFAULT_SCRUB_FADE_SECONDS = 0.008

/**
 * The shortest loop region worth having, in seconds. Anything below it is
 * treated as an instruction to clear rather than as a region: a ten-millisecond
 * loop is a buzz, not a passage, and it is what an accidental click on a
 * loop-setting control produces.
 */
export const MIN_LOOP_SECONDS = 0.01

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

/** One scrub-preview grain in flight: its source and its own envelope. */
interface PreviewGrain {
  readonly source: AudioEngineSourceNode
  readonly env: AudioEngineGainNode
  /** When the grain's own schedule ends it — past this, `onended` has fired. */
  readonly endsAt: number
}

/** Snapshot of an engine that has not loaded anything. */
const IDLE_SNAPSHOT: StemEngineSnapshot = {
  status: 'idle',
  stems: [],
  playing: false,
  durationSeconds: 0,
  loopRegion: null,
  scrubbing: false,
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
  private readonly loadStemAudio: (
    url: string,
    signal?: AbortSignal,
  ) => Promise<ArrayBuffer>
  private readonly lookaheadSeconds: number
  private readonly scrubGrainSeconds: number
  private readonly scrubRetriggerSeconds: number
  private readonly scrubFadeSeconds: number

  private context: AudioEngineContext | null = null
  private entries: StemEntry[] = []
  private readonly listeners = new Set<() => void>()

  private status: StemEngineStatus = 'idle'
  /** The failure that made stems unplayable; survives transport commands. */
  private loadError: unknown = null
  /**
   * The failure of the *last* transport command (a rejected `resume()`, a
   * context the browser closed). Cleared by the next `play()`, because a
   * later attempt that works is the answer to an earlier one that did not.
   */
  private transportError: unknown = null
  private durationSeconds = 0
  private playing = false
  private disposed = false

  /** Bumped by every `load()`, so a superseded one can never publish. */
  private loadGeneration = 0
  /** Aborts the in-flight stem downloads of the current `load()`. */
  private loadAbort: AbortController | null = null

  /** Context time the running sources were scheduled to start at. */
  private scheduledStart = 0
  /** Buffer offset the running sources were started from. */
  private startOffset = 0
  /** The playhead while stopped, in seconds. */
  private position = 0
  /** The region playback wraps within, or `null`. Always legal when set. */
  private loopRegion: LoopRegion | null = null

  /** Whether a scrub-preview session is open (feature 052). */
  private scrubbing = false
  /**
   * Whether the transport was playing when the open session began. Captured
   * at `begin` because by `end` the transport is always paused — which is the
   * whole reason the session has three calls rather than two.
   */
  private scrubWasPlaying = false
  /**
   * Context time the next grain may be scheduled at. Compared against the
   * audio clock, never against a timer: a preview that arrives before it is
   * dropped, and the next pointer move supersedes the position it carried.
   */
  private previewBusyUntil = 0
  /** Grains currently scheduled, so a session end can silence them all. */
  private previewGrains: PreviewGrain[] = []

  private snapshot: StemEngineSnapshot = IDLE_SNAPSHOT

  constructor(options: StemAudioEngineOptions = {}) {
    this.createContext = options.createContext ?? (() => new AudioContext())
    this.loadStemAudio = options.loadStemAudio ?? fetchStemAudio
    this.lookaheadSeconds =
      options.lookaheadSeconds ?? DEFAULT_LOOKAHEAD_SECONDS
    this.scrubGrainSeconds =
      options.scrubGrainSeconds ?? DEFAULT_SCRUB_GRAIN_SECONDS
    this.scrubRetriggerSeconds =
      options.scrubRetriggerSeconds ?? DEFAULT_SCRUB_RETRIGGER_SECONDS
    this.scrubFadeSeconds =
      options.scrubFadeSeconds ?? DEFAULT_SCRUB_FADE_SECONDS
  }

  load = async (sources: readonly StemSource[]): Promise<void> => {
    if (this.disposed) {
      return
    }
    // `load` is public, so two calls can overlap. Everything below belongs to
    // *this* generation: the entries are held in a local and the results are
    // published only while no later load has started. Without that, the first
    // call's buffers land on the second call's stem names.
    const generation = ++this.loadGeneration
    this.teardownGraph()
    this.loadAbort?.abort()
    const abort = new AbortController()
    this.loadAbort = abort

    const entries: StemEntry[] = sources.map((source) => ({
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
    this.entries = entries
    this.status = entries.length === 0 ? 'error' : 'loading'
    this.loadError = entries.length === 0 ? new Error('No stems to play') : null
    this.transportError = null
    this.durationSeconds = 0
    this.position = 0
    // A new job is a new timeline: a region measured against the last one
    // would name seconds that no longer mean anything.
    this.loopRegion = null
    this.playing = false
    this.notify()
    if (entries.length === 0) {
      return
    }

    let context: AudioEngineContext
    try {
      context = this.ensureContext()
    } catch (reason) {
      // A browser (or jsdom) with no Web Audio API. Nothing is playable.
      this.failLoad(generation, reason)
      return
    }

    // Fetch and decode concurrently…
    const buffers = await Promise.all(
      entries.map(async (entry) => {
        try {
          const bytes = await this.loadStemAudio(entry.url, abort.signal)
          return await context.decodeAudioData(bytes)
        } catch (reason) {
          entry.status = 'error'
          entry.error = reason
          return null
        }
      }),
    )
    if (this.disposed || generation !== this.loadGeneration) {
      return
    }

    try {
      // …but wire the graph in stem order, so the mixer's node order is the
      // result's stem order however the network happened to interleave.
      entries.forEach((entry, index) => {
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
    } catch (reason) {
      // The browser can close a context underneath us (tab discard, memory
      // pressure) and every node call then throws. The contract says `load`
      // never rejects, so this is an error state, not an unhandled rejection.
      this.failLoad(generation, reason)
      return
    }

    this.publishLoadOutcome()
  }

  retryFailedStems = async (): Promise<void> => {
    if (this.disposed) {
      return
    }
    // Only the failures are re-fetched. A loaded stem's buffer, gain node and
    // running source are never touched — recovering one stem must not
    // interrupt the ones the user is already listening to.
    const failed = this.entries.filter((entry) => entry.status === 'error')
    if (failed.length === 0) {
      // Nothing to recover: not even a request, let alone a graph rebuild. A
      // transport failure (a refused `resume()`) is not this call's business;
      // `play()` is its remedy.
      return
    }

    // The same generation guard `load()` uses, for the same reason: a
    // `load()` or a `dispose()` arriving mid-retry must orphan this call
    // rather than let it publish onto entries that no longer exist.
    const generation = ++this.loadGeneration
    this.loadAbort?.abort()
    const abort = new AbortController()
    this.loadAbort = abort

    for (const entry of failed) {
      entry.status = 'loading'
      // Cleared now, so a stem that recovers leaves no stale rejection behind
      // for `publishLoadOutcome` to re-report as the engine's `loadError`.
      entry.error = null
    }
    // Deliberately *not* touching `status` or `loadError`: the mix is still
    // playable and still explaining the failure until this attempt answers it.
    this.notify()

    let context: AudioEngineContext
    try {
      context = this.ensureContext()
    } catch (reason) {
      this.failLoad(generation, reason)
      return
    }

    const buffers = await Promise.all(
      failed.map(async (entry) => {
        try {
          const bytes = await this.loadStemAudio(entry.url, abort.signal)
          return await context.decodeAudioData(bytes)
        } catch (reason) {
          entry.status = 'error'
          entry.error = reason
          return null
        }
      }),
    )
    if (this.disposed || generation !== this.loadGeneration) {
      return
    }

    const recovered = new Map<StemEntry, AudioEngineBuffer>()
    failed.forEach((entry, index) => {
      const buffer = buffers[index]
      if (buffer !== undefined && buffer !== null) {
        recovered.set(entry, buffer)
      }
    })
    try {
      // Walked in stem order, exactly as `load()` does, so the mixer's node
      // order stays the result's stem order however the network interleaved.
      for (const entry of this.entries) {
        const buffer = recovered.get(entry)
        if (buffer === undefined) {
          continue
        }
        const gain = context.createGain()
        gain.connect(context.destination)
        entry.buffer = buffer
        entry.gain = gain
        entry.status = 'loaded'
      }
    } catch (reason) {
      this.failLoad(generation, reason)
      return
    }

    this.publishLoadOutcome()

    if (this.playing) {
      // Read *after* publishing, because a recovered stem can have lengthened
      // the mix and `currentTime()` clamps to the duration. Under a loop
      // region this is the wrapped position — where the audio actually is.
      const at = this.currentTime()
      try {
        // One rebuilt generation, the same one-move pattern `setLoopRegion`
        // uses while playing: the recovered stem joins mid-flight at the
        // offset every other stem has already reached.
        this.startSources(at)
      } catch (reason) {
        this.transportError = reason
        this.playing = false
        this.position = at
      }
      this.notify()
    }
  }

  play = async (): Promise<void> => {
    if (this.disposed) {
      return
    }
    // A scrub session is a silenced transport, and this is the transport
    // command that answers it: the grains go, and the start below is the
    // resume — so the session must not schedule one of its own.
    this.endScrubSession()
    if (this.playing || this.status !== 'ready') {
      return
    }
    // This attempt answers the last one: an autoplay-policy rejection the
    // user has since satisfied with a click must not stay on screen.
    this.transportError = null
    try {
      const context = this.ensureContext()
      if (context.state === 'suspended') {
        // Autoplay policy: the context only starts from a user gesture, and
        // `play()` is always reached from one.
        await context.resume()
      }
      if (!this.disposed && !this.playing) {
        const offset = this.position >= this.durationSeconds ? 0 : this.position
        this.startSources(offset)
      }
    } catch (reason) {
      this.transportError = reason
      this.playing = false
    }
    this.notify()
  }

  pause = (): void => {
    // Pausing a scrub session is just closing it: the transport is already
    // stopped, and it must not resume on the way out.
    const closed = this.endScrubSession().open
    if (!this.playing) {
      if (closed) {
        this.notify()
      }
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
    // A discrete seek (an arrow key, Home/End) supersedes any drag still in
    // flight. The session is closed *without* resuming, so this seek is the
    // one generation the move costs rather than the second of two.
    const resume = this.endScrubSession().wasPlaying
    const target = clamp(seconds, 0, this.durationSeconds)
    if (this.playing || resume) {
      try {
        // An AudioBufferSourceNode is single-use: seeking means new sources,
        // started together at one new time with one new offset.
        this.startSources(target)
      } catch (reason) {
        this.transportError = reason
        this.playing = false
        this.position = target
      }
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

  setLoopRegion = (startSeconds: number, endSeconds: number): void => {
    if (this.disposed) {
      return
    }
    const start = clamp(startSeconds, 0, this.durationSeconds)
    const end = clamp(endSeconds, 0, this.durationSeconds)
    if (end - start < MIN_LOOP_SECONDS) {
      // Degenerate, inverted, or clamped down to nothing by a mix that ends
      // before the region did: all of them mean "no region".
      this.clearLoopRegion()
      return
    }
    const current = this.loopRegion
    if (current !== null && current.start === start && current.end === end) {
      // Setting the region it already has must not rebuild the graph — a
      // handle dragged back where it started would be an audible gap.
      return
    }
    this.applyLoopRegion({ start, end })
  }

  clearLoopRegion = (): void => {
    if (this.disposed || this.loopRegion === null) {
      return
    }
    this.applyLoopRegion(null)
  }

  beginScrubPreview = (): void => {
    if (this.disposed || this.status !== 'ready' || this.scrubbing) {
      return
    }
    // Audacity pauses while you scrub: what you hear under the pointer is the
    // preview, not the mix running on underneath it.
    this.scrubWasPlaying = this.playing
    if (this.playing) {
      // Read before stopping, and read through `currentTime()` — under a loop
      // region that value is the *wrapped* one, which is where the audio
      // actually is and therefore where the playhead must stay.
      const at = this.currentTime()
      this.stopSources()
      this.playing = false
      this.position = at
    }
    this.scrubbing = true
    // A fresh session owes nothing to the last one's retrigger window.
    this.previewBusyUntil = 0
    this.notify()
  }

  scrubPreview = (seconds: number): void => {
    const context = this.context
    if (this.disposed || !this.scrubbing || context === null) {
      return
    }
    if (context.currentTime < this.previewBusyUntil) {
      // Inside the last grain's retrigger window. Dropped rather than queued:
      // by the time a queued position played, the pointer would be elsewhere.
      return
    }
    // One clock reading for the whole set, exactly as `startSources` does —
    // it is what keeps the stems of a grain aligned with each other.
    const when = context.currentTime + this.lookaheadSeconds
    const grain = this.scrubGrainSeconds
    // A fade can never eat more than half the grain, however it is configured.
    const fade = Math.min(this.scrubFadeSeconds, grain / 2)
    const offset = clamp(seconds, 0, this.durationSeconds)
    this.previewBusyUntil = when + this.scrubRetriggerSeconds
    try {
      for (const entry of this.entries) {
        if (entry.buffer === null || entry.gain === null) {
          continue
        }
        // Its own envelope, so the grain fades in and out without touching
        // the stem's gain — which is carrying mute, solo and level, and which
        // the transport is using too.
        const env = context.createGain()
        env.gain.setValueAtTime(0, when)
        env.gain.linearRampToValueAtTime(1, when + fade)
        env.gain.setValueAtTime(1, when + grain - fade)
        env.gain.linearRampToValueAtTime(0, when + grain)
        env.connect(entry.gain)
        const source = context.createBufferSource()
        source.buffer = entry.buffer
        // Never `loop`: a grain auditions one position and stops. See the
        // module docstring and feature 053's note.
        source.connect(env)
        // A stem shorter than `offset` simply produces silence; there is no
        // special case, and no assumption about how many stems there are.
        source.start(when, offset)
        source.stop(when + grain)
        this.previewGrains.push({ source, env, endsAt: when + grain })
      }
    } catch (reason) {
      // The browser can close a context underneath us. A preview is not worth
      // throwing out of a pointer handler for; it lands in the snapshot like
      // every other transport failure.
      this.transportError = reason
      this.notify()
    }
  }

  endScrubPreview = (commitSeconds?: number): void => {
    if (this.disposed || !this.scrubbing) {
      return
    }
    const { wasPlaying } = this.endScrubSession()
    if (commitSeconds !== undefined) {
      this.position = clamp(commitSeconds, 0, this.durationSeconds)
    }
    if (wasPlaying) {
      try {
        // The gesture's one real transport move — the same one-generation
        // rebuild `seek` makes, under the same failure contract. A loop
        // region reapplies here on its own, because `startSources` is where
        // the flags are set.
        this.startSources(this.position)
      } catch (reason) {
        this.transportError = reason
        this.playing = false
      }
    }
    this.notify()
  }

  currentTime = (): number => {
    if (!this.playing || this.context === null) {
      return clamp(this.position, 0, this.durationSeconds)
    }
    // Derived from the audio clock, never from a counter: this is the only
    // number that agrees with what is actually coming out of the speakers.
    const elapsed = Math.max(0, this.context.currentTime - this.scheduledStart)
    const raw = this.startOffset + elapsed
    const region = this.loopRegion
    // Only a generation that started *before* the region's end can be
    // looping: one started at or after it ran straight past and its sources
    // never re-enter, so its position is the raw one (see the module
    // docstring's "trap, not a fence").
    if (region !== null && this.startOffset < region.end && raw >= region.end) {
      return region.start + ((raw - region.end) % (region.end - region.start))
    }
    return Math.min(raw, this.durationSeconds)
  }

  getStemBuffer = (name: string): AudioEngineBuffer | null => {
    if (this.disposed) {
      return null
    }
    // `teardownGraph` clears every entry's buffer, so a disposed or reloaded
    // engine answers `null` here without any extra bookkeeping.
    return this.entry(name)?.buffer ?? null
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
    // Downloads outlive the graph unless they are cancelled: a four-stem job
    // is four whole-file transfers, and React's double-invoked mount effect
    // starts the whole set twice in development.
    this.loadAbort?.abort()
    this.loadAbort = null
    this.teardownGraph()
    this.playing = false
    this.status = 'idle'
    this.loadError = null
    this.transportError = null
    this.loopRegion = null
    this.snapshot = IDLE_SNAPSHOT
    this.listeners.clear()
    const context = this.context
    this.context = null
    if (context !== null) {
      try {
        void Promise.resolve(context.close()).catch(() => {
          // Closing an already-closed context rejects; nothing to do.
        })
      } catch {
        // …and some browsers throw synchronously instead.
      }
    }
  }

  /**
   * Store a region and make the running sources agree with it.
   *
   * An `AudioBufferSourceNode`'s `loop` flags are only honoured from `start()`
   * onwards, so changing the region while playing means a new generation —
   * through the same `startSources` the seek path uses, at the position the
   * transport has actually reached, with the same try/catch. While paused
   * there is nothing to rebuild: the next `play()` reads the region.
   */
  private applyLoopRegion(region: LoopRegion | null): void {
    // Read under the *outgoing* region: that is where the audio is now.
    const at = this.currentTime()
    this.loopRegion = region
    if (this.playing) {
      try {
        this.startSources(at)
      } catch (reason) {
        this.transportError = reason
        this.playing = false
        this.position = at
      }
    }
    this.notify()
  }

  /**
   * Publish what the entries now say: the overall status, the transport's
   * extent, the failure worth showing, and the resolved gains.
   *
   * Shared by `load()` and `retryFailedStems()` so the two can never drift
   * apart — a retry that recomputed the duration differently from a load
   * would give the same set of decoded stems two different timelines.
   * Callers reach here only once their generation is still current.
   */
  private publishLoadOutcome(): void {
    const loaded = this.entries.filter((entry) => entry.status === 'loaded')
    // One missing stem must not silence the rest, but it must still be
    // reported: the player shows the remaining stems *and* the failure.
    this.loadError =
      this.entries.find((entry) => entry.error !== null)?.error ?? null
    this.status = loaded.length > 0 ? 'ready' : 'error'
    this.durationSeconds = loaded.reduce(
      (longest, entry) => Math.max(longest, entry.buffer?.duration ?? 0),
      0,
    )
    this.applyGains()
    this.notify()
  }

  /** Record a failure that made a whole load unusable. */
  private failLoad(generation: number, reason: unknown): void {
    if (this.disposed || generation !== this.loadGeneration) {
      return
    }
    this.status = 'error'
    this.loadError = reason
    this.notify()
  }

  /**
   * Close any open scrub session without resuming the transport, and say what
   * was closed: whether there was a session at all, and whether the transport
   * was playing when it began.
   *
   * Every caller decides the resume for itself — `endScrubPreview` schedules
   * one, `seek` folds it into its own generation, `play` starts anyway and
   * `pause` wants none — which is why this helper never starts sources.
   */
  private endScrubSession(): { open: boolean; wasPlaying: boolean } {
    if (!this.scrubbing) {
      return { open: false, wasPlaying: false }
    }
    this.stopPreviewGrains(this.scrubFadeSeconds)
    const wasPlaying = this.scrubWasPlaying
    this.scrubbing = false
    this.scrubWasPlaying = false
    this.previewBusyUntil = 0
    return { open: true, wasPlaying }
  }

  /**
   * Silence, stop and disconnect every grain in flight. `fadeSeconds` ramps
   * the envelopes down first; a teardown passes `0`, having no time to be
   * polite about it.
   */
  private stopPreviewGrains(fadeSeconds: number): void {
    const grains = this.previewGrains
    this.previewGrains = []
    const context = this.context
    const now = context?.currentTime ?? 0
    for (const grain of grains) {
      const disconnect = (): void => {
        try {
          grain.source.disconnect()
          grain.env.disconnect()
        } catch {
          // A closed context refuses; the nodes are unreachable either way.
        }
      }
      try {
        if (context !== null && fadeSeconds > 0 && now < grain.endsAt) {
          grain.env.gain.cancelScheduledValues(now)
          grain.env.gain.linearRampToValueAtTime(0, now + fadeSeconds)
          grain.source.stop(now + fadeSeconds)
          // Disconnecting here and now would sever the graph at the next
          // render quantum — milliseconds before the ramp finishes — turning
          // the fade into dead code and the release back into the click it
          // exists to prevent. The node reports when it is actually done.
          // (A grain past `endsAt` has already fired `onended` and would
          // never report again — it is silent, so it disconnects below.)
          grain.source.onended = disconnect
          continue
        }
        grain.source.stop()
      } catch {
        // A grain that has already ended refuses a second stop, and so does
        // any node whose context the browser closed.
      }
      disconnect()
    }
  }

  /** Stop every source and drop every gain node the last load built. */
  private teardownGraph(): void {
    this.stopSources()
    // Grains outlive nothing: a new job or a disposal takes them with it.
    this.stopPreviewGrains(0)
    this.scrubbing = false
    this.scrubWasPlaying = false
    this.previewBusyUntil = 0
    for (const entry of this.entries) {
      try {
        entry.gain?.disconnect()
      } catch {
        // A closed context refuses; the node is unreachable either way.
      }
      entry.gain = null
      entry.buffer = null
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
    const region = this.loopRegion
    for (const entry of this.entries) {
      if (entry.buffer === null || entry.gain === null) {
        continue
      }
      const source = context.createBufferSource()
      source.buffer = entry.buffer
      if (region !== null && entry.buffer.duration > region.start) {
        // Set before `start()`, which is the only time the platform reads
        // them. A stem that ends before the region begins is left running
        // straight through — there is nothing of it in the loop to repeat.
        source.loop = true
        source.loopStart = region.start
        // Belt and braces: a stem shorter than the region would otherwise be
        // told to wrap past its own end. Exact sync across the mix assumes
        // the stems are the same length, which is what a separation produces;
        // a genuinely short stem loops on its own boundary instead.
        source.loopEnd = Math.min(region.end, entry.buffer.duration)
      }
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
        source.disconnect()
      } catch {
        // Stopping a source that never started (or already ended) throws,
        // and so does touching a node whose context the browser closed.
      }
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
      loopRegion: this.loopRegion,
      scrubbing: this.scrubbing,
      // A transient transport failure shadows a load failure while it stands
      // and clears on the next transport command; a load failure has no such
      // remedy and survives until the next load.
      error: this.transportError ?? this.loadError,
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
