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
import { DEFAULT_STEREO_HANDLING } from '../api/jobs'
import type { AudioFile, SeparationMode, StereoHandling } from '../api/types'

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

/**
 * State of the separation-mode catalog fetch, discriminated on `status`.
 * The catalog is served by `GET /separation-modes`; nothing about modes,
 * stems or quality tiers is known to the client before it loads.
 */
export type ModesState =
  | { readonly status: 'idle' }
  | { readonly status: 'loading' }
  | {
      readonly status: 'loaded'
      readonly modes: readonly SeparationMode[]
    }
  | {
      readonly status: 'error'
      readonly code: string
      readonly message: string
    }

/**
 * State of the `POST /jobs` request that starts a separation,
 * discriminated on `status`: `idle` (nothing in flight), `creating`, or
 * `error` (envelope code + human-readable message, retryable).
 */
export type JobCreateState =
  | { readonly status: 'idle' }
  | { readonly status: 'creating' }
  | {
      readonly status: 'error'
      readonly code: string
      readonly message: string
    }

/**
 * The `configure` phase: which separation mode and quality tier the user
 * picked, plus the catalog fetch and job-creation request states.
 *
 * `modeId`/`qualityId` are IDs served by the backend - they are never
 * compared against, or defaulted to, any literal in client code. They are
 * `null` until the catalog loads (and again after `upload/reset`).
 */
export interface ConfigureState {
  /** The loaded separation-mode catalog. */
  readonly modes: ModesState
  /** ID of the selected separation mode, or `null` when none is selected. */
  readonly modeId: string | null
  /** ID of the selected quality tier within the selected mode. */
  readonly qualityId: string | null
  /**
   * What to do with the input's stereo image before separating (feature 041).
   *
   * Unlike `modeId`/`qualityId` this is **not** a catalog value: it is a
   * statement about the user's own recording, so it is independent of which
   * mode or tier is selected and survives changing either. It resets with the
   * upload, because the next file is a different recording.
   */
  readonly stereoHandling: StereoHandling
  /** State of the create-job request. */
  readonly create: JobCreateState
}

