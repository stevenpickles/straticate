import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import { ApiError } from '../api/client'
import { getSeparationResult, stemUrl } from '../api/stems'
import type { SeparationResult } from '../api/types'
import {
  createStemAudioEngine,
  type StemEngineSnapshot,
  type StemPlayerEngine,
} from '../audio/engine'
import { formatDuration } from '../format'
import { useAppDispatch } from '../state/appState'
import { useJobDispatch, useJobState } from '../state/jobState'
import { StemTimeline, type StemTimelineStem } from './StemTimeline'
import './StemPlayer.css'

/** Envelope-shaped view of any rejection. */
interface ErrorInfo {
  readonly code: string
  readonly message: string
  readonly detail: unknown
}

/** Fallback for a rejection that is not an {@link ApiError}. */
const UNKNOWN_ERROR: ErrorInfo = {
  code: 'unknown_error',
  message: 'Something went wrong. Please try again.',
  detail: undefined,
}

/** State of the `GET /jobs/{id}/result` fetch. */
type ResultState =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | { readonly status: 'loaded'; readonly result: SeparationResult }
  | { readonly status: 'error'; readonly error: ErrorInfo }

/** Snapshot of an engine that does not exist yet. */
const EMPTY_SNAPSHOT: StemEngineSnapshot = {
  status: 'idle',
  stems: [],
  playing: false,
  durationSeconds: 0,
  loopRegion: null,
  scrubbing: false,
  error: null,
}

const subscribeToNothing = () => () => {
  // No engine, so nothing ever changes.
}

const emptySnapshot = () => EMPTY_SNAPSHOT

/** Envelope-shaped `{code, message, detail}` for any rejection reason. */
function errorInfo(reason: unknown): ErrorInfo {
  return reason instanceof ApiError
    ? { code: reason.code, message: reason.message, detail: reason.detail }
    : UNKNOWN_ERROR
}

/**
 * The job state a `result_not_available` envelope carries in its `detail`,
 * or `null` when the backend sent none.
 */
function detailState(detail: unknown): string | null {
  if (typeof detail !== 'object' || detail === null || !('state' in detail)) {
    return null
  }
  const state: unknown = (detail as { state: unknown }).state
  return typeof state === 'string' ? state : null
}

/** Humanize a snake_case contract identifier for use mid-sentence. */
function humanizeInline(identifier: string): string {
  return identifier.replaceAll('_', ' ').trim()
}

/**
 * Turn a backend error into something worth reading, using the codes feature
 * 021 documents for this route. `result_not_available` is one code for three
 * situations — still running, cancelled, failed — distinguished by the
 * `state` the envelope's `detail` carries; `stem_file_missing` means the job
 * record outlived its files, so the honest remedy is to separate again.
 */
function explainError(error: ErrorInfo): string {
  switch (error.code) {
    case 'result_not_available': {
      const state = detailState(error.detail)
      if (state === 'cancelled') {
        return 'This separation was cancelled, so there are no stems to play.'
      }
      if (state === 'failed') {
        return 'This separation failed, so there are no stems to play.'
      }
      return state === null
        ? 'The stems are not ready yet.'
        : `The stems are not ready yet — this job is ${humanizeInline(state)}.`
    }
    case 'stem_file_missing':
      return 'The audio for this job is gone from disk. Run the separation again to recreate the stems.'
    case 'job_not_found':
      return 'The backend no longer knows about this job. Run the separation again.'
    default:
      return error.message
  }
}

/** Props for {@link StemPlayer}. */
export interface StemPlayerProps {
  /**
   * Builds the playback engine. Defaults to the real Web Audio engine;
   * tests inject a fake. Must be stable across renders — a new function
   * identity tears the engine down and rebuilds it.
   */
  createEngine?: () => StemPlayerEngine
}

/**
 * The `inspect` step of the workflow: listen to a completed job's separated
 * stems.
 *
 * Every stem lane comes from `SeparationResult.stems` — a two-stem and a
 * four-stem job render through the same code, and no stem name or count
 * appears anywhere here (AGENTS.md principle 6). The audio itself is handled
 * by `src/audio/engine.ts`, which keeps the stems on one clock; this
 * component only renders its snapshot and forwards the user's intent.
 *
 * The transport is `StemTimeline` (feature 050): waveform lanes on a shared
 * time axis, a playhead, and a timeline that *is* the accessible seek control.
 * What stays here is the state the timeline reports into — the playhead
 * position, and the drag position that overrides it mid-gesture — plus the
 * Play/Pause button, the readout, and (feature 053) the loop controls: "Loop
 * start", "Loop end" and "Clear loop", with a badge that names the region.
 * Those buttons exist because the gesture that draws a region is a pointer
 * drag over an `aria-hidden` picture; they are the keyboard and screen-reader
 * path to the same intent, and the badge is the live region that reports it.
 *
 * Playback is an **inspection tool** (ARCHITECTURE.md §13): transport, solo
 * and mute, and nothing that edits audio.
 *
 * It also carries the route out of the `inspect` phase ("Start another
 * separation"), because the progress panel that offers the same control is
 * mounted only for `separate` — see the handler for why that matters.
 *
 * Must be rendered under an `AppStateProvider` and a `JobStateProvider`.
 */
