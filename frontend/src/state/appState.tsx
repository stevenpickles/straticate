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

/** Shape of the global application state. */
export interface AppState {
  /** The workflow phase currently shown in the workspace. */
  readonly phase: WorkflowPhase
}

/** Actions accepted by {@link appReducer}. */
export type AppAction = {
  readonly type: 'phase/set'
  readonly phase: WorkflowPhase
}

/** Initial application state: the workflow starts at file selection. */
export const initialAppState: AppState = {
  phase: 'select',
}

/** Pure reducer over {@link AppState}; exported for direct unit testing. */
export function appReducer(state: AppState, action: AppAction): AppState {
  switch (action.type) {
    case 'phase/set':
      return { ...state, phase: action.phase }
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
