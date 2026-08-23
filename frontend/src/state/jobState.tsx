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
export const TERMINAL_JOB_STATES: readonly JobLifecycleState[] = [
  'completed',
  'cancelled',
  'failed',
]

const terminalJobStates = new Set<JobLifecycleState>(TERMINAL_JOB_STATES)

/** Whether a job lifecycle state is terminal (`completed`/`cancelled`/`failed`). */
export function isTerminalJobState(state: JobLifecycleState): boolean {
  return terminalJobStates.has(state)
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
}

/** Actions accepted by {@link jobReducer}. */
export type JobAction =
  | {
      /**
       * Track a job from an authoritative REST response (create, fetch, or
       * cancel). Tracking a different job resets progress, metrics, and
       * cancellation stage.
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

/** Initial state: no job tracked, socket closed. */
export const initialJobState: JobStateValue = {
  job: null,
  progress: null,
  metrics: null,
  cancelledAtStage: null,
  connection: 'closed',
}

function trackJob(state: JobStateValue, job: Job): JobStateValue {
  if (state.job !== null && state.job.id === job.id) {
    return { ...state, job }
  }
  return {
    ...state,
    job,
    progress: null,
    metrics: null,
    cancelledAtStage: null,
  }
}

function applyEvent(
  state: JobStateValue,
  event: WebSocketEvent,
): JobStateValue {
  // `job_created` may adopt a job when nothing is tracked yet; every other
  // event only applies to the job already being tracked.
  if (state.job === null) {
    return event.type === 'job_created' ? trackJob(state, event.job) : state
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
      return trackJob(state, action.job)
    case 'job/clear':
      return { ...initialJobState, connection: state.connection }
    case 'ws/event':
      return applyEvent(state, action.event)
    case 'ws/status':
      return state.connection === action.status
        ? state
        : { ...state, connection: action.status }
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
