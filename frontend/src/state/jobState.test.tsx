import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  JobStateProvider,
  TERMINAL_JOB_STATES,
  initialJobState,
  isTerminalJobState,
  jobReducer,
  useJobDispatch,
  useJobState,
  type JobStateValue,
} from './jobState'
import type { ErrorInfo, WebSocketEvent } from '../api/types'
import {
  sampleJob,
  sampleJobId,
  sampleResult,
  sampleRuntimeMetrics,
} from '../test/fixtures'

const otherJobId = '01OTHERJOBULID000000000000'

function tracked(): JobStateValue {
  return jobReducer(initialJobState, { type: 'job/track', job: sampleJob })
}

function apply(state: JobStateValue, event: WebSocketEvent): JobStateValue {
  return jobReducer(state, { type: 'ws/event', event })
}

describe('initial job state', () => {
  it('tracks nothing and reports a closed socket', () => {
    expect(initialJobState).toEqual({
      job: null,
      progress: null,
      metrics: null,
      cancelledAtStage: null,
      connection: 'closed',
      cancel: { status: 'idle' },
    })
  })
})

describe('isTerminalJobState', () => {
  it('recognizes exactly the terminal states', () => {
    expect(TERMINAL_JOB_STATES).toEqual(['completed', 'cancelled', 'failed'])
    for (const state of TERMINAL_JOB_STATES) {
      expect(isTerminalJobState(state)).toBe(true)
    }
    expect(isTerminalJobState('queued')).toBe(false)
    expect(isTerminalJobState('separating')).toBe(false)
  })
})

describe('jobReducer job/track', () => {
  it('tracks a job from a REST response', () => {
    const state = tracked()
    expect(state.job).toEqual(sampleJob)
    expect(state.progress).toBeNull()
  })

  it('resets progress, metrics, and cancellation stage for a different job', () => {
    let state = tracked()
    state = apply(state, {
      type: 'job_progress',
      job_id: sampleJobId,
      stage: 'separating',
      progress: 0.5,
      chunks_completed: 24,
      chunks_total: 48,
      elapsed_seconds: 12,
      audio_processed_seconds: 100,
      audio_total_seconds: 227.4,
    })
    state = apply(state, sampleRuntimeMetrics)
    expect(state.progress).not.toBeNull()

    const next = jobReducer(state, {
      type: 'job/track',
      job: { ...sampleJob, id: otherJobId },
    })
    expect(next.job?.id).toBe(otherJobId)
    expect(next.progress).toBeNull()
    expect(next.metrics).toBeNull()
  })

  it('keeps live progress when re-tracking the same job', () => {
    let state = tracked()
    state = apply(state, sampleRuntimeMetrics)

    const next = jobReducer(state, {
      type: 'job/track',
      job: { ...sampleJob, state: 'separating' },
    })
    expect(next.job?.state).toBe('separating')
    expect(next.metrics).toEqual(sampleRuntimeMetrics)
  })

  it('refuses to demote a terminal job with a stale REST snapshot', () => {
    // `getJob` on reconnect and `cancelJob` both answer with the job as it
    // was when the handler ran, which can be older than an event already
    // applied. The job has genuinely stopped, so no further event would
    // arrive to undo the damage.
    const completed = apply(tracked(), {
      type: 'job_completed',
      job_id: sampleJobId,
      result: sampleResult,
    })

    const stale = jobReducer(completed, {
      type: 'job/track',
      job: { ...sampleJob, state: 'separating', progress: 0.65 },
    })
    expect(stale).toBe(completed)
    expect(stale.job?.state).toBe('completed')
    expect(stale.job?.result).toEqual(sampleResult)
  })

  it('refuses to demote a cancelled job with a stale cancel response', () => {
    const cancelled = apply(tracked(), {
      type: 'job_cancelled',
      job_id: sampleJobId,
      stage_at_cancellation: 'separating',
    })

    const stale = jobReducer(cancelled, {
      type: 'job/track',
      job: { ...sampleJob, state: 'separating' },
    })
    expect(stale.job?.state).toBe('cancelled')
    expect(stale.cancelledAtStage).toBe('separating')
  })

  it('still accepts a snapshot that carries the job forward', () => {
    const state = jobReducer(tracked(), {
      type: 'job/track',
      job: { ...sampleJob, state: 'completed', result: sampleResult },
    })
    expect(state.job?.state).toBe('completed')
  })

  it('the demotion guard is per job, not global', () => {
    const completed = apply(tracked(), {
      type: 'job_completed',
      job_id: sampleJobId,
      result: sampleResult,
    })
    const next = jobReducer(completed, {
      type: 'job/track',
      job: { ...sampleJob, id: otherJobId, state: 'queued' },
    })
    expect(next.job?.id).toBe(otherJobId)
    expect(next.job?.state).toBe('queued')
  })

  it('job/clear returns to the initial state but keeps the connection status', () => {
    const state = jobReducer(
      { ...tracked(), connection: 'open' },
      { type: 'job/clear' },
    )
    expect(state.job).toBeNull()
    expect(state.connection).toBe('open')
  })
})

