import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TelemetryPanel } from './TelemetryPanel'
import {
  JobStateProvider,
  initialJobState,
  useJobDispatch,
} from '../state/jobState'
import {
  sampleCpuRuntimeMetrics,
  sampleFakeDeviceRuntimeMetrics,
  sampleJob,
  sampleRuntimeMetrics,
} from '../test/fixtures'
import type { GpuMetrics, RuntimeMetricsEvent } from '../api/types'

/** The device block of a fixture, without a non-null assertion. */
function requireDevice(event: RuntimeMetricsEvent): GpuMetrics {
  if (event.gpu === null) {
    throw new Error('fixture is expected to carry a device block')
  }
  return event.gpu
}

const cudaDevice = requireDevice(sampleRuntimeMetrics)

/** The CUDA-shaped fixture with some device fields overridden. */
function withDevice(overrides: Partial<GpuMetrics>): RuntimeMetricsEvent {
  return { ...sampleRuntimeMetrics, gpu: { ...cudaDevice, ...overrides } }
}

/** Render the panel with `metrics` already in the store. */
function renderPanel(metrics: RuntimeMetricsEvent | null) {
  return render(
    <JobStateProvider initialState={{ ...initialJobState, metrics }}>
      <TelemetryPanel />
    </JobStateProvider>,
  )
}

/** The `<dd>` value rendered for the `<dt>` labelled `label`, or `null`. */
function fieldValue(label: string): string | null {
  const term = Array.from(document.querySelectorAll('dt')).find(
    (element) => element.textContent === label,
  )
  return term?.nextElementSibling?.textContent ?? null
}

/** Labels of every row currently rendered. */
function renderedLabels(): string[] {
  return Array.from(document.querySelectorAll('dt')).map(
    (element) => element.textContent ?? '',
  )
}

