import type { GpuMetrics, ModelInfo, ProcessingMetrics } from '../api/types'
import {
  formatDuration,
  formatFileSize,
  formatPercentage,
  formatRealtimeFactor,
  formatTemperature,
} from '../format'
import { useJobState } from '../state/jobState'
import './TelemetryPanel.css'

/** One label/value pair of a telemetry group's definition list. */
interface TelemetryField {
  readonly label: string
  readonly value: string
}

/** A titled block of {@link TelemetryField}s. */
interface TelemetryGroup {
  readonly title: string
  readonly fields: readonly TelemetryField[]
}

/**
 * Turn a snake_case contract token into a display label
 * (`loading_model` → `Loading model`). Purely mechanical: no stage, mode or
 * backend name is enumerated anywhere, so a value the contract adds later
 * renders without a code change.
 */
function humanize(token: string): string {
  const words = token.replace(/_/g, ' ').trim()
  return words === '' ? '' : `${words.charAt(0).toUpperCase()}${words.slice(1)}`
}

/**
 * Rows describing the loaded model. Every value comes straight from the
 * event; identifiers (`architecture`, `separation_mode`) are shown verbatim
 * because this is a diagnostic panel and those *are* the contract values.
 */
function modelFields(model: ModelInfo): TelemetryField[] {
  return [
    { label: 'Model', value: model.display_name },
    { label: 'Architecture', value: model.architecture },
    { label: 'Version', value: model.version },
    { label: 'Mode', value: model.separation_mode },
    { label: 'Stems', value: String(model.stem_count) },
  ]
}

/**
 * Rows describing the compute device. Memory figures reuse
 * {@link formatFileSize}, so VRAM keeps the app's single binary-unit
 * convention. `utilization` and `temperature_celsius` are independently
 * nullable (NVML is optional and never required): each missing row is
 * omitted entirely rather than filled with a zero or a dash, and the memory
 * rows are unaffected.
 */
function deviceFields(gpu: GpuMetrics): TelemetryField[] {
  return [
    { label: 'Device', value: gpu.name },
    { label: 'Backend', value: gpu.backend },
    { label: 'Device ID', value: gpu.device_id },
    {
      label: 'Memory Allocated',
      value: formatFileSize(gpu.memory_allocated_bytes),
    },
    { label: 'Memory Peak', value: formatFileSize(gpu.memory_peak_bytes) },
    { label: 'Memory Total', value: formatFileSize(gpu.memory_total_bytes) },
    ...(gpu.utilization === null
      ? []
      : [{ label: 'Utilization', value: formatPercentage(gpu.utilization) }]),
    ...(gpu.temperature_celsius === null
      ? []
      : [
          {
            label: 'Temperature',
            value: formatTemperature(gpu.temperature_celsius),
          },
        ]),
  ]
}

/** Rows describing how the run is going, ending with the real-time factor. */
function processingFields(processing: ProcessingMetrics): TelemetryField[] {
  return [
    { label: 'Stage', value: humanize(processing.stage) },
    {
      label: 'Chunks',
      value: `${String(processing.chunks_completed)} of ${String(processing.chunks_total)}`,
    },
    { label: 'Elapsed', value: formatDuration(processing.elapsed_seconds) },
    {
      label: 'Audio Processed',
      value: formatDuration(processing.audio_processed_seconds),
    },
    {
      label: 'Real-Time Factor',
      value: formatRealtimeFactor(processing.realtime_factor),
    },
  ]
}

/** Build the groups to render for one `runtime_metrics` sample. */
function telemetryGroups(
  model: ModelInfo,
  gpu: GpuMetrics | null,
  processing: ProcessingMetrics,
): TelemetryGroup[] {
  return [
    { title: 'Model', fields: modelFields(model) },
    // `gpu` is null when the separator reports no compute device (the
    // "running on CPU" shape): the whole group disappears, it is never a
    // block of zeros.
    ...(gpu === null ? [] : [{ title: 'Device', fields: deviceFields(gpu) }]),
    { title: 'Processing', fields: processingFields(processing) },
  ]
}

/**
 * Runtime telemetry for the tracked job: which model is loaded, what the
 * compute device is doing, and how the processing is going — including the
 * real-time factor (ARCHITECTURE.md §12).
 *
 * Renders the newest `runtime_metrics` event held by the job store, and
 * nothing at all until one has arrived. The panel does not open the
 * WebSocket (feature 017 mounts `useJobEvents()`) and keeps no history: the
 * store holds one sample and this renders that sample.
 *
 * Must be rendered under a `JobStateProvider`.
 */
export function TelemetryPanel() {
  const { metrics } = useJobState()

  if (metrics === null) {
    return null
  }

  const groups = telemetryGroups(metrics.model, metrics.gpu, metrics.processing)

  return (
    <section className="telemetry-panel" aria-label="Runtime telemetry">
      {groups.map((group) => (
        <div className="telemetry-group" key={group.title}>
          <h3 className="telemetry-group-title">{group.title}</h3>
          <dl className="telemetry-fields">
            {group.fields.map((field) => (
              <div className="telemetry-field" key={field.label}>
                <dt className="telemetry-label">{field.label}</dt>
                <dd className="telemetry-value">{field.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </section>
  )
}
