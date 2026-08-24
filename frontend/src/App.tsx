import { AppStateProvider } from './state/appState'
import { JobStateProvider } from './state/jobState'
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
 */
export default function App() {
  return (
    <AppStateProvider>
      <JobStateProvider>
        <JobEventBridge />
        <div className="app">
          <Header />
          <Workspace />
        </div>
      </JobStateProvider>
    </AppStateProvider>
  )
}
