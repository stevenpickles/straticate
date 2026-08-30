/**
 * The playback session for the tracked job: its separation result, its audio
 * engine, and the timeline window it is being looked at through.
 *
 * ## The ownership invariant
 *
 * **The session's lifetime is the tracked job's, not the Inspect screen's.**
 * Before feature 065 the engine and the result fetch lived in `StemPlayer`, so
 * anything that unmounted the player — a phase change, a trip to the model
 * library in a future build, a re-render that swapped the subtree — disposed
 * the Web Audio graph and re-downloaded every stem on the way back (about ten
 * seconds for a four-minute job), losing the playhead, the loop region and the
 * zoom window with it. Hoisting both here inverts that: the player becomes a
 * *view* of a session that already exists, and only the job changing takes the
 * session down.
 *
 * This is App.tsx's model-library rule generalised. That component already
 * hides the workspace rather than unmounting it, precisely so "the stem
 * player's Web Audio graph … survives a trip to the library and back"; the
 * survival now comes from where the session lives rather than from a `hidden`
 * attribute one screen happens to use.
 *
 * ### The consequence worth stating out loud
 *
 * **Audio that is playing keeps playing when the Inspect UI goes away.** The
 * transport belongs to the session, and nothing about unmounting a view stops
 * it. That is the same behaviour the model library has advertised since
 * feature 037, not a novelty of this one — but it is the reason
 * `job/clear` disposes *immediately* rather than lazily: "Start another
 * separation" has to be silence.
 *
 * ## What it owns, and when each thing is built
 *
 * 1. **The result fetch.** `GET /jobs/{id}/result` is the contract's source of
 *    truth and the only thing that can say *why* there is nothing to play
 *    (feature 021's 409 `detail.state`). Its state machine and feature 048's
 *    `attempt` counter moved here with the engine, deliberately: left in the
 *    player they would refetch on every remount, and each fetch would hand the
 *    engine a new `result` identity and force a rebuild. Only the *settled*
 *    outcomes are stored; `idle` and `loading` are derived from whether the
 *    session is open and which attempt has answered.
 * 2. **The engine**, built on the first `load` the session needs and then kept.
 *    A changed result for the **same** job — a 048 retry that finally succeeds
 *    — calls `load()` again on the *same* instance rather than disposing and
 *    recreating: `load()` is generation-guarded and tears the old graph down
 *    itself.
 * 3. **The timeline window**, as a {@link TimelineWindowStore} over a ref.
 *    Zoom and scroll change on every wheel tick, so they must not be provider
 *    state; `useTimelineGeometry` seeds from the store and writes back through
 *    it.
 *
 * ## Nothing is downloaded for a job that is never inspected
 *
 * A session stays shut until {@link StemSessionValue.openSession} is called —
 * `StemPlayer` calls it on mount. Until then there is no fetch and no engine,
 * so a job watched through to completion and then abandoned costs nothing.
 *
 * ## Dispose triggers
 *
 * | Trigger | What happens |
 * | --- | --- |
 * | `job/clear` (both "Start another separation" sites) | dispose at once — sources stopped, buffers dropped, context closed |
 * | a different job is tracked | dispose the old session, reset result/attempt and the window store: a new job is a new timeline |
 * | the provider unmounts | dispose — the app is going away |
 *
 * Unmounting `StemPlayer` is **not** on that list, which is the whole feature.
 *
 * ## Memory
 *
 * A retained session retains decoded audio: roughly 340 MB for a four-stem,
 * four-minute stereo job at 44.1 kHz (`4 stems × 2 ch × 44100 × 240 s × 4 B`).
 * That is the price of not re-downloading, and it is bounded — one session,
 * freed the moment the job stops being tracked. Lazy opening is the mitigation
 * that keeps a job the user never listened to at zero.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { ApiError } from '../api/client'
import { getSeparationResult, stemUrl } from '../api/stems'
import type { SeparationResult } from '../api/types'
import { createStemAudioEngine, type StemPlayerEngine } from '../audio/engine'
import {
  WHOLE_FILE,
  type TimelineWindow,
  type TimelineWindowStore,
} from '../components/useTimelineGeometry'
import { useJobState } from './jobState'

/** Envelope-shaped view of any rejection. */
export interface ErrorInfo {
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

/** Envelope-shaped `{code, message, detail}` for any rejection reason. */
export function errorInfo(reason: unknown): ErrorInfo {
  return reason instanceof ApiError
    ? { code: reason.code, message: reason.message, detail: reason.detail }
    : UNKNOWN_ERROR
}

/** State of the `GET /jobs/{id}/result` fetch. */
export type ResultState =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | { readonly status: 'loaded'; readonly result: SeparationResult }
  | { readonly status: 'error'; readonly error: ErrorInfo }

/** Nothing fetched (yet); shared so identity comparisons stay cheap. */
const IDLE_RESULT: ResultState = { status: 'idle' }

/** A fetch is in flight; shared for the same reason. */
const LOADING_RESULT: ResultState = { status: 'loading' }

/**
 * A **settled** result fetch, tagged with the request it answered.
 *
 * Only the outcomes are stored; `idle` and `loading` are derived from whether
 * the session is open and whether the settled tag still matches the request
 * that is current. Storing them instead would mean writing state
 * synchronously from an effect on every job change — a cascading render, and
 * a frame in which a closed session still reports the last job's stems.
 */
type SettledResult =
  | {
      readonly key: string
      readonly status: 'loaded'
      readonly result: SeparationResult
    }
  | {
      readonly key: string
      readonly status: 'error'
      readonly error: ErrorInfo
    }

/** What {@link useStemSession} hands back. */
export interface StemSessionValue {
  /** State of the tracked job's result fetch. */
  readonly result: ResultState
  /**
   * The engine playing this job's stems, or `null` before the session has one
   * (nothing opened it, or the result has not arrived yet).
   */
  readonly engine: StemPlayerEngine | null
  /**
   * Where the timeline's `{ zoom, scrollSeconds }` lives, so it outlives the
   * timeline. Stable for the life of the session.
   */
  readonly windowStore: TimelineWindowStore
  /**
   * Open the session for the tracked job: fetch its result and, once that
   * lands, build and load the engine. Idempotent, and stable until the tracked
   * job changes — a view calls it from a mount effect, and the change of
   * identity is what re-opens the session for a new job.
   */
  readonly openSession: () => void
  /**
   * Fetch the result again for the same job. The remedy for every shape of
   * result-fetch failure (feature 048): a 409 `result_not_available` while the
   * job is still separating resolves once it finishes, and a dropped request
   * is, definitionally, worth retrying.
   */
  readonly retryResult: () => void
}

const StemSessionContext = createContext<StemSessionValue | undefined>(
  undefined,
)

/** Props for {@link StemSessionProvider}. */
export interface StemSessionProviderProps {
  children: ReactNode
  /**
   * Builds the playback engine. Defaults to the real Web Audio engine; tests
   * inject a fake. Must be stable across renders — a new function identity
   * would be a new engine for the next load.
   */
  readonly createEngine?: () => StemPlayerEngine
}

/**
 * Provides the tracked job's stem session. Mount it once, inside
 * `JobStateProvider` and above anything that plays stems.
 *
 * See the module docstring for what it owns and when it lets go of it.
 */
export function StemSessionProvider({
  children,
  createEngine = createStemAudioEngine,
}: StemSessionProviderProps) {
  const { job } = useJobState()
  const jobId = job?.id ?? null

  const [settled, setSettled] = useState<SettledResult | null>(null)
  const [engine, setEngine] = useState<StemPlayerEngine | null>(null)

  /**
   * The job the session has been opened for, or `null` for a shut one. A job
   * *id* rather than a flag, so "open" is always answered against the job
   * being tracked right now: the tracked job changing shuts the session
   * without any state having to be written, which is what keeps a job nobody
   * inspected from fetching anything.
   */
  const [openedFor, setOpenedFor] = useState<string | null>(null)
  const open = openedFor !== null && openedFor === jobId

  // Bumped by `retryResult`, which is the only thing that ever changes it.
  // Widening the fetch effect's dependencies onto it is what turns that click
  // into a genuine refetch of the same job.
  const [attempt, setAttempt] = useState(0)

  /**
   * Which fetch is current: the job asked about and how many times it has been
   * asked. A settled answer carrying a different key is an older attempt's,
   * and the session reads as `loading` until the matching one lands.
   */
  const requestKey = jobId === null ? null : `${jobId}#${String(attempt)}`

  /**
   * The live engine, mirrored outside React state so the teardown effect can
   * reach it without listing it as a dependency — depending on the engine
   * would make *creating* one a reason to dispose it.
   */
  const engineRef = useRef<StemPlayerEngine | null>(null)

  /**
   * The timeline window. A ref, not state: it changes on every wheel tick and
   * nothing above the timeline needs to re-render when it does.
   */
  const timelineWindowRef = useRef<TimelineWindow>(WHOLE_FILE)
  const windowStore = useMemo<TimelineWindowStore>(
    () => ({
      get: () => timelineWindowRef.current,
      set: (next) => {
        timelineWindowRef.current = next
      },
    }),
    [],
  )

  // Identity changes with the tracked job, deliberately: a view that calls
  // this from a mount effect re-opens the session when the job changes
  // underneath it, and does nothing at all otherwise.
  const openSession = useCallback(() => {
    setOpenedFor(jobId)
  }, [jobId])

  const retryResult = useCallback(() => {
    setAttempt((current) => current + 1)
  }, [])

  // The result is fetched rather than read off `job.result`: the REST route is
  // the contract's source of truth, and it is the only thing that can tell us
  // *why* there is nothing to play (021's 409 `detail.state`).
  useEffect(() => {
    if (jobId === null || requestKey === null || !open) {
      // Nothing to fetch, and nothing to reset either: a shut session reads as
      // `idle` by derivation below rather than by writing state from here.
      return
    }
    let current = true
    getSeparationResult(jobId)
      .then((fetched) => {
        if (current) {
          setSettled({ key: requestKey, status: 'loaded', result: fetched })
        }
      })
      .catch((reason: unknown) => {
        if (current) {
          setSettled({
            key: requestKey,
            status: 'error',
            error: errorInfo(reason),
          })
        }
      })
    return () => {
      current = false
    }
  }, [jobId, open, requestKey])

  /**
   * What the session has to say about the result right now. Memoised, because
   * this identity is what decides whether the engine reloads: a fresh object
   * per render would reload the mix on every keystroke in the app.
   */
  const result = useMemo<ResultState>(() => {
    if (!open || requestKey === null) {
      return IDLE_RESULT
    }
    if (settled === null || settled.key !== requestKey) {
      return LOADING_RESULT
    }
    return settled.status === 'loaded'
      ? { status: 'loaded', result: settled.result }
      : { status: 'error', error: settled.error }
  }, [open, requestKey, settled])

  // One engine per **job**, not per result: a result that changes for the same
  // job (048's "Try again" finally succeeding) reloads the instance that is
  // already here. `load()` is generation-guarded and tears down the old graph,
  // so a reload is the cheaper and safer of the two, and it keeps every
  // consumer's reference valid.
  useEffect(() => {
    if (jobId === null || !open || result.status !== 'loaded') {
      return
    }
    const instance = engineRef.current ?? createEngine()
    engineRef.current = instance
    setEngine(instance)
    void instance.load(
      result.result.stems.map((stem) => ({
        name: stem.name,
        url: stemUrl(jobId, stem.name),
      })),
    )
  }, [jobId, open, result, createEngine])

  // The session's teardown, and the only one. It runs when the tracked job
  // changes — to another job or to none, which is what `job/clear` does — and
  // when the provider itself unmounts. Nothing here is keyed to a view, which
  // is the feature.
  //
  // React's development double-invoke runs it once on mount with nothing built
  // yet; `dispose()` is idempotent and aborts any download still in flight, so
  // the second pass starts from a clean session either way.
  useEffect(() => {
    return () => {
      const instance = engineRef.current
      engineRef.current = null
      instance?.dispose()
      setEngine(null)
      setSettled(null)
      setAttempt(0)
      setOpenedFor(null)
      // A new job is a new timeline: a window measured against the last one
      // would name seconds that no longer mean anything — the same reasoning
      // the engine applies to a loop region in `load()`.
      timelineWindowRef.current = WHOLE_FILE
    }
  }, [jobId])

  const value = useMemo<StemSessionValue>(
    () => ({ result, engine, windowStore, openSession, retryResult }),
    [result, engine, windowStore, openSession, retryResult],
  )

  return (
    <StemSessionContext.Provider value={value}>
      {children}
    </StemSessionContext.Provider>
  )
}

/**
 * Read the tracked job's stem session. Must be used under a
 * {@link StemSessionProvider}.
 */
export function useStemSession(): StemSessionValue {
  const session = useContext(StemSessionContext)
  if (session === undefined) {
    throw new Error('useStemSession must be used within a StemSessionProvider')
  }
  return session
}
