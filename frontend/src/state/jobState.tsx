/**
 * State of the separation job currently being tracked, fed by REST
 * responses and WebSocket events.
 *
 * The store tracks **one** job at a time — the one the user started. Every
 * WebSocket event is filtered by `job_id`, so events for other jobs (the
 * hub broadcasts to all clients) and stale events that arrive after the
 * tracked job changed are ignored.
 *
 * `job` is the best-known record of that job: it starts as the REST
 * response and is kept current by events. Chunk-grained progress detail
 * (which the `Job` schema does not carry) lives in {@link JobProgressDetail},
 * and the newest `runtime_metrics` payload is stored verbatim for the
 * telemetry panel (feature 020).
 *
 * The store also holds the state of the user's cancel request
 * ({@link CancelRequestState}), which outlives the HTTP call: cancelling is
 * a request, and the job only really stops when a terminal state arrives.
 */
import {
  createContext,
  useContext,
  useReducer,
  type Dispatch,
  type ReactNode,
} from 'react'
import type {
  Job,
  JobState as JobLifecycleState,
  RuntimeMetricsEvent,
  WebSocketEvent,
} from '../api/types'
import type { ConnectionStatus } from '../ws/client'

/** Job lifecycle states from which no further transition happens. */
export const TERMINAL_JOB_STATES = [
  'completed',
  'cancelled',
  'failed',
] as const satisfies readonly JobLifecycleState[]

/** A lifecycle state from which no further transition happens. */
export type TerminalJobState = (typeof TERMINAL_JOB_STATES)[number]

/** A lifecycle state a job can still move on from. */
export type ProcessingJobStage = Exclude<JobLifecycleState, TerminalJobState>

const terminalJobStates = new Set<JobLifecycleState>(TERMINAL_JOB_STATES)

/** Whether a job lifecycle state is terminal (`completed`/`cancelled`/`failed`). */
export function isTerminalJobState(
  state: JobLifecycleState,
): state is TerminalJobState {
  return terminalJobStates.has(state)
}

/**
 * Position of each non-terminal stage in the linear processing order
 * (ARCHITECTURE.md §6), mirroring the backend's `_PROCESSING_ORDER` in
 * `backend/src/straticate/jobs/state.py`. `cancelled` and `failed` have no
 * position: a job reaches them from *any* non-terminal stage, so they are not
 * points on a forward ordering and are handled as terminal instead.
 *
 * The `Record<ProcessingJobStage, number>` annotation is doing real work: it
 * is the exhaustiveness check. A stage added to (or removed from) the
 * backend's `JobState` enum changes the generated union, and this object then
 * has the wrong shape and fails to compile — right here, which is where the
 * new stage's position in the order has to be decided. There is deliberately
 * no fallback for an unknown stage, because silently ranking one at `-1`
 * would turn a contract change into a subtly wrong UI.
 */
const JOB_STAGE_RANK: Readonly<Record<ProcessingJobStage, number>> = {
  queued: 0,
  preparing: 1,
  decoding: 2,
  loading_model: 3,
  separating: 4,
  post_processing: 5,
  encoding: 6,
}

/**
 * Whether moving a job from `tracked` to `incoming` would move it
 * **backwards** through its lifecycle.
 *
 * - A terminal job may only be replaced by another terminal record. It has
 *   genuinely stopped, so no further event will ever arrive to correct a
 *   demotion.
 * - A non-terminal job may always move to a terminal one: that is the job
 *   finishing, cancelling or failing.
 * - Between two non-terminal stages, the processing order decides. Equal
 *   stages are not backwards — a later record of the same stage is still
 *   newer information.
 */
export function isBackwardJobTransition(
  tracked: JobLifecycleState,
  incoming: JobLifecycleState,
): boolean {
  if (isTerminalJobState(tracked)) {
    return !isTerminalJobState(incoming)
  }
  if (isTerminalJobState(incoming)) {
    return false
  }
  return JOB_STAGE_RANK[incoming] < JOB_STAGE_RANK[tracked]
}

/**
 * Chunk-grained progress from the newest `job_progress` event. Progress is
 * real work: `chunksCompleted / chunksTotal`.
 */
export interface JobProgressDetail {
  /** Overall progress in `[0, 1]`. */
  readonly progress: number
  /** Chunks processed so far. */
  readonly chunksCompleted: number
  /** Total chunks to process. */
  readonly chunksTotal: number
  /** Elapsed processing time in seconds. */
  readonly elapsedSeconds: number
  /** Audio processed so far, in seconds. */
  readonly audioProcessedSeconds: number
  /** Total audio duration, in seconds. */
  readonly audioTotalSeconds: number
}

/**
 * State of the user's cancel request for the tracked job.
 *
 * Cancellation is a *request*, not a stop: `POST /jobs/{id}/cancel` may
 * return a job that is still in a processing state, and the authoritative
 * transition arrives as a `job_cancelled` event. `requesting` therefore
 * covers the whole wait — from the click until the job reaches a terminal
 * state — so the UI can show a "cancelling" affordance throughout.
 */
