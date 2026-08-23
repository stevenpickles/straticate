/**
 * Global application state for the Straticate workflow, implemented
 * with React context + reducer (no external state library).
 */
import {
  createContext,
  useContext,
  useReducer,
  type Dispatch,
  type ReactNode,
} from 'react'
import type { AudioFile } from '../api/types'

/** The phases of the product workflow, in order. */
export type WorkflowPhase =
  'select' | 'configure' | 'separate' | 'inspect' | 'export'

/** All workflow phases in their canonical order. */
export const WORKFLOW_PHASES: readonly WorkflowPhase[] = [
  'select',
  'configure',
  'separate',
  'inspect',
  'export',
]

/**
 * State of the audio upload step, discriminated on `status`:
 * `idle` (nothing in flight), `uploading` (with a 0..1 progress fraction,
 * or `null` when the total length is not computable), `uploaded` (the
 * backend-registered {@link AudioFile}), or `error` (envelope code +
 * human-readable message).
 */
export type UploadState =
  | { readonly status: 'idle' }
  | { readonly status: 'uploading'; readonly fraction: number | null }
  | { readonly status: 'uploaded'; readonly file: AudioFile }
  | {
      readonly status: 'error'
      readonly code: string
      readonly message: string
    }

/** Shape of the global application state. */
export interface AppState {
  /** The workflow phase currently shown in the workspace. */
  readonly phase: WorkflowPhase
  /** State of the audio upload step. */
  readonly upload: UploadState
}

/** Actions accepted by {@link appReducer}. */
export type AppAction =
  | {
      readonly type: 'phase/set'
      readonly phase: WorkflowPhase
    }
  | {
      /** An upload has started; progress is not yet known. */
      readonly type: 'upload/started'
    }
  | {
      /** Upload progress changed: a 0..1 fraction, or `null` when indeterminate. */
      readonly type: 'upload/progress'
      readonly fraction: number | null
    }
  | {
      /**
       * The backend accepted the file. Stores the {@link AudioFile} and
       * advances the workflow from `select` to `configure`.
       */
      readonly type: 'upload/succeeded'
      readonly file: AudioFile
    }
  | {
      /** The upload failed with a backend envelope code and message. */
      readonly type: 'upload/failed'
      readonly code: string
      readonly message: string
    }
  | {
      /**
       * Clear the upload — dismissing an error, an abort, or discarding an
       * already-uploaded file — returning the drop zone to idle and the
       * workflow to the `select` phase.
       */
      readonly type: 'upload/reset'
    }

/** Initial application state: the workflow starts at file selection. */
export const initialAppState: AppState = {
  phase: 'select',
  upload: { status: 'idle' },
}

/** Pure reducer over {@link AppState}; exported for direct unit testing. */
export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'phase/set':
      return { ...state, phase: action.phase }
    case 'upload/started':
      return { ...state, upload: { status: 'uploading', fraction: null } }
    case 'upload/progress':
      if (state.upload.status !== 'uploading') {
        return state
      }
      return {
        ...state,
        upload: { status: 'uploading', fraction: action.fraction },
      }
    case 'upload/succeeded':
      return {
        ...state,
        upload: { status: 'uploaded', file: action.file },
        phase: state.phase === 'select' ? 'configure' : state.phase,
      }
    case 'upload/failed':
      return {
        ...state,
        upload: { status: 'error', code: action.code, message: action.message },
      }
    case 'upload/reset':
      return { ...state, upload: { status: 'idle' }, phase: 'select' }
  }
}

const AppStateContext = createContext<AppState | undefined>(undefined)
const AppDispatchContext = createContext<Dispatch<AppAction> | undefined>(
  undefined,
)

/** Props for {@link AppStateProvider}. */
export interface AppStateProviderProps {
  children: ReactNode
  /** Override the initial state (useful in tests). */
  initialState?: AppState
}

/** Provides application state and dispatch to the component tree. */
export function AppStateProvider({
  children,
  initialState = initialAppState,
}: AppStateProviderProps) {
  const [state, dispatch] = useReducer(appReducer, initialState)
  return (
    <AppStateContext.Provider value={state}>
      <AppDispatchContext.Provider value={dispatch}>
        {children}
      </AppDispatchContext.Provider>
    </AppStateContext.Provider>
  )
}

/** Read the current application state. Must be used under {@link AppStateProvider}. */
export function useAppState(): AppState {
  const state = useContext(AppStateContext)
  if (state === undefined) {
    throw new Error('useAppState must be used within an AppStateProvider')
  }
  return state
}

/** Get the dispatch function for {@link AppAction}s. Must be used under {@link AppStateProvider}. */
export function useAppDispatch(): Dispatch<AppAction> {
  const dispatch = useContext(AppDispatchContext)
  if (dispatch === undefined) {
    throw new Error('useAppDispatch must be used within an AppStateProvider')
  }
  return dispatch
}