export function StemPlayer({
  createEngine = createStemAudioEngine,
}: StemPlayerProps = {}) {
  const { job } = useJobState()
  const jobDispatch = useJobDispatch()
  const appDispatch = useAppDispatch()
  const jobId = job?.id ?? null

  const [result, setResult] = useState<ResultState>({ status: 'idle' })
  const [engine, setEngine] = useState<StemPlayerEngine | null>(null)
  const [currentTime, setCurrentTime] = useState(0)

  // Bumped by the "Try again" control in the error state, which is the only
  // thing that ever changes it. Widening the effect's dependencies on it is
  // what turns that click into a genuine refetch of the same job.
  const [attempt, setAttempt] = useState(0)

  // The result is fetched rather than read off `job.result`: the REST route
  // is the contract's source of truth, and it is the only thing that can
  // tell us *why* there is nothing to play (021's 409 `detail.state`).
  useEffect(() => {
    if (jobId === null) {
      setResult({ status: 'idle' })
      return
    }
    let current = true
    setResult({ status: 'loading' })
    getSeparationResult(jobId)
      .then((fetched) => {
        if (current) {
          setResult({ status: 'loaded', result: fetched })
        }
      })
      .catch((reason: unknown) => {
        if (current) {
          setResult({ status: 'error', error: errorInfo(reason) })
        }
      })
    return () => {
      current = false
    }
  }, [jobId, attempt])

  // One engine per loaded result, disposed when the result or the job
  // changes and on unmount: sources stopped, nodes disconnected, context
  // closed.
  useEffect(() => {
    if (jobId === null || result.status !== 'loaded') {
      return
    }
    const instance = createEngine()
    setEngine(instance)
    setCurrentTime(0)
    void instance.load(
      result.result.stems.map((stem) => ({
        name: stem.name,
        url: stemUrl(jobId, stem.name),
      })),
    )
    return () => {
      setEngine(null)
      instance.dispose()
    }
  }, [jobId, result, createEngine])

  const snapshot = useSyncExternalStore(
    engine?.subscribe ?? subscribeToNothing,
    engine?.getSnapshot ?? emptySnapshot,
  )

  // The readout's *value* always comes from the audio clock; animation
  // frames only decide how often it is repainted, which is why pausing stops
  // the loop instead of freezing a counter.
  useEffect(() => {
    if (engine === null) {
      return
    }
    setCurrentTime(engine.currentTime())
    if (!snapshot.playing) {
      return
    }
    let frame = requestAnimationFrame(function tick() {
      setCurrentTime(engine.currentTime())
      frame = requestAnimationFrame(tick)
    })
    return () => {
      cancelAnimationFrame(frame)
    }
  }, [engine, snapshot.playing])

  const togglePlayback = useCallback(() => {
    if (engine === null) {
      return
    }
    if (snapshot.playing) {
      engine.pause()
    } else {
      // Reached from a click, which is the user gesture a suspended
      // AudioContext needs in order to resume.
      void engine.play()
    }
  }, [engine, snapshot.playing])

  // A drag across the timeline moves only what is *displayed*; the seek is
  // committed once, on release. Seeking on every pointer move would stop,
  // discard and rebuild every source node dozens of times a second, each
  // rebuild opening a fresh scheduling lookahead — audible gapping, not
  // scrubbing. The ref that makes "once per gesture" true lives in
  // `StemTimeline`, next to the pointer handlers; what lives here is the
  // displayed position it drives.
  //
  // Feature 052 made the drag *audible* without changing any of that: the
  // moves now also sound a short grain of every stem at the cursor, which
  // schedules throwaway nodes rather than rebuilding the transport, and the
  // one commit on release is `endScrubPreview(seconds)` — that call is the
  // gesture's seek.
  const [scrubValue, setScrubValue] = useState<number | null>(null)

  /**
   * Whether a preview session is open, i.e. whether the commit about to
   * arrive is a *pointer* gesture's. A ref, and read and cleared inside the
   * same handler, for the same reason the timeline's gesture ref is: the
   * decision must be made before React has re-rendered anything.
   *
   * The timeline has one commit path (`onSeek`), deliberately; this is what
   * turns that one path into the right engine transition. A drag commits
   * through `endScrubPreview`, which **is** the gesture's seek — calling
   * `seek` as well would rebuild every source node twice. A keypress with no
   * drag under way commits through `seek`, which has no session to close.
   */
  const previewing = useRef(false)

  const handleScrubStart = useCallback(() => {
    previewing.current = true
    engine?.beginScrubPreview()
  }, [engine])

  // Two things at once, and only two: the displayed position, and a grain of
  // every stem at that position. The engine throttles the preview against its
  // own clock, so this can fire on every pointer move — unlike a seek, which
  // would rebuild the whole graph each time.
  const handleScrub = useCallback(
    (seconds: number) => {
      setScrubValue(seconds)
      engine?.scrubPreview(seconds)
    },
    [engine],
  )

  /** A cancelled gesture commits nothing: the clock is the truth again. */
  const cancelScrub = useCallback(() => {
    previewing.current = false
    // No commit: the playhead goes back to wherever the session left it.
    engine?.endScrubPreview()
    setScrubValue(null)
    setCurrentTime(engine?.currentTime() ?? 0)
  }, [engine])

  const commitSeek = useCallback(
    (seconds: number) => {
      // Read and cleared before either call, so a keyboard commit *during* a
      // drag ends the session (one transport move) and the release that
      // follows finds nothing to commit.
      const fromDrag = previewing.current
      previewing.current = false
      setScrubValue(null)
      setCurrentTime(seconds)
      // The engine can close the session underneath a drag — `play`, `pause`
      // and `seek` all end one defensively, reachable through a second
      // pointer or a programmatic caller. `endScrubPreview` would then be a
      // no-op and the dragged-to position would silently never commit, so
      // the live engine state decides alongside the ref, and a closed
      // session falls back to a plain seek.
      if (fromDrag && engine !== null && engine.getSnapshot().scrubbing) {
        engine.endScrubPreview(seconds)
      } else {
        engine?.seek(seconds)
      }
    },
    [engine],
  )

  const toggleMute = useCallback(
    (name: string) => {
      engine?.toggleMute(name)
    },
    [engine],
  )

  const toggleSolo = useCallback(
    (name: string) => {
      engine?.toggleSolo(name)
    },
    [engine],
  )

  // Batched, like `commitSeek` and unlike `setLevel`: a region set while
  // playing tears down and rebuilds every source node, so the timeline
  // commits one call per gesture and the two "Loop …" buttons are one press
  // each. The engine clamps, and clears rather than setting a degenerate
  // region — so "start after end" needs no special case here.
  const setLoopRegion = useCallback(
    (startSeconds: number, endSeconds: number) => {
      engine?.setLoopRegion(startSeconds, endSeconds)
    },
    [engine],
  )

  const clearLoopRegion = useCallback(() => {
    engine?.clearLoopRegion()
  }, [engine])

  // Continuous, deliberately — unlike `commitSeek`, which batches a whole
  // drag into one call because a seek tears down and rebuilds every source
  // node. A level change is a plain gain write (`AudioParam.value`), so there
  // is nothing to batch against and every `change` event can reach the engine
  // as it fires.
  const setLevel = useCallback(
    (name: string, value: number) => {
      engine?.setLevel(name, value)
    },
    [engine],
  )

  /**
   * The route out of the `inspect` phase. It lives here rather than only on
   * the progress panel because that panel is mounted for `separate` alone:
   * without this control, opening the results would strand the user in
   * `inspect` until a page reload.
   */
  const startAnother = useCallback(() => {
    jobDispatch({ type: 'job/clear' })
    appDispatch({ type: 'results/startAnother' })
  }, [jobDispatch, appDispatch])

  // The remedy for every shape of result-fetch failure: a 409
  // `result_not_available` while the job is still separating resolves once
  // it finishes, and a dropped request is, definitionally, worth retrying.
  const retryResult = useCallback(() => {
    setAttempt((current) => current + 1)
  }, [])

  let body: ReactNode
  if (jobId === null) {
    body = <p className="workspace-hint">No separation job is being tracked.</p>
  } else if (result.status === 'error') {
    body = (
      <>
        <p className="stem-player-error" role="alert">
          {explainError(result.error)}
        </p>
        <button
          type="button"
          className="stem-player-retry"
          onClick={retryResult}
        >
          Try again
        </button>
      </>
    )
  } else if (result.status !== 'loaded') {
    body = (
      <p className="workspace-hint" role="status">
        Loading the separation result…
      </p>
    )
  } else {
    const stems = result.result.stems
    const stemStates = new Map(snapshot.stems.map((stem) => [stem.name, stem]))
    const longestStem = stems.reduce(
      (longest, stem) => Math.max(longest, stem.duration_seconds),
      0,
    )
    const duration =
      snapshot.durationSeconds > 0 ? snapshot.durationSeconds : longestStem
    const ready = snapshot.status === 'ready'
    const engineError =
      snapshot.error === null ? null : errorInfo(snapshot.error)
    /** The drag position while a gesture is in flight, the clock otherwise. */
    const position = Math.min(scrubValue ?? currentTime, duration)
    const loopRegion = snapshot.loopRegion

    /**
     * "Loop start" means *loop from here*: to the region's own end when that
     * is still ahead of the playhead, and to the end of the mix otherwise —
     * which is also what it means with no region yet. "Loop end" is the
     * mirror image, falling back to the start of the mix. An impossible pair
     * therefore never reaches the engine as one; it resolves to the widest
     * region consistent with the edge the user just placed, rather than being
     * silently swapped (which would move the edge they did *not* touch).
     */
    const markLoopStart = (): void => {
      setLoopRegion(
        position,
        loopRegion !== null && loopRegion.end > position
          ? loopRegion.end
          : duration,
      )
    }

    const markLoopEnd = (): void => {
      setLoopRegion(
        loopRegion !== null && loopRegion.start < position
          ? loopRegion.start
          : 0,
        position,
      )
    }

    // Lane rows come from the *result*, merged with whatever the engine
    // snapshot knows so far: the rows have to exist while the stems are still
    // decoding, and the snapshot is empty until they have. Either way the
    // count comes from the data, never from a literal (AGENTS.md principle 6).
    const timelineStems: StemTimelineStem[] = stems.map((stem) => {
      const state = stemStates.get(stem.name)
      return {
        name: stem.name,
        status: state?.status ?? 'loading',
        muted: state?.muted ?? false,
        soloed: state?.soloed ?? false,
        audible: state?.audible ?? true,
        // The engine's own default until a snapshot exists to read from.
        level: state?.level ?? 1,
        // The decoded length once there is one; the contract's until then.
        durationSeconds:
          state !== undefined && state.durationSeconds > 0
            ? state.durationSeconds
            : stem.duration_seconds,
      }
    })

    body = (
      <>
        {snapshot.status !== 'ready' && snapshot.status !== 'error' && (
          <p className="workspace-hint" role="status">
            Decoding stems…
          </p>
        )}

        {engineError !== null && (
          <p className="stem-player-error" role="alert">
            {explainError(engineError)}
          </p>
        )}

        <StemTimeline
          stems={timelineStems}
          durationSeconds={duration}
          positionSeconds={position}
          ready={ready}
          engine={engine}
          onScrubStart={handleScrubStart}
          onScrub={handleScrub}
          onScrubCancel={cancelScrub}
          onSeek={commitSeek}
          loopRegion={loopRegion}
          onSetLoopRegion={setLoopRegion}
          onClearLoopRegion={clearLoopRegion}
          onTogglePlayback={togglePlayback}
          onToggleMute={toggleMute}
          onToggleSolo={toggleSolo}
          onSetLevel={setLevel}
        />

        <div className="stem-player-transport">
          <button
            type="button"
            className="stem-player-play"
            disabled={!ready}
            onClick={togglePlayback}
          >
            {snapshot.playing ? 'Pause' : 'Play'}
          </button>
          <p className="stem-player-time">
            {formatDuration(position)} / {formatDuration(duration)}
          </p>

          {/*
            The keyboard and screen-reader path to a loop region: the ruler
            drag that draws one is a pointer gesture on an `aria-hidden`
            picture, so the transport carries the same three intents as
            buttons. The badge is the live region that says what came of them.
          */}
          <div className="stem-player-loop-controls">
            <button
              type="button"
              className="stem-player-loop"
              disabled={!ready}
              onClick={markLoopStart}
            >
              Loop start
            </button>
            <button
              type="button"
              className="stem-player-loop"
              disabled={!ready}
              onClick={markLoopEnd}
            >
              Loop end
            </button>
            <button
              type="button"
              className="stem-player-loop"
              disabled={loopRegion === null}
              onClick={clearLoopRegion}
            >
              Clear loop
            </button>
          </div>

          {/*
            Mounted whether or not there is a region: a live region has to be
            in the DOM *before* its content changes for the change to be
            announced.
          */}
          <div className="stem-player-loop-status" aria-live="polite">
            {loopRegion !== null && (
              <p className="stem-player-loop-badge">
                {`Loop ${formatDuration(loopRegion.start)} – ${formatDuration(loopRegion.end)}`}
              </p>
            )}
          </div>
        </div>
      </>
    )
  }

  return (
    <section className="stem-player" aria-label="Stem player">
      <h2 className="stem-player-title">Stems</h2>
      {body}
      <button
        type="button"
        className="stem-player-restart"
        onClick={startAnother}
      >
        Start another separation
      </button>
    </section>
  )
}
