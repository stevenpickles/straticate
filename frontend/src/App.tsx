import { AppStateProvider } from './state/appState'
import { JobStateProvider } from './state/jobState'
import { Header } from './components/Header'
import { Workspace } from './components/Workspace'

/**
 * Application shell: wires up global state and lays out the header
 * above the workflow workspace.
 *
 * {@link JobStateProvider} only holds job state; the job event socket is
 * opened by whichever component calls `useJobEvents()` (feature 017).
 */
export default function App() {
  return (
    <AppStateProvider>
      <JobStateProvider>
        <div className="app">
          <Header />
          <Workspace />
        </div>
      </JobStateProvider>
    </AppStateProvider>
  )
}
