import { useJobState } from '../state/jobState'
import './TelemetryPanel.css'

/**
 * Placeholder for the runtime telemetry panel.
 *
 * TODO(feature 020): this file is owned by feature 020 (telemetry panel UI)
 * from now on. Feature 011 created it only so the `separate` phase has a
 * mount point in `Workspace.tsx`. 020 renders `metrics.model`, `metrics.gpu`
 * (null on CPU) and `metrics.processing` from `useJobState()` inside this
 * region, and styles it in `TelemetryPanel.css`, not in `index.css`.
 *
 * Renders nothing until a `runtime_metrics` event has arrived.
 */
export function TelemetryPanel() {
  const { metrics } = useJobState()

  if (metrics === null) {
    return null
  }

  return <section className="telemetry-panel" aria-label="Runtime telemetry" />
}
