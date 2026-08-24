import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import {
  appReducer,
  initialAppState,
  initialConfigureState,
  AppStateProvider,
  useAppState,
  useAppDispatch,
  WORKFLOW_PHASES,
  type AppState,
  type WorkflowPhase,
} from './appState'
import { sampleAudioFile, sampleSeparationModes } from '../test/fixtures'

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

describe('appReducer configure slice', () => {
  /** Application state with the mode catalog loaded (and preselected). */
  function withModesLoaded(
    modes: typeof sampleSeparationModes = sampleSeparationModes,
  ): AppState {
    return appReducer(
      appReducer(initialAppState, { type: 'configure/modesRequested' }),
      { type: 'configure/modesLoaded', modes },
    )
  }

  const [twoStemMode, fourStemMode] = sampleSeparationModes

  it('starts idle with nothing loaded and nothing selected', () => {
    expect(initialAppState.configure).toEqual(initialConfigureState)
    expect(initialAppState.configure.modes).toEqual({ status: 'idle' })
    expect(initialAppState.configure.modeId).toBeNull()
    expect(initialAppState.configure.qualityId).toBeNull()
    expect(initialAppState.configure.create).toEqual({ status: 'idle' })
  })

  it('configure/modesRequested enters loading', () => {
    const state = appReducer(initialAppState, {
      type: 'configure/modesRequested',
    })
    expect(state.configure.modes).toEqual({ status: 'loading' })
  })

  it('configure/modesLoaded preselects the first mode and its first tier', () => {
    const state = withModesLoaded()
    expect(state.configure.modes).toEqual({
      status: 'loaded',
      modes: sampleSeparationModes,
    })
    expect(state.configure.modeId).toBe(twoStemMode?.id)
    expect(state.configure.qualityId).toBe(twoStemMode?.quality_options[0]?.id)
  })

  it('configure/modesLoaded selects nothing when the catalog is empty', () => {
    const state = withModesLoaded([])
    expect(state.configure.modeId).toBeNull()
    expect(state.configure.qualityId).toBeNull()
  })

  it('configure/modesFailed records the envelope code and message', () => {
    const state = appReducer(withModesLoaded(), {
      type: 'configure/modesFailed',
      code: 'unknown_error',
      message: 'The separation modes could not be loaded.',
    })
    expect(state.configure.modes).toEqual({
      status: 'error',
      code: 'unknown_error',
      message: 'The separation modes could not be loaded.',
    })
    expect(state.configure.modeId).toBeNull()
    expect(state.configure.qualityId).toBeNull()
  })

  it('configure/modeSelected resets the quality to that mode first option', () => {
    let state = withModesLoaded()
    // Move off the preselected tier first, so the reset is observable.
    state = appReducer(state, {
      type: 'configure/qualitySelected',
      qualityId: twoStemMode?.quality_options[1]?.id ?? '',
    })
    expect(state.configure.qualityId).toBe(twoStemMode?.quality_options[1]?.id)

    state = appReducer(state, {
      type: 'configure/modeSelected',
      modeId: fourStemMode?.id ?? '',
    })
    expect(state.configure.modeId).toBe(fourStemMode?.id)
    expect(state.configure.qualityId).toBe(fourStemMode?.quality_options[0]?.id)
  })

  it('never keeps a quality tier belonging to another mode', () => {
    const foreignTier = twoStemMode?.quality_options[1]?.id ?? ''
    expect(
      fourStemMode?.quality_options.some((option) => option.id === foreignTier),
    ).toBe(false)

    let state = appReducer(withModesLoaded(), {
      type: 'configure/modeSelected',
      modeId: fourStemMode?.id ?? '',
    })
    state = appReducer(state, {
      type: 'configure/qualitySelected',
      qualityId: foreignTier,
    })
    expect(state.configure.qualityId).toBe(fourStemMode?.quality_options[0]?.id)
  })

  it('ignores selections before the catalog loads, and unknown IDs', () => {
    expect(
      appReducer(initialAppState, {
        type: 'configure/modeSelected',
        modeId: twoStemMode?.id ?? '',
      }),
    ).toBe(initialAppState)

    const loaded = withModesLoaded()
    expect(
      appReducer(loaded, {
        type: 'configure/modeSelected',
        modeId: 'not-a-mode',
      }),
    ).toBe(loaded)
    expect(
      appReducer(loaded, {
        type: 'configure/qualitySelected',
        qualityId: 'not-a-tier',
      }),
    ).toBe(loaded)
  })

  it('upload/reset clears the whole configure slice', () => {
    let state = appReducer(withModesLoaded(), {
      type: 'configure/createFailed',
      code: 'audio_not_found',
      message: 'No such audio file.',
    })
    state = { ...state, phase: 'configure' }

    state = appReducer(state, { type: 'upload/reset' })
    expect(state.configure).toEqual(initialConfigureState)
    expect(state.phase).toBe('select')
  })
})

