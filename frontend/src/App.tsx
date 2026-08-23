import { AppStateProvider } from './state/appState'
import { Header } from './components/Header'
import { Workspace } from './components/Workspace'

/**
 * Application shell: wires up global state and lays out the header
 * above the workflow workspace.
 */
export default function App() {
  return (
    <AppStateProvider>
      <div className="app">
        <Header />
        <Workspace />
      </div>
    </AppStateProvider>
  )
}
