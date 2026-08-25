import { useState } from 'react'
import { AppStateProvider } from './state/appState'
import { JobStateProvider } from './state/jobState'
import { SessionGate } from './state/SessionGate'
import { Header } from './components/Header'
import { ModelLibrary } from './components/ModelLibrary'
import { Workspace } from './components/Workspace'
import { JobEventBridge } from './ws/JobEventBridge'

/**
 * Application shell: wires up global state and lays out the header
 * above the workflow workspace.
 *
 * {@link JobStateProvider} only holds job state; {@link JobEventBridge}
 * opens the job event socket once for the whole session (rather than only
 * while the `separate` phase is on screen) and resyncs the tracked job over
 * REST on every (re)connect.
 *
 * {@link SessionGate} rehydrates the workflow from the identifiers the last
 * page kept in `sessionStorage` (feature 033) and holds the workspace back
 * until it has, so a reload lands on the phase the user left rather than
 * flashing file selection on the way there. With nothing stored it settles
 * on its first render and the app starts exactly as it always did.
 *
 * {@link ModelLibrary} (feature 037) is the one thing here that is *not* a
 * workflow phase — managing model weights is not a step of separating a file —
 * so it is a view beside the workspace rather than inside it. Opening it
 * **hides the workspace without unmounting it**: the stem player's Web Audio
 * graph, a running job's progress and the uploaded file all survive a trip to
 * the library and back, which they would not if the workspace were swapped
 * out. Whether the library is open is deliberately local state and not part of
 * {@link AppStateProvider}: it is not a phase, it is not persisted across a
 * reload, and nothing in the workflow may branch on it.
 */
export default function App() {
  const [libraryOpen, setLibraryOpen] = useState(false)

  return (
    <AppStateProvider>
      <JobStateProvider>
        <JobEventBridge />
        <div className="app">
          <Header
            libraryOpen={libraryOpen}
            onToggleLibrary={() => {
              setLibraryOpen((open) => !open)
            }}
          />
          <div className="app-workflow" hidden={libraryOpen}>
            <SessionGate>
              <Workspace />
            </SessionGate>
          </div>
          {libraryOpen && (
            <ModelLibrary
              onClose={() => {
                setLibraryOpen(false)
              }}
            />
          )}
        </div>
      </JobStateProvider>
    </AppStateProvider>
  )
}
