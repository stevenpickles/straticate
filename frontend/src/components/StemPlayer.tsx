import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react'
import type { StemEngineSnapshot } from '../audio/engine'
import { formatDuration } from '../format'
import { useAppDispatch } from '../state/appState'
import { useJobDispatch, useJobState } from '../state/jobState'
import { errorInfo, useStemSession, type ErrorInfo } from '../state/stemSession'
import { StemTimeline, type StemTimelineStem } from './StemTimeline'
import './StemPlayer.css'

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

/**
 * The `inspect` step of the workflow: listen to a completed job's separated
 * stems.
 *
 * **It is a view of a session, not the owner of one** (feature 065). The
 * result fetch, the engine and the timeline window belong to
 * `StemSessionProvider`, whose lifetime is the tracked job's; this component
 * opens that session on mount and reads it. Unmounting the player therefore
 * costs nothing — no disposal, no re-download, no lost playhead — and audio
 * that is playing carries on, exactly as it already did behind the model
 * library. Only the job changing takes the session down.
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
 * Two failures can be retried here, and they are not the same failure
 * (feature 064 completed the pair feature 048 started). A failed **result
 * fetch** replaces the whole body, and its "Try again" refetches the result.
 * A failed **stem-audio download** leaves the player standing — the stems
 * that did load are playable — so its "Try again" sits beside the alert and
 * asks the engine to re-fetch only the stems that failed. It is rendered
 * only when some stem is in `error`: a transport failure has nothing to
 * re-fetch, and Play is its remedy. Either click moves focus onto the player
 * region first, because the button it was made on is about to unmount.
 *
 * It also carries the route out of the `inspect` phase ("Start another
 * separation"), because the progress panel that offers the same control is
 * mounted only for `separate` — see the handler for why that matters.
 *
 * Must be rendered under an `AppStateProvider`, a `JobStateProvider` and a
 * `StemSessionProvider`.
 */
export function StemPlayer() {
  const { job } = useJobState()
  const jobDispatch = useJobDispatch()
  const appDispatch = useAppDispatch()
  const jobId = job?.id ?? null

  const {
    result,
    engine,
    windowStore,
    openSession,
    retryResult: refetchResult,
    persistView,
  } = useStemSession()

  // Opening the session is what starts the result fetch and, in time, the
  // stem downloads — so a job the user never opens costs nothing. `openSession`
  // changes identity with the tracked job, which is what re-opens the session
  // when the job changes while this view stays mounted.
  useEffect(() => {
    openSession()
  }, [openSession])

  // Seeded from the engine, not from zero: re-entering the player lands on the
  // playhead the session was left at rather than snapping to the start for a
  // frame. On a first mount there is no engine yet and the effect below fills
  // it in as soon as there is one.
  const [currentTime, setCurrentTime] = useState(
    () => engine?.currentTime() ?? 0,
  )

  /**
   * The player region itself, so a retry can put focus somewhere meaningful
   * before the button it was clicked on unmounts (feature 048 recorded the
   * drop to `<body>` as an accepted trade-off; this is that handoff).
   */
  const playerRef = useRef<HTMLElement>(null)

  const snapshot = useSyncExternalStore(
    engine?.subscribe ?? subscribeToNothing,
    engine?.getSnapshot ?? emptySnapshot,
  )

  // The readout's *value* always comes from the audio clock; animation
  // frames only decide how often it is repainted, which is why pausing stops
  // the loop instead of freezing a counter.
  //
  // `snapshot.status` is in the dependency list for feature 066: the session
  // can move the clock itself, restoring a persisted playhead once the
  // engine reaches `ready`, with no gesture on this component to have called
  // `setCurrentTime` on the way. That restore lands before this effect's next
  // run — `engine.seek()` is called synchronously from the same snapshot
  // notification that flips `status`, and this effect only runs after —  so
  // re-reading the clock on that transition is what picks it up. Reading it
  // on every other `status` change is free: `engine.currentTime()` has not
  // moved since the last read, `setCurrentTime` bails out on the same value,
  // and nothing repaints.
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
  }, [engine, snapshot.playing, snapshot.status])

  const togglePlayback = useCallback(() => {
    if (engine === null) {
      return
    }
    if (snapshot.playing) {
      engine.pause()
      // A pause is a commit point (feature 066): the playhead has stopped
      // moving, so this is where it is worth writing down. Play is not — the
      // position at the moment of pressing Play is whatever the last commit
      // already recorded.
      persistView()
    } else {
      // Reached from a click, which is the user gesture a suspended
      // AudioContext needs in order to resume.
      void engine.play()
    }
  }, [engine, snapshot.playing, persistView])

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
      // The seek commit (feature 066) — one write per gesture, exactly like
      // the one transport move above it.
      persistView()
    },
    [engine, persistView],
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
      // The loop-set commit (feature 066).
      persistView()
    },
    [engine, persistView],
  )

  const clearLoopRegion = useCallback(() => {
    engine?.clearLoopRegion()
    // The loop-clear commit (feature 066): a cleared region is itself
    // worth persisting, or a reload would restore the one it replaced.
    persistView()
  }, [engine, persistView])

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

  /**
   * Move focus onto the player region before a retry control unmounts.
   *
   * Both retry buttons disappear the moment their state flips — the result
   * one takes the whole error branch with it, the stem one loses its
   * condition — and the browser's default for a focused element that is
   * removed is to drop focus to `<body>`, stranding a keyboard user at the
   * top of the document. The region is the nearest thing that survives every
   * one of these transitions, so it is where focus goes; `tabIndex={-1}`
   * makes it focusable programmatically without adding a tab stop.
   */
  const keepFocusInPlayer = useCallback(() => {
    playerRef.current?.focus()
  }, [])

  // The remedy for every shape of result-fetch failure: a 409
  // `result_not_available` while the job is still separating resolves once
  // it finishes, and a dropped request is, definitionally, worth retrying.
  // The refetch itself is the session's, because the result is.
  const retryResult = useCallback(() => {
    keepFocusInPlayer()
    refetchResult()
  }, [keepFocusInPlayer, refetchResult])

  /**
   * The remedy for a failed **stem-audio** download, which is a different
   * failure from the one above: the result loaded, so there is an engine, and
   * only the stems whose bytes never arrived are re-fetched. Deliberately not
   * a reload — the stems that did load keep their buffers, their levels and,
   * if the mix is playing, their place in it.
   */
  const retryStems = useCallback(() => {
    keepFocusInPlayer()
    void engine?.retryFailedStems()
  }, [engine, keepFocusInPlayer])

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
    /**
     * Whether the failure on screen has anything a retry could fix. A stem
     * whose bytes never arrived does; a transport failure — an autoplay
     * rejection, a context the browser closed — does not, and `play()` is its
     * remedy, so offering "Try again" there would be a button that fetches
     * nothing. Derived from the data, so it never names a stem or a count.
     */
    const retryableStems = snapshot.stems.some(
      (stem) => stem.status === 'error',
    )
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

        {retryableStems && (
          <button
            type="button"
            className="stem-player-retry"
            onClick={retryStems}
          >
            Try again
          </button>
        )}

        <StemTimeline
          stems={timelineStems}
          durationSeconds={duration}
          positionSeconds={position}
          ready={ready}
          engine={engine}
          windowStore={windowStore}
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
    <section
      className="stem-player"
      aria-label="Stem player"
      ref={playerRef}
      // Focusable programmatically but not a tab stop: it exists so a retry
      // click has somewhere to leave focus when its button unmounts.
      tabIndex={-1}
    >
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
