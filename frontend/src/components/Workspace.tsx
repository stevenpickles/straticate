import { useAppState, type WorkflowPhase } from '../state/appState'

const PHASE_LABELS: Record<WorkflowPhase, string> = {
  select: 'Select',
  configure: 'Configure',
  separate: 'Separate',
  inspect: 'Inspect',
  export: 'Export',
}

/**
 * Main workspace area: renders the current workflow phase. For now a
 * placeholder — the file drop zone arrives with feature 008.
 */
export function Workspace() {
  const { phase } = useAppState()

  return (
    <main className="workspace">
      <p className="workspace-phase">{PHASE_LABELS[phase]}</p>
      <p className="workspace-hint">
        Select an audio file to begin separating stems.
      </p>
    </main>
  )
}
