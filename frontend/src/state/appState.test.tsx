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
import { sampleAudioFile } from '../test/fixtures'

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

describe('appReducer upload slice', () => {
  it('starts idle', () => {
    expect(initialAppState.upload).toEqual({ status: 'idle' })
  })

  it('upload/started enters uploading with unknown progress', () => {
    const state = appReducer(initialAppState, { type: 'upload/started' })
    expect(state.upload).toEqual({ status: 'uploading', fraction: null })
    expect(state.phase).toBe('select')
  })

  it('upload/progress updates the fraction while uploading', () => {
    let state = appReducer(initialAppState, { type: 'upload/started' })
    state = appReducer(state, { type: 'upload/progress', fraction: 0.42 })
    expect(state.upload).toEqual({ status: 'uploading', fraction: 0.42 })

    state = appReducer(state, { type: 'upload/progress', fraction: null })
    expect(state.upload).toEqual({ status: 'uploading', fraction: null })
  })

  it('upload/progress is ignored when no upload is in flight', () => {
    const state = appReducer(initialAppState, {
      type: 'upload/progress',
      fraction: 0.5,
    })
    expect(state).toBe(initialAppState)
  })

  it('upload/succeeded stores the AudioFile and advances select → configure', () => {
    let state = appReducer(initialAppState, { type: 'upload/started' })
    state = appReducer(state, {
      type: 'upload/succeeded',
      file: sampleAudioFile,
    })
    expect(state.upload).toEqual({ status: 'uploaded', file: sampleAudioFile })
    expect(state.phase).toBe('configure')
  })

  it('upload/succeeded does not regress a later phase', () => {
    let state: AppState = {
      ...initialAppState,
      phase: 'separate',
      upload: { status: 'uploading', fraction: 0.9 },
    }
    state = appReducer(state, {
      type: 'upload/succeeded',
      file: sampleAudioFile,
    })
    expect(state.phase).toBe('separate')
  })

  it('upload/failed records the envelope code and message', () => {
    let state = appReducer(initialAppState, { type: 'upload/started' })
    state = appReducer(state, {
      type: 'upload/failed',
      code: 'audio_too_large',
      message: 'The uploaded file exceeds the maximum allowed size.',
    })
    expect(state.upload).toEqual({
      status: 'error',
      code: 'audio_too_large',
      message: 'The uploaded file exceeds the maximum allowed size.',
    })
    expect(state.phase).toBe('select')
  })

  it('upload/reset returns to idle', () => {
    let state = appReducer(initialAppState, {
      type: 'upload/failed',
      code: 'audio_not_decodable',
      message: 'The uploaded file could not be decoded as audio.',
    })
    state = appReducer(state, { type: 'upload/reset' })
    expect(state.upload).toEqual({ status: 'idle' })
  })

  it('upload/reset discards an uploaded file and returns to select', () => {
    let state = appReducer(initialAppState, {
      type: 'upload/succeeded',
      file: sampleAudioFile,
    })
    expect(state.phase).toBe('configure')

    state = appReducer(state, { type: 'upload/reset' })
    expect(state.upload).toEqual({ status: 'idle' })
    expect(state.phase).toBe('select')
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