describe('appReducer create-job request state', () => {
  const started = appReducer(initialAppState, {
    type: 'configure/createStarted',
  })

  it('configure/createStarted marks a request in flight', () => {
    expect(started.configure.create).toEqual({ status: 'creating' })
    expect(started.phase).toBe('select')
  })

  it('configure/createSucceeded clears the request and advances to separate', () => {
    const state = appReducer(started, { type: 'configure/createSucceeded' })
    expect(state.configure.create).toEqual({ status: 'idle' })
    expect(state.phase).toBe<WorkflowPhase>('separate')
  })

  it('configure/createFailed records the envelope and keeps the selection', () => {
    const loaded = appReducer(
      appReducer(initialAppState, { type: 'configure/modesRequested' }),
      { type: 'configure/modesLoaded', modes: sampleSeparationModes },
    )
    const state = appReducer(
      appReducer(loaded, { type: 'configure/createStarted' }),
      {
        type: 'configure/createFailed',
        code: 'separator_unavailable',
        message: 'No separator implementation exists for this model.',
      },
    )
    expect(state.configure.create).toEqual({
      status: 'error',
      code: 'separator_unavailable',
      message: 'No separator implementation exists for this model.',
    })
    expect(state.configure.modeId).toBe(sampleSeparationModes[0]?.id)
    expect(state.phase).toBe('select')
  })

  it('a retry after a failure returns to creating', () => {
    const failed = appReducer(started, {
      type: 'configure/createFailed',
      code: 'service_unavailable',
      message: 'The server is shutting down.',
    })
    const retried = appReducer(failed, { type: 'configure/createStarted' })
    expect(retried.configure.create).toEqual({ status: 'creating' })
  })
})

describe('appReducer results slice', () => {
  /** The workflow as it stands with a job running on an uploaded file. */
  const separating: AppState = {
    ...initialAppState,
    phase: 'separate',
    upload: { status: 'uploaded', file: sampleAudioFile },
  }

  it('results/inspect advances from separate to inspect', () => {
    const state = appReducer(separating, { type: 'results/inspect' })
    expect(state.phase).toBe<WorkflowPhase>('inspect')
    expect(state.upload).toBe(separating.upload)
  })

  it('results/inspect is idempotent', () => {
    const once = appReducer(separating, { type: 'results/inspect' })
    expect(appReducer(once, { type: 'results/inspect' }).phase).toBe('inspect')
  })

  it.each<WorkflowPhase>(['select', 'configure', 'export'])(
    'results/inspect is ignored from the %s phase',
    (phase) => {
      const state = { ...separating, phase }
      expect(appReducer(state, { type: 'results/inspect' })).toBe(state)
    },
  )

  it('results/startAnother returns to configure with the file intact', () => {
    const inspecting = appReducer(separating, { type: 'results/inspect' })
    const state = appReducer(inspecting, { type: 'results/startAnother' })
    expect(state.phase).toBe<WorkflowPhase>('configure')
    expect(state.upload).toEqual({
      status: 'uploaded',
      file: sampleAudioFile,
    })
  })

  it('results/startAnother keeps the loaded catalog and selection', () => {
    const loaded = appReducer(
      { ...separating, phase: 'configure' },
      { type: 'configure/modesLoaded', modes: sampleSeparationModes },
    )
    const state = appReducer(
      { ...loaded, phase: 'separate' },
      { type: 'results/startAnother' },
    )
    expect(state.configure.modes).toEqual({
      status: 'loaded',
      modes: sampleSeparationModes,
    })
    expect(state.configure.modeId).toBe(sampleSeparationModes[0]?.id)
  })

  it('results/startAnother clears a stale create failure', () => {
    const failed = appReducer(separating, {
      type: 'configure/createFailed',
      code: 'service_unavailable',
      message: 'The server is shutting down.',
    })
    const state = appReducer(failed, { type: 'results/startAnother' })
    expect(state.configure.create).toEqual({ status: 'idle' })
  })

  it('results/startAnother is ignored when no file is uploaded', () => {
    const state = { ...initialAppState, phase: 'separate' as const }
    expect(appReducer(state, { type: 'results/startAnother' })).toBe(state)
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