describe('jobReducer WebSocket events', () => {
  it('never adopts a job_created event: the hub broadcasts to every client', () => {
    // A second tab (or another machine against the same backend) would
    // otherwise silently take over a job this client never started.
    expect(
      apply(initialJobState, {
        type: 'job_created',
        job_id: sampleJobId,
        job: sampleJob,
      }),
    ).toBe(initialJobState)
  })

  it('refreshes the record from a job_created event for the tracked job', () => {
    const state = apply(tracked(), {
      type: 'job_created',
      job_id: sampleJobId,
      job: { ...sampleJob, model_id: 'resolved-later' },
    })
    expect(state.job?.model_id).toBe('resolved-later')
  })

  it('records the start timestamp from job_started', () => {
    const state = apply(tracked(), {
      type: 'job_started',
      job_id: sampleJobId,
      started_at: '2026-08-23T12:00:05Z',
    })
    expect(state.job?.started_at).toBe('2026-08-23T12:00:05Z')
  })

  it('advances the state on job_stage_changed', () => {
    const state = apply(tracked(), {
      type: 'job_stage_changed',
      job_id: sampleJobId,
      stage: 'separating',
      previous_stage: 'loading_model',
    })
    expect(state.job?.state).toBe('separating')
  })

  it('stores chunk-grained detail from job_progress', () => {
    const state = apply(tracked(), {
      type: 'job_progress',
      job_id: sampleJobId,
      stage: 'separating',
      progress: 0.65,
      chunks_completed: 31,
      chunks_total: 48,
      elapsed_seconds: 18.2,
      audio_processed_seconds: 148.0,
      audio_total_seconds: 227.4,
    })
    expect(state.job?.state).toBe('separating')
    expect(state.job?.progress).toBeCloseTo(0.65)
    expect(state.progress).toEqual({
      progress: 0.65,
      chunksCompleted: 31,
      chunksTotal: 48,
      elapsedSeconds: 18.2,
      audioProcessedSeconds: 148.0,
      audioTotalSeconds: 227.4,
    })
  })

  it('stores the newest runtime_metrics payload verbatim', () => {
    let state = apply(tracked(), sampleRuntimeMetrics)
    expect(state.metrics).toEqual(sampleRuntimeMetrics)

    const newer = {
      ...sampleRuntimeMetrics,
      gpu: null,
    } satisfies WebSocketEvent
    state = apply(state, newer)
    expect(state.metrics).toEqual(newer)
    expect(state.metrics?.gpu).toBeNull()
  })

  it('job_completed is terminal and carries the result', () => {
    const state = apply(tracked(), {
      type: 'job_completed',
      job_id: sampleJobId,
      result: sampleResult,
    })
    expect(state.job?.state).toBe('completed')
    expect(state.job?.progress).toBe(1)
    expect(state.job?.result).toEqual(sampleResult)
    expect(isTerminalJobState(state.job?.state ?? 'queued')).toBe(true)
  })

  it('job_cancelled is terminal and records the stage at cancellation', () => {
    const state = apply(tracked(), {
      type: 'job_cancelled',
      job_id: sampleJobId,
      stage_at_cancellation: 'separating',
    })
    expect(state.job?.state).toBe('cancelled')
    expect(state.cancelledAtStage).toBe('separating')
  })

  it('job_failed is terminal and carries the error envelope', () => {
    const error: ErrorInfo = {
      code: 'cuda_out_of_memory',
      message: 'The GPU ran out of memory.',
      detail: {},
    }
    const state = apply(tracked(), {
      type: 'job_failed',
      job_id: sampleJobId,
      error,
    })
    expect(state.job?.state).toBe('failed')
    expect(state.job?.error).toEqual(error)
  })

  it('ignores events for a different job', () => {
    const state = tracked()
    const events: WebSocketEvent[] = [
      { type: 'job_started', job_id: otherJobId, started_at: '2026-01-01Z' },
      {
        type: 'job_stage_changed',
        job_id: otherJobId,
        stage: 'separating',
        previous_stage: 'decoding',
      },
      {
        type: 'job_progress',
        job_id: otherJobId,
        stage: 'separating',
        progress: 0.9,
        chunks_completed: 43,
        chunks_total: 48,
        elapsed_seconds: 30,
        audio_processed_seconds: 200,
        audio_total_seconds: 227.4,
      },
      { ...sampleRuntimeMetrics, job_id: otherJobId },
      {
        type: 'job_completed',
        job_id: otherJobId,
        result: { ...sampleResult, job_id: otherJobId },
      },
      {
        type: 'job_cancelled',
        job_id: otherJobId,
        stage_at_cancellation: 'separating',
      },
      {
        type: 'job_failed',
        job_id: otherJobId,
        error: { code: 'boom', message: 'boom' },
      },
      {
        type: 'job_created',
        job_id: otherJobId,
        job: { ...sampleJob, id: otherJobId },
      },
    ]

    for (const event of events) {
      expect(apply(state, event)).toBe(state)
    }
  })

  it('ignores a stale event that arrives after the tracked job changed', () => {
    let state = tracked()
    state = jobReducer(state, {
      type: 'job/track',
      job: { ...sampleJob, id: otherJobId },
    })

    const stale = apply(state, {
      type: 'job_progress',
      job_id: sampleJobId,
      stage: 'separating',
      progress: 0.65,
      chunks_completed: 31,
      chunks_total: 48,
      elapsed_seconds: 18.2,
      audio_processed_seconds: 148.0,
      audio_total_seconds: 227.4,
    })
    expect(stale).toBe(state)
    expect(stale.progress).toBeNull()
  })

  it('ignores every event while nothing is tracked', () => {
    expect(
      apply(initialJobState, {
        type: 'job_started',
        job_id: sampleJobId,
        started_at: '2026-08-23T12:00:05Z',
      }),
    ).toBe(initialJobState)
  })
})

