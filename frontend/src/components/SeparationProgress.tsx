import { useJobState } from '../state/jobState'
import './SeparationProgress.css'

/**
 * Placeholder for the separation progress UI.
 *
 * TODO(feature 017): this file is owned by feature 017 (progress UI + cancel
 * + job error handling) from now on. Feature 011 created it only so the
 * `separate` phase has a mount point in `Workspace.tsx` — 017 replaces the
 * body below with the real chunk-grained progress bar, stage display, cancel
 * button and terminal-state handling, and calls `useJobEvents()` (nothing
 * mounted here opens the socket yet, so this text does not update on its own).
 * Style it in `SeparationProgress.css`, not in `index.css`.
 */
export function SeparationProgress() {
  const { job } = useJobState()

  return (
    <section className="separation-progress" aria-label="Separation progress">
      {job === null ? (
        <p className="workspace-hint">No separation job is being tracked.</p>
      ) : (
        <p className="workspace-hint">
          {job.state} — {Math.round(job.progress * 100)}%
        </p>
      )}
    </section>
  )
}
