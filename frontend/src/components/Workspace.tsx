import { useAppState, type WorkflowPhase } from '../state/appState'
import { AudioSummary } from './AudioSummary'
import { DropZone } from './DropZone'

const PHASE_LABELS: Record<WorkflowPhase, string> = {
  select: 'Select',
  configure: 'Configure',
  separate: 'Separate',
  inspect: 'Inspect',
  export: 'Export',
}

/**
 * Main workspace area: renders the current workflow phase. The `select`
 * phase shows the file drop zone; `configure` shows the uploaded file's
 * metadata summary above the (not yet built) separation options.
 */
export function Workspace() {
  const { phase, upload } = useAppState()

  return (
    <main className="workspace">
      <p className="workspace-phase">{PHASE_LABELS[phase]}</p>
      {phase === 'select' && (
        <>
          <p className="workspace-hint">
            Select an audio file to begin separating stems.
          </p>
          <DropZone />
        </>
      )}
      {phase === 'configure' && upload.status === 'uploaded' && (
        <>
          <AudioSummary file={upload.file} />
          {/* Placeholder for the separation mode + quality chooser (feature 011). */}
          <section
            className="separation-options-placeholder"
            aria-label="Separation options"
          >
            <p className="workspace-hint">Separation options coming next.</p>
          </section>
        </>
      )}
    </main>
  )
}