describe('jobReducer cancel slice', () => {
  function requesting(): JobStateValue {
    return jobReducer(tracked(), { type: 'cancel/requested' })
  }

  it('records a requested cancellation', () => {
    expect(requesting().cancel).toEqual({ status: 'requesting' })
  })

  it('ignores a request while nothing is tracked', () => {
    expect(jobReducer(initialJobState, { type: 'cancel/requested' })).toBe(
      initialJobState,
    )
  })

  it('ignores a request for an already terminal job', () => {
    const state = jobReducer(initialJobState, {
      type: 'job/track',
      job: { ...sampleJob, state: 'completed' },
    })
    expect(jobReducer(state, { type: 'cancel/requested' })).toBe(state)
  })

  it('records a failed cancel request with its envelope', () => {
    const state = jobReducer(requesting(), {
      type: 'cancel/failed',
      code: 'job_not_found',
      message: 'No such job.',
    })
    expect(state.cancel).toEqual({
      status: 'error',
      code: 'job_not_found',
      message: 'No such job.',
    })
  })

  it('keeps requesting while the cancel response is still a processing state', () => {
    const state = jobReducer(requesting(), {
      type: 'job/track',
      job: { ...sampleJob, state: 'separating' },
    })
    expect(state.cancel).toEqual({ status: 'requesting' })
  })

  it('settles on the authoritative job_cancelled event', () => {
    const state = apply(requesting(), {
      type: 'job_cancelled',
      job_id: sampleJobId,
      stage_at_cancellation: 'separating',
    })
    expect(state.cancel).toEqual({ status: 'idle' })
    expect(state.cancelledAtStage).toBe('separating')
  })

  it('settles on any terminal transition, including a race with completion', () => {
    const completed = apply(requesting(), {
      type: 'job_completed',
      job_id: sampleJobId,
      result: sampleResult,
    })
    expect(completed.cancel).toEqual({ status: 'idle' })

    const failed = apply(requesting(), {
      type: 'job_failed',
      job_id: sampleJobId,
      error: { code: 'cuda_out_of_memory', message: 'Out of memory.' },
    })
    expect(failed.cancel).toEqual({ status: 'idle' })
  })

  it('clears a cancel failure once the job reaches a terminal state', () => {
    const errored = jobReducer(requesting(), {
      type: 'cancel/failed',
      code: 'service_unavailable',
      message: 'Shutting down.',
    })
    const state = apply(errored, {
      type: 'job_failed',
      job_id: sampleJobId,
      error: { code: 'cuda_out_of_memory', message: 'Out of memory.' },
    })
    expect(state.cancel).toEqual({ status: 'idle' })
  })

  it('resets when a different job is tracked', () => {
    const state = jobReducer(requesting(), {
      type: 'job/track',
      job: { ...sampleJob, id: otherJobId },
    })
    expect(state.cancel).toEqual({ status: 'idle' })
  })

  it('is cleared by job/clear', () => {
    expect(jobReducer(requesting(), { type: 'job/clear' }).cancel).toEqual({
      status: 'idle',
    })
  })
})

