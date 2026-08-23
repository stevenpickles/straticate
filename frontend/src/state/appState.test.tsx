import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  appReducer,
  initialAppState,
  AppStateProvider,
  useAppState,
  useAppDispatch,
  WORKFLOW_PHASES,
  type AppState,
  type WorkflowPhase,
} from './appState'

describe('appReducer', () => {
  it('starts at the select phase', () => {
    expect(initialAppState.phase).toBe<WorkflowPhase>('select')
  })

  it('transitions through every workflow phase', () => {
    let state: AppState = initialAppState
    for (const phase of WORKFLOW_PHASES) {
      state = appReducer(state, { type: 'phase/set', phase })
      expect(state.phase).toBe(phase)
    }
  })

  it('returns a new state object on transition', () => {
    const next = appReducer(initialAppState, {
      type: 'phase/set',
      phase: 'configure',
    })
    expect(next).not.toBe(initialAppState)
    expect(initialAppState.phase).toBe('select')
  })
})

describe('useAppState / useAppDispatch', () => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <AppStateProvider>{children}</AppStateProvider>
  )

  it('provides state and dispatch through context', () => {
    const { result } = renderHook(
      () => ({ state: useAppState(), dispatch: useAppDispatch() }),
      { wrapper },
    )
    expect(result.current.state.phase).toBe('select')

    act(() => {
      result.current.dispatch({ type: 'phase/set', phase: 'separate' })
    })
    expect(result.current.state.phase).toBe('separate')
  })

  it('throws when used outside the provider', () => {
    expect(() => renderHook(() => useAppState())).toThrow(
      /within an AppStateProvider/,
    )
    expect(() => renderHook(() => useAppDispatch())).toThrow(
      /within an AppStateProvider/,
    )
  })
})
