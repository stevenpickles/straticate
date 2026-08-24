import './ExportPanel.css'

/**
 * Placeholder mount point for the export UI in the `inspect` phase.
 *
 * TODO(feature 024): replace this body with the real export UI — format
 * choice, stem selection, and the download itself, over
 * `GET /api/v1/jobs/{id}/export` (feature 022). Feature 024 owns this file
 * and its sibling `ExportPanel.css` from now on, and should not need to edit
 * `Workspace.tsx`, `state/appState.tsx` or `index.css` to do it.
 */
export function ExportPanel() {
  return (
    <section className="export-panel" aria-label="Export">
      <p className="workspace-hint">
        Exporting stems arrives with feature 024.
      </p>
    </section>
  )
}