export type CancelRequestState =
  | {
      /** No cancellation has been requested (or the last one settled). */
      readonly status: 'idle'
    }
  | {
      /** A cancellation was requested and the job has not stopped yet. */
      readonly status: 'requesting'
    }
  | {
      /** The cancel request itself failed; the user may retry. */
      readonly status: 'error'
      /** Machine-readable code from the backend error envelope. */
      readonly code: string
      /** Human-readable message from the backend error envelope. */
      readonly message: string
    }

/** Shape of the tracked-job state. */
export interface JobStateValue {
  /**
   * Best-known record of the tracked job, or `null` when none is tracked.
   * Read `job.state` for the live lifecycle state, `job.result` for the
   * terminal result, and `job.error` for the terminal failure.
   */
  readonly job: Job | null
  /** Newest chunk-grained progress, or `null` before the first `job_progress`. */
  readonly progress: JobProgressDetail | null
  /** Newest `runtime_metrics` event, stored verbatim; `null` until one arrives. */
  readonly metrics: RuntimeMetricsEvent | null
  /** Stage the job was in when cancelled, or `null` when it was not cancelled. */
  readonly cancelledAtStage: JobLifecycleState | null
  /** Status of the job event WebSocket. */
  readonly connection: ConnectionStatus
  /**
   * State of the user's cancel request. Reset when a different job is
   * tracked, and cleared as soon as the job reaches a terminal state.
   */
  readonly cancel: CancelRequestState
}

/** Actions accepted by {@link jobReducer}. */
export type JobAction =
  | {
      /**
       * Track a job from a REST response (create, fetch, or cancel).
       * Tracking a different job resets progress, metrics, cancellation
       * stage, and the cancel request. A snapshot that is *behind* the
       * event stream can never demote the tracked job out of a terminal
       * state (see {@link trackJob}).
       */
      readonly type: 'job/track'
      readonly job: Job
    }
  | {
      /** Stop tracking any job and return to the initial state. */
      readonly type: 'job/clear'
    }
  | {
      /** A decoded WebSocket event; ignored unless it targets the tracked job. */
      readonly type: 'ws/event'
      readonly event: WebSocketEvent
    }
  | {
      /** The WebSocket connection status changed. */
      readonly type: 'ws/status'
      readonly status: ConnectionStatus
    }
  | {
      /**
       * A cancellation was requested for the tracked job. Ignored when no
       * job is tracked or the tracked job is already terminal.
       */
      readonly type: 'cancel/requested'
    }
  | {
      /** The cancel request failed; the message is shown and retryable. */
      readonly type: 'cancel/failed'
      readonly code: string
      readonly message: string
    }

/** Cancel slice at rest; shared so identity comparisons stay cheap. */
const idleCancelRequest: CancelRequestState = { status: 'idle' }

/** Initial state: no job tracked, socket closed, no cancellation requested. */
export const initialJobState: JobStateValue = {
  job: null,
  progress: null,
  metrics: null,
  cancelledAtStage: null,
  connection: 'closed',
  cancel: idleCancelRequest,
}

function trackJob(state: JobStateValue, job: Job): JobStateValue {
  if (state.job !== null && state.job.id === job.id) {
    // **A REST snapshot may never move the tracked job backwards.**
    //
    // A REST record is a point-in-time snapshot and races the event stream.
    // `getJob` (on reconnect, when a restored job is adopted, and right after
    // `POST /jobs`) and `cancelJob` all answer with the job as it was when the
    // handler ran, and HTTP and the WebSocket are separate connections whose
    // relative arrival order is not guaranteed. So an answer prepared before
    // an event this store has already applied can land after it.
    //
    // Events are authoritative for forward progress; REST is authoritative
    // only for resyncing a job the events have not already carried further.
    //
    // The rule was originally drawn around terminal states only, because that
    // is where it is *permanent*: the job has stopped, no further event will
    // arrive to correct the demotion, and the UI is stranded mid-run forever
    // (features 017 and 031). But the same race rewinds non-terminal stages
    // too — `POST /jobs` answers `queued`, the socket delivers `separating`,
    // and a `GET` served in between writes `queued` back, at which point the
    // panel says "waiting in the queue" and hides the progress bar until the
    // next event lands. Visibly wrong, just not permanently so. Both are the
    // same mistake, so both are refused here.
    //
    // It lives in the reducer, not in the callers, so every present and future
    // `job/track` producer inherits it rather than having to rediscover the
    // race.
    if (isBackwardJobTransition(state.job.state, job.state)) {
      return state
    }
    // Same stage, older progress: the same staleness one level finer. Chunk
    // counts only ever climb, so a lower figure is a snapshot the events have
    // already overtaken, and applying it would walk the progress bar back.
    if (job.state === state.job.state && job.progress < state.job.progress) {
      return state
    }
    return { ...state, job }
  }
  return {
    ...state,
    job,
    progress: null,
    metrics: null,
    cancelledAtStage: null,
    cancel: idleCancelRequest,
  }
}