describe('jobReducer ws/status', () => {
  it('records the connection status', () => {
    const state = jobReducer(initialJobState, {
      type: 'ws/status',
      status: 'reconnecting',
    })
    expect(state.connection).toBe('reconnecting')
  })

  it('returns the same state when the status is unchanged', () => {
    expect(
      jobReducer(initialJobState, { type: 'ws/status', status: 'closed' }),
    ).toBe(initialJobState)
  })
})

describe('JobStateProvider', () => {
  function wrapper({ children }: { children: ReactNode }) {
    return <JobStateProvider>{children}</JobStateProvider>
  }

  it('exposes state and dispatch to the tree', () => {
    const { result } = renderHook(
      () => ({ state: useJobState(), dispatch: useJobDispatch() }),
      { wrapper },
    )

    expect(result.current.state).toEqual(initialJobState)

    act(() => {
      result.current.dispatch({ type: 'job/track', job: sampleJob })
    })
    expect(result.current.state.job).toEqual(sampleJob)
  })

  it('accepts an initial state override', () => {
    const { result } = renderHook(() => useJobState(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <JobStateProvider
          initialState={{ ...initialJobState, connection: 'open' }}
        >
          {children}
        </JobStateProvider>
      ),
    })

    expect(result.current.connection).toBe('open')
  })

  it('throws when used outside the provider', () => {
    expect(() => renderHook(() => useJobState())).toThrow(
      /within a JobStateProvider/,
    )
    expect(() => renderHook(() => useJobDispatch())).toThrow(
      /within a JobStateProvider/,
    )
  })
})
