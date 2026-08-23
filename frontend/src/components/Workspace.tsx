import { useAppState, type WorkflowPhase } from '../state/appState'
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
 * phase shows the file drop zone; `configure` currently shows a minimal
 * confirmation of the uploaded file (the metadata panel is feature 009).
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
        <p className="workspace-hint">
          Uploaded <strong>{upload.file.filename}</strong>. Configuration
          options are coming soon.
        </p>
      )}
    </main>
  )
}
