import { useEffect, useState } from 'react'
import { getVersion } from '../api/client'

type BackendStatus =
  | { kind: 'checking' }
  | { kind: 'available'; version: string }
  | { kind: 'unavailable' }

/**
 * Application header: name, tagline, and a live backend indicator that
 * shows the backend version or a graceful "backend unavailable" state.
 */
export function Header() {
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
