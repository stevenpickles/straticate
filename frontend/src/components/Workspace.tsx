import { useAppState, type WorkflowPhase } from '../state/appState'
import { AudioSummary } from './AudioSummary'
import { DropZone } from './DropZone'
import { ExportPanel } from './ExportPanel'
import { SeparationOptions } from './SeparationOptions'
import { SeparationProgress } from './SeparationProgress'
import { StemPlayer } from './StemPlayer'
import { TelemetryPanel } from './TelemetryPanel'

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
 * metadata summary above the separation mode + quality chooser; `separate`
 * shows the running job's progress and its runtime telemetry; `inspect`
 * shows the stem player and the export panel.
 *
 * Each phase mounts components that own their own markup and styles, so a
 * feature building out a phase edits its component (and that component's
 * sibling `.css`) rather than this file.
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
          <SeparationOptions />
        </>
      )}
      {phase === 'separate' && (
        <>
          {/* Owned by feature 017. */}
          <SeparationProgress />
          {/* Owned by feature 020. */}
          <TelemetryPanel />
        </>
      )}
      {phase === 'inspect' && (
        <>
          {/* Owned by feature 023. */}
          <StemPlayer />
          {/* Owned by feature 024. */}
          <ExportPanel />
        </>
      )}
    </main>
  )
}
