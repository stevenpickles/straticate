import {
  useCallback,
  useEffect,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
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
import { useJobState } from '../state/jobState'
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
 * Every stem row comes from `SeparationResult.stems` — a two-stem and a
 * four-stem job render through the same code, and no stem name or count
 * appears anywhere here (AGENTS.md principle 6). The audio itself is handled
 * by `src/audio/engine.ts`, which keeps the stems on one clock; this
 * component only renders its snapshot and forwards the user's intent.
 *
 * Playback is an **inspection tool** (ARCHITECTURE.md §13): transport, solo
 * and mute, and nothing that edits audio.
 *
 * Must be rendered under a `JobStateProvider`.
 */
export function StemPlayer({
  createEngine = createStemAudioEngine,
}: StemPlayerProps = {}) {
  const { job } = useJobState()
  const jobId = job?.id ?? null

  const [result, setResult] = useState<ResultState>({ status: 'idle' })
  const [engine, setEngine] = useState<StemPlayerEngine | null>(null)
  const [currentTime, setCurrentTime] = useState(0)

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
  }, [jobId])

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

  const handleSeek = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const seconds = Number(event.target.value)
      setCurrentTime(seconds)
      engine?.seek(seconds)
    },
    [engine],
  )

  if (jobId === null) {
    return (
      <section className="stem-player" aria-label="Stem player">
        <p className="workspace-hint">No separation job is being tracked.</p>
      </section>
    )
  }

  if (result.status === 'error') {
    return (
      <section className="stem-player" aria-label="Stem player">
        <p className="stem-player-error" role="alert">
          {explainError(result.error)}
        </p>
      </section>
    )
  }

  if (result.status !== 'loaded') {
    return (
      <section className="stem-player" aria-label="Stem player">
        <p className="workspace-hint" role="status">
          Loading the separation result…
        </p>
      </section>
    )
  }

  const stems = result.result.stems
  const stemStates = new Map(snapshot.stems.map((stem) => [stem.name, stem]))
  const longestStem = stems.reduce(
    (longest, stem) => Math.max(longest, stem.duration_seconds),
    0,
  )
  const duration =
    snapshot.durationSeconds > 0 ? snapshot.durationSeconds : longestStem
  const ready = snapshot.status === 'ready'
  const engineError = snapshot.error === null ? null : errorInfo(snapshot.error)

  return (
    <section className="stem-player" aria-label="Stem player">
      <h2 className="stem-player-title">Stems</h2>

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

      <ul className="stem-player-stems">
        {stems.map((stem) => {
          const state = stemStates.get(stem.name)
          const status = state?.status ?? 'loading'
          const muted = state?.muted ?? false
          const soloed = state?.soloed ?? false
          return (
            <li className="stem-player-stem" key={stem.name}>
              <span className="stem-player-stem-name">{stem.name}</span>
              <span className="stem-player-stem-detail">
                {status === 'loaded'
                  ? formatDuration(stem.duration_seconds)
                  : status === 'error'
                    ? 'Unavailable'
                    : 'Loading…'}
              </span>
              <button
                type="button"
                className="stem-player-toggle"
                aria-label={`Mute ${stem.name}`}
                aria-pressed={muted}
                disabled={status !== 'loaded'}
                onClick={() => {
                  engine?.toggleMute(stem.name)
                }}
              >
                Mute
              </button>
              <button
                type="button"
                className="stem-player-toggle"
                aria-label={`Solo ${stem.name}`}
                aria-pressed={soloed}
                disabled={status !== 'loaded'}
                onClick={() => {
                  engine?.toggleSolo(stem.name)
                }}
              >
                Solo
              </button>
            </li>
          )
        })}
      </ul>

      <div className="stem-player-transport">
        <button
          type="button"
          className="stem-player-play"
          disabled={!ready}
          onClick={togglePlayback}
        >
          {snapshot.playing ? 'Pause' : 'Play'}
        </button>
        <input
          type="range"
          className="stem-player-seek"
          aria-label="Seek"
          min={0}
          max={duration}
          step={0.01}
          value={Math.min(currentTime, duration)}
          disabled={!ready}
          onChange={handleSeek}
        />
        <p className="stem-player-time">
          {formatDuration(currentTime)} / {formatDuration(duration)}
        </p>
      </div>
    </section>
  )
}