/**
 * Clear a pending cancel request once the job has stopped. A terminal job
 * is the answer to the request, whatever the outcome — the wait is over, so
 * neither the "cancelling" affordance nor a stale failure should survive it.
 */
function settleCancel(state: JobStateValue): JobStateValue {
  if (state.cancel.status === 'idle' || state.job === null) {
    return state
  }
  return isTerminalJobState(state.job.state)
    ? { ...state, cancel: idleCancelRequest }
    : state
}

function applyEvent(
  state: JobStateValue,
  event: WebSocketEvent,
): JobStateValue {
  // Every event applies only to the job already being tracked. The hub
  // broadcasts to *every* connected client, so a `job_created` event is not
  // evidence that this client started the job: adopting it would let a
  // second tab (or another machine against the same backend) silently take
  // over the first one's job. A job this client creates is tracked from the
  // `POST /jobs` response instead, which is authoritative about ownership.
  if (state.job === null) {
    return state
  }
  if (event.job_id !== state.job.id) {
    return state
  }
  const job = state.job

  switch (event.type) {
    case 'job_created':
      return trackJob(state, event.job)
    case 'job_started':
      return { ...state, job: { ...job, started_at: event.started_at } }
    case 'job_stage_changed':
      return { ...state, job: { ...job, state: event.stage } }
    case 'job_progress':
      return {
        ...state,
        job: { ...job, state: event.stage, progress: event.progress },
        progress: {
          progress: event.progress,
          chunksCompleted: event.chunks_completed,
          chunksTotal: event.chunks_total,
          elapsedSeconds: event.elapsed_seconds,
          audioProcessedSeconds: event.audio_processed_seconds,
          audioTotalSeconds: event.audio_total_seconds,
        },
      }
    case 'runtime_metrics':
      return { ...state, metrics: event }
    case 'job_completed':
      return {
        ...state,
        job: {
          ...job,
          state: 'completed',
          progress: 1,
          result: event.result,
          error: null,
        },
      }
    case 'job_cancelled':
      return {
        ...state,
        job: { ...job, state: 'cancelled' },
        cancelledAtStage: event.stage_at_cancellation,
      }
    case 'job_failed':
      return { ...state, job: { ...job, state: 'failed', error: event.error } }
  }
}

/** Pure reducer over {@link JobStateValue}; exported for direct unit testing. */
export function jobReducer(
  state: JobStateValue,
  action: JobAction,
): JobStateValue {
  switch (action.type) {
    case 'job/track':
      return settleCancel(trackJob(state, action.job))
    case 'job/clear':
      return { ...initialJobState, connection: state.connection }
    case 'ws/event':
      return settleCancel(applyEvent(state, action.event))
    case 'ws/status':
      return state.connection === action.status
        ? state
        : { ...state, connection: action.status }
    case 'cancel/requested':
      return state.job === null || isTerminalJobState(state.job.state)
        ? state
        : { ...state, cancel: { status: 'requesting' } }
    case 'cancel/failed':
      return {
        ...state,
        cancel: {
          status: 'error',
          code: action.code,
          message: action.message,
        },
      }
  }
}

const JobStateContext = createContext<JobStateValue | undefined>(undefined)
const JobDispatchContext = createContext<Dispatch<JobAction> | undefined>(
  undefined,
)

/** Props for {@link JobStateProvider}. */
export interface JobStateProviderProps {
  children: ReactNode
  /** Override the initial state (useful in tests). */
  initialState?: JobStateValue
}

/**
 * Provides job state and dispatch to the component tree. The provider does
 * not open the WebSocket itself: call `useJobEvents()` (see
 * `src/ws/useJobEvents.ts`) from the component that needs live events.
 */
export function JobStateProvider({
  children,
  initialState = initialJobState,
}: JobStateProviderProps) {
  const [state, dispatch] = useReducer(jobReducer, initialState)
  return (
    <JobStateContext.Provider value={state}>
      <JobDispatchContext.Provider value={dispatch}>
        {children}
      </JobDispatchContext.Provider>
    </JobStateContext.Provider>
  )
}

/** Read the tracked-job state. Must be used under {@link JobStateProvider}. */
export function useJobState(): JobStateValue {
  const state = useContext(JobStateContext)
  if (state === undefined) {
    throw new Error('useJobState must be used within a JobStateProvider')
  }
  return state
}

/** Get the dispatch function for {@link JobAction}s. Must be used under {@link JobStateProvider}. */
export function useJobDispatch(): Dispatch<JobAction> {
  const dispatch = useContext(JobDispatchContext)
  if (dispatch === undefined) {
    throw new Error('useJobDispatch must be used within a JobStateProvider')
  }
  return dispatch
}