describe('TelemetryPanel', () => {
  it('renders the model, device and processing groups from a sample', () => {
    renderPanel(sampleRuntimeMetrics)

    expect(
      screen.getByRole('region', { name: 'Runtime telemetry' }),
    ).toBeInTheDocument()
    for (const title of ['Model', 'Device', 'Processing']) {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument()
    }

    expect(fieldValue('Model')).toBe('Vocals — High Quality')
    expect(fieldValue('Architecture')).toBe('mel_band_roformer')
    expect(fieldValue('Version')).toBe('1.0')
    expect(fieldValue('Mode')).toBe('vocals')
    expect(fieldValue('Stems')).toBe('2')

    expect(fieldValue('Device')).toBe('NVIDIA GeForce RTX 5090')
    expect(fieldValue('Backend')).toBe('cuda')
    expect(fieldValue('Device ID')).toBe('cuda:0')
    expect(fieldValue('Memory Allocated')).toBe('8.6 GB')
    expect(fieldValue('Memory Peak')).toBe('9.4 GB')
    expect(fieldValue('Memory Total')).toBe('32 GB')
    expect(fieldValue('Utilization')).toBe('91%')
    expect(fieldValue('Temperature')).toBe('63 °C')

    expect(fieldValue('Stage')).toBe('Separating')
    expect(fieldValue('Chunks')).toBe('31 of 48')
    expect(fieldValue('Elapsed')).toBe('0:18')
    expect(fieldValue('Audio Processed')).toBe('2:28')
    expect(fieldValue('Real-Time Factor')).toBe('7.9×')
  })

  it('renders nothing at all before the first sample arrives', () => {
    const { container } = renderPanel(null)

    expect(container).toBeEmptyDOMElement()
    expect(
      screen.queryByRole('region', { name: 'Runtime telemetry' }),
    ).not.toBeInTheDocument()
  })

  it('omits the whole device group when the run reports no device', () => {
    renderPanel(sampleCpuRuntimeMetrics)

    expect(
      screen.queryByRole('heading', { name: 'Device' }),
    ).not.toBeInTheDocument()
    expect(renderedLabels()).not.toContain('Memory Total')
    expect(renderedLabels()).not.toContain('Backend')

    // Model and processing are unaffected.
    expect(screen.getByRole('heading', { name: 'Model' })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', { name: 'Processing' }),
    ).toBeInTheDocument()
    expect(fieldValue('Model')).toBe('Vocals — High Quality')
    expect(fieldValue('Real-Time Factor')).toBe('7.9×')
  })

  it('omits only the utilization row when NVML reported no utilization', () => {
    renderPanel(withDevice({ utilization: null }))

    expect(renderedLabels()).not.toContain('Utilization')
    expect(fieldValue('Temperature')).toBe('63 °C')
    expect(fieldValue('Memory Allocated')).toBe('8.6 GB')
    expect(fieldValue('Memory Total')).toBe('32 GB')
  })

  it('omits only the temperature row when NVML reported no temperature', () => {
    renderPanel(withDevice({ temperature_celsius: null }))

    expect(renderedLabels()).not.toContain('Temperature')
    expect(fieldValue('Utilization')).toBe('91%')
    expect(fieldValue('Memory Peak')).toBe('9.4 GB')
  })

  it('keeps the memory rows when NVML is unavailable entirely', () => {
    renderPanel(withDevice({ utilization: null, temperature_celsius: null }))

    expect(screen.getByRole('heading', { name: 'Device' })).toBeInTheDocument()
    expect(renderedLabels()).not.toContain('Utilization')
    expect(renderedLabels()).not.toContain('Temperature')
    expect(fieldValue('Memory Allocated')).toBe('8.6 GB')
    expect(fieldValue('Memory Peak')).toBe('9.4 GB')
    expect(fieldValue('Memory Total')).toBe('32 GB')
    expect(fieldValue('Device')).toBe('NVIDIA GeForce RTX 5090')
  })

  it('renders the development separator’s honest "fake" device', () => {
    renderPanel(sampleFakeDeviceRuntimeMetrics)

    expect(screen.getByRole('heading', { name: 'Device' })).toBeInTheDocument()
    expect(fieldValue('Device')).toBe('Straticate Fake Accelerator')
    expect(fieldValue('Backend')).toBe('fake')
    expect(fieldValue('Device ID')).toBe('fake:0')
    expect(fieldValue('Memory Allocated')).toBe('1.4 GB')
    expect(fieldValue('Memory Total')).toBe('8 GB')
    expect(fieldValue('Utilization')).toBe('42%')
    expect(fieldValue('Temperature')).toBe('48 °C')

    expect(fieldValue('Model')).toBe('Fake Vocals (development)')
    expect(fieldValue('Architecture')).toBe('fake')
    expect(fieldValue('Stage')).toBe('Loading model')
    expect(fieldValue('Chunks')).toBe('0 of 46')
    expect(fieldValue('Real-Time Factor')).toBe('0.5×')
  })

  it('replaces the displayed values when a newer sample arrives', async () => {
    const later: RuntimeMetricsEvent = {
      ...sampleRuntimeMetrics,
      gpu: {
        ...cudaDevice,
        memory_allocated_bytes: 12079595520,
        utilization: 0.74,
        temperature_celsius: 68,
      },
      processing: {
        ...sampleRuntimeMetrics.processing,
        stage: 'post_processing',
        chunks_completed: 48,
        elapsed_seconds: 29.1,
        audio_processed_seconds: 227.4,
        realtime_factor: 7.8,
      },
    }

    function Harness() {
      const dispatch = useJobDispatch()
      return (
        <>
          <button
            type="button"
            onClick={() => {
              dispatch({ type: 'ws/event', event: later })
            }}
          >
            Emit newer sample
          </button>
          <TelemetryPanel />
        </>
      )
    }

    render(
      <JobStateProvider
        initialState={{
          ...initialJobState,
          job: sampleJob,
          metrics: sampleRuntimeMetrics,
        }}
      >
        <Harness />
      </JobStateProvider>,
    )

    expect(fieldValue('Real-Time Factor')).toBe('7.9×')

    await userEvent.click(
      screen.getByRole('button', { name: 'Emit newer sample' }),
    )

    expect(await screen.findByText('7.8×')).toBeInTheDocument()
    expect(fieldValue('Stage')).toBe('Post processing')
    expect(fieldValue('Chunks')).toBe('48 of 48')
    expect(fieldValue('Elapsed')).toBe('0:29')
    expect(fieldValue('Audio Processed')).toBe('3:47')
    expect(fieldValue('Memory Allocated')).toBe('11.3 GB')
    expect(fieldValue('Utilization')).toBe('74%')
    expect(fieldValue('Temperature')).toBe('68 °C')
  })
})
