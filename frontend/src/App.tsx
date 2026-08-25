import { useRef, useState } from 'react'
import { AppStateProvider } from './state/appState'
import { DiskSpaceProvider } from './state/diskSpace'
import { JobStateProvider } from './state/jobState'
import { ModelRevisionProvider } from './state/modelRevision'
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
 *
 * {@link DiskSpaceProvider} holds one reading of the free space on the machine
 * running Straticate (feature 040), because both places an install is offered —
 * the configure step's panel and the library's cards — must not disagree about
 * a fact concerning one disk. It **fetches nothing on mount**: the read happens
 * where an install is actually offered, and again when a download changes what
 * is on the disk. There is no timer anywhere in it.
 *
 * That "hidden, not unmounted" choice has one consequence this component owns:
 * the workflow does not re-read anything on the way back, so a model installed
 * or removed from a library card would leave the configure step describing the
 * world as it was. Closing the library therefore bumps
 * {@link ModelRevisionProvider}'s counter, which is the signal for a view to
 * re-read model state **once** — a known event, not a timer.
 */
export default function App() {
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [modelRevision, setModelRevision] = useState(0)
  const libraryToggleRef = useRef<HTMLButtonElement>(null)

  const closeLibrary = () => {
    setLibraryOpen(false)
    // Models may have been installed or removed while it was open.
    setModelRevision((revision) => revision + 1)
  }

  return (
    <AppStateProvider>
      <JobStateProvider>
        <DiskSpaceProvider>
          <ModelRevisionProvider revision={modelRevision}>
            <JobEventBridge />
            <div className="app">
              <Header
                ref={libraryToggleRef}
                libraryOpen={libraryOpen}
                onToggleLibrary={() => {
                  if (libraryOpen) {
                    closeLibrary()
                  } else {
                    setLibraryOpen(true)
                  }
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
                    // Focus first, while the button that asked to close is still
                    // mounted: a keyboard user who pressed "Back to workflow"
                    // has conceptually returned to the control they came in
                    // through, and dropping focus on `<body>` would restart
                    // their next Tab at the top of the document.
                    libraryToggleRef.current?.focus()
                    closeLibrary()
                  }}
                />
              )}
            </div>
          </ModelRevisionProvider>
        </DiskSpaceProvider>
      </JobStateProvider>
    </AppStateProvider>
  )
}
