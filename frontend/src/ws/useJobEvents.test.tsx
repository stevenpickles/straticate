import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { JobEventClient } from './client'
import { useJobEvents } from './useJobEvents'
import { JobStateProvider, useJobState } from '../state/jobState'
import { FakeScheduler, FakeWebSocketFactory } from '../test/mockWebSocket'
import { sampleJob, sampleJobId } from '../test/fixtures'

const silentLogger = { warn: vi.fn() }

let sockets: FakeWebSocketFactory
let scheduler: FakeScheduler
let client: JobEventClient

function wrapper({ children }: { children: ReactNode }) {
  return <JobStateProvider>{children}</JobStateProvider>
}

beforeEach(() => {
  sockets = new FakeWebSocketFactory()
  scheduler = new FakeScheduler()
  silentLogger.warn.mockClear()
  client = new JobEventClient({
    createWebSocket: sockets.create,
    schedule: scheduler.schedule,
    random: () => 0,
    logger: silentLogger,
  })
})

describe('useJobEvents', () => {
  it('connects on mount and closes the socket on unmount', () => {
    const { unmount } = renderHook(() => useJobEvents({ client }), { wrapper })

    expect(sockets.sockets).toHaveLength(1)
    expect(client.status).toBe('connecting')

    unmount()
    expect(sockets.last.closed).toBe(true)
    expect(client.status).toBe('closed')
  })

  it('feeds decoded events into the job store', () => {
    const { result } = renderHook(
      () => ({ client: useJobEvents({ client }), state: useJobState() }),
      { wrapper },
    )

    act(() => {
      sockets.last.emitOpen()
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_created',
          job_id: sampleJobId,
          job: sampleJob,
        }),
      )
    })
    expect(result.current.state.job).toEqual(sampleJob)

    act(() => {
      sockets.last.emitMessage(
        JSON.stringify({
          type: 'job_stage_changed',
          job_id: sampleJobId,
          stage: 'separating',
          previous_stage: 'loading_model',
        }),
      )
    })
    expect(result.current.state.job?.state).toBe('separating')
  })

  it('mirrors the connection status into the store', () => {
    const { result } = renderHook(
      () => ({ client: useJobEvents({ client }), state: useJobState() }),
      { wrapper },
    )

    expect(result.current.state.connection).toBe('connecting')

    act(() => {
      sockets.last.emitOpen()
    })
    expect(result.current.state.connection).toBe('open')

    act(() => {
      sockets.last.emitClose()
    })
    expect(result.current.state.connection).toBe('reconnecting')
  })

  it('stops feeding the store after unmount', () => {
    const { result, unmount } = renderHook(
      () => ({ client: useJobEvents({ client }), state: useJobState() }),
      { wrapper },
    )
    const socket = sockets.last

    unmount()
    // The client detaches its handlers on close, so nothing is delivered.
    act(() => {
      socket.emitMessage(
        JSON.stringify({
          type: 'job_created',
          job_id: sampleJobId,
          job: sampleJob,
        }),
      )
    })
    expect(result.current.state.job).toBeNull()
  })

  it('calls onOpen on connect and after every reconnect', () => {
    const onOpen = vi.fn()
    renderHook(() => useJobEvents({ client, onOpen }), { wrapper })

    expect(onOpen).not.toHaveBeenCalled()

    act(() => {
      sockets.last.emitOpen()
    })
    expect(onOpen).toHaveBeenCalledTimes(1)

    act(() => {
      sockets.last.emitClose()
      scheduler.runNext()
      sockets.last.emitOpen()
    })
    expect(onOpen).toHaveBeenCalledTimes(2)
  })

  it('returns the client it drives, creating one when none is given', () => {
    const { result } = renderHook(() => useJobEvents({ client }), { wrapper })
    expect(result.current).toBe(client)
  })

  it('requires a JobStateProvider', () => {
    expect(() => renderHook(() => useJobEvents({ client }))).toThrow(
      /within a JobStateProvider/,
    )
  })
})