/** Shape of the global application state. */
export interface AppState {
  /** The workflow phase currently shown in the workspace. */
  readonly phase: WorkflowPhase
  /** State of the audio upload step. */
  readonly upload: UploadState
  /** State of the separation mode + quality selection step. */
  readonly configure: ConfigureState
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
  | {
      /** The separation-mode catalog fetch has started. */
      readonly type: 'configure/modesRequested'
    }
  | {
      /**
       * The catalog loaded. Preselects the first mode and, within it, its
       * first quality option (the backend orders them
       * `fast` then `balanced` then `high_quality`).
       */
      readonly type: 'configure/modesLoaded'
      readonly modes: readonly SeparationMode[]
    }
  | {
      /** The catalog fetch failed with a backend envelope code and message. */
      readonly type: 'configure/modesFailed'
      readonly code: string
      readonly message: string
    }
  | {
      /**
       * The user picked a separation mode. Resets the quality selection to
       * that mode's first option, so a `quality_id` from another mode can
       * never survive. Unknown mode IDs are ignored.
       */
      readonly type: 'configure/modeSelected'
      readonly modeId: string
    }
  | {
      /**
       * The user picked a quality tier. Ignored unless the tier is an
       * option of the currently selected mode.
       */
      readonly type: 'configure/qualitySelected'
      readonly qualityId: string
    }
  | {
      /**
       * The user chose how their audio's stereo image should be treated.
       * Independent of the catalog, so it is never validated against it.
       */
      readonly type: 'configure/stereoHandlingSelected'
      readonly stereoHandling: StereoHandling
    }
  | {
      /** A create-job request is in flight (blocks a second submission). */
      readonly type: 'configure/createStarted'
    }
  | {
      /**
       * The backend queued the job. Clears the request state and advances
       * the workflow from `configure` to `separate`.
       */
      readonly type: 'configure/createSucceeded'
    }
  | {
      /** Creating the job failed; the user may retry without changing anything. */
      readonly type: 'configure/createFailed'
      readonly code: string
      readonly message: string
    }
  | {
      /**
       * The user asked to listen to a completed job's stems: advances the
       * workflow from `separate` to `inspect`. Ignored from any other phase
       * (there is nothing to inspect before a job has run).
       */
      readonly type: 'results/inspect'
    }
  | {
      /**
       * The user asked to separate again: returns the workflow to `configure`
       * with the upload and the loaded catalog intact, and clears any stale
       * create-request error. With no uploaded file it falls back to `select`
       * rather than doing nothing — it is the escape hatch out of a finished
       * job, so it must always land somewhere the user can act.
       *
       * The tracked job is a separate store: whoever dispatches this also
       * dispatches `job/clear` (see `state/jobState.tsx`).
       */
      readonly type: 'results/startAnother'
    }

/** Initial state of the configure slice: nothing loaded, nothing selected. */
export const initialConfigureState: ConfigureState = {
  modes: { status: 'idle' },
  modeId: null,
  qualityId: null,
  stereoHandling: DEFAULT_STEREO_HANDLING,
  create: { status: 'idle' },
}

/** Initial application state: the workflow starts at file selection. */
export const initialAppState: AppState = {
  phase: 'select',
  upload: { status: 'idle' },
  configure: initialConfigureState,
}

/** The mode with `modeId` in a loaded catalog, or `undefined`. */
function findMode(
  modes: readonly SeparationMode[],
  modeId: string | null,
): SeparationMode | undefined {
  return modes.find((mode) => mode.id === modeId)
}

/**
 * The ID of a mode's first quality option - the backend already orders them
 * `fast`, `balanced`, `high_quality`, so "first" is that mode's default
 * tier. `null` for a mode with no options (which the backend never serves).
 */
function firstQualityId(mode: SeparationMode | undefined): string | null {
  return mode?.quality_options[0]?.id ?? null
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
      return {
        ...state,
        upload: { status: 'idle' },
        phase: 'select',
        configure: initialConfigureState,
      }
    case 'configure/modesRequested':
      return {
        ...state,
        configure: { ...state.configure, modes: { status: 'loading' } },
      }
    case 'configure/modesLoaded': {
      const mode = action.modes[0]
      return {
        ...state,
        configure: {
          modes: { status: 'loaded', modes: action.modes },
          modeId: mode?.id ?? null,
          qualityId: firstQualityId(mode),
          // Carried across, not reset: the catalog has nothing to say about
          // the user's own recording, and a retry of the catalog fetch must
          // not silently undo a choice they already made.
          stereoHandling: state.configure.stereoHandling,
          create: { status: 'idle' },
        },
      }
    }
    case 'configure/modesFailed':
      return {
        ...state,
        configure: {
          ...initialConfigureState,
          modes: {
            status: 'error',
            code: action.code,
            message: action.message,
          },
        },
      }
    case 'configure/modeSelected': {
      const { modes } = state.configure
      if (modes.status !== 'loaded') {
        return state
      }
      const mode = findMode(modes.modes, action.modeId)
      if (mode === undefined) {
        return state
      }
      return {
        ...state,
        configure: {
          ...state.configure,
          modeId: mode.id,
          qualityId: firstQualityId(mode),
        },
      }
    }
    case 'configure/qualitySelected': {
      const { modes, modeId } = state.configure
      if (modes.status !== 'loaded') {
        return state
      }
      const option = findMode(modes.modes, modeId)?.quality_options.find(
        (candidate) => candidate.id === action.qualityId,
      )
      if (option === undefined) {
        return state
      }
      return {
        ...state,
        configure: { ...state.configure, qualityId: option.id },
      }
    }
    case 'configure/stereoHandlingSelected':
      return {
        ...state,
        configure: {
          ...state.configure,
          stereoHandling: action.stereoHandling,
        },
      }
    case 'configure/createStarted':
      return {
        ...state,
        configure: { ...state.configure, create: { status: 'creating' } },
      }
    case 'configure/createSucceeded':
      return {
        ...state,
        phase: 'separate',
        configure: { ...state.configure, create: { status: 'idle' } },
      }
    case 'configure/createFailed':
      return {
        ...state,
        configure: {
          ...state.configure,
          create: {
            status: 'error',
            code: action.code,
            message: action.message,
          },
        },
      }
    case 'results/inspect':
      return state.phase === 'separate' || state.phase === 'inspect'
        ? { ...state, phase: 'inspect' }
        : state
    case 'results/startAnother':
      return {
        ...state,
        // Always a transition, never a no-op: its dispatcher pairs it with an
        // unconditional `job/clear`, and if only one of the two applied the
        // user would be left on a phase whose job had vanished, with no
        // control left to escape it. Without an uploaded file there is
        // nothing to configure, so the honest destination is file selection.
        phase: state.upload.status === 'uploaded' ? 'configure' : 'select',
        configure: { ...state.configure, create: { status: 'idle' } },
      }
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
