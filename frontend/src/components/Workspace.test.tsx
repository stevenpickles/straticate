import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Workspace } from './Workspace'
import {
  AppStateProvider,
  initialConfigureState,
  type AppState,
} from '../state/appState'
import {
  JobStateProvider,
  initialJobState,
  type JobStateValue,
} from '../state/jobState'
import {
  sampleAudioFile,
  sampleJob,
  sampleRuntimeMetrics,
  sampleSeparationModes,
} from '../test/fixtures'

function stubModesFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(sampleSeparationModes), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
}

function renderWorkspace(
  appState: Partial<AppState> = {},
  jobState: Partial<JobStateValue> = {},
) {
  const initialState: AppState = {
    phase: 'select',
    upload: { status: 'idle' },
    configure: initialConfigureState,
    ...appState,
  }
  return render(
    <AppStateProvider initialState={initialState}>
      <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
        <Workspace />
      </JobStateProvider>
    </AppStateProvider>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Workspace', () => {
  it('shows the drop zone in the select phase', () => {
    renderWorkspace()
    expect(screen.getByText('Select')).toBeInTheDocument()
    expect(screen.getByText('Drop a music file here')).toBeInTheDocument()
  })

  it('mounts the separation options in the configure phase', async () => {
    stubModesFetch()
    renderWorkspace({
      phase: 'configure',
      upload: { status: 'uploaded', file: sampleAudioFile },
    })

    expect(
      screen.getByRole('region', { name: 'Separation options' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByRole('button', { name: 'Start separation' }),
    ).toBeInTheDocument()
  })

  it('mounts the progress and telemetry regions in the separate phase', () => {
    renderWorkspace(
      { phase: 'separate' },
      { job: sampleJob, metrics: sampleRuntimeMetrics },
    )

    expect(screen.getByText('Separate')).toBeInTheDocument()
    // Feature 017 owns SeparationProgress; feature 020 owns TelemetryPanel.
    expect(
      screen.getByRole('region', { name: 'Separation progress' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: 'Runtime telemetry' }),
    ).toBeInTheDocument()
  })

  it('renders the telemetry region only once metrics have arrived', () => {
    renderWorkspace({ phase: 'separate' }, { job: sampleJob })
    expect(
      screen.queryByRole('region', { name: 'Runtime telemetry' }),
    ).not.toBeInTheDocument()
  })
})
