import { AppStateProvider } from './state/appState'
import { JobStateProvider } from './state/jobState'
import { SessionGate } from './state/SessionGate'
import { Header } from './components/Header'
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
 */
export default function App() {
  return (
    <AppStateProvider>
      <JobStateProvider>
        <JobEventBridge />
        <div className="app">
          <Header />
          <SessionGate>
            <Workspace />
          </SessionGate>
        </div>
      </JobStateProvider>
    </AppStateProvider>
  )
}
