import { useEffect, useState, type Ref } from 'react'
import { getVersion } from '../api/client'
import { MODEL_LIBRARY_ID } from './modelLibraryId'

type BackendStatus =
  | { kind: 'checking' }
  | { kind: 'available'; version: string }
  | { kind: 'unavailable' }

/** Props of {@link Header}. */
export interface HeaderProps {
  /** Whether the model library is the view currently on screen. */
  readonly libraryOpen?: boolean
  /**
   * Show or hide the model library. Optional so the header can be rendered on
   * its own; without it the button is inert rather than absent, because a
   * header that sometimes has a control and sometimes does not is harder to
   * reason about than one that always does.
   */
  readonly onToggleLibrary?: () => void
  /**
   * Forwarded to the models button, so whoever closes the library can put
   * focus back on the control the user came in through.
   */
  readonly ref?: Ref<HTMLButtonElement>
}

/**
 * Application header: name, tagline, the way in and out of the model library,
 * and a live backend indicator that shows the backend version or a graceful
 * "backend unavailable" state.
 *
 * The models button is the whole navigation this application has. Feature 037
 * put the library beside the workflow rather than as a sixth phase of it —
 * managing model weights is not a step of separating a file — so the header,
 * which is on screen in every phase, is where it belongs.
 */
export function Header({
  libraryOpen = false,
  onToggleLibrary,
  ref,
}: HeaderProps) {
  const [status, setStatus] = useState<BackendStatus>({ kind: 'checking' })

  useEffect(() => {
    let cancelled = false
    getVersion()
      .then((response) => {
        if (!cancelled) {
          setStatus({ kind: 'available', version: response.version })
        }
      })
      .catch(() => {
        if (!cancelled) {
          setStatus({ kind: 'unavailable' })
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <header className="header">
      <div className="header-brand">
        <h1 className="header-title">Straticate</h1>
        <p className="header-tagline">Extricate the layers</p>
      </div>
      <div className="header-actions">
        <button
          ref={ref}
          type="button"
          className="header-models"
          aria-expanded={libraryOpen}
          // Only while it exists: pointing at an absent element is worse than
          // pointing at nothing.
          aria-controls={libraryOpen ? MODEL_LIBRARY_ID : undefined}
          onClick={onToggleLibrary}
        >
          {libraryOpen ? 'Close models' : 'Models'}
        </button>
      </div>
      <div className="header-backend" role="status">
        {status.kind === 'checking' && (
          <span className="backend-checking">checking backend…</span>
        )}
        {status.kind === 'available' && (
          <span className="backend-available">backend v{status.version}</span>
        )}
        {status.kind === 'unavailable' && (
          <span className="backend-unavailable">backend unavailable</span>
        )}
      </div>
    </header>
  )
}
