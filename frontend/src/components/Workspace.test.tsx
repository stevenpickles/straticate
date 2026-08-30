import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Workspace } from './Workspace'
import {
  AppStateProvider,
  initialAnalysisState,
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
  sampleResult,
  sampleRuntimeMetrics,
  sampleSeparationModes,
} from '../test/fixtures'
import { StemSessionProvider } from '../state/stemSession'
import { FakeAudioContext } from '../test/fakeAudioContext'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Answer the two reads the configure phase makes on mount.
 *
 * Routed by URL rather than answered with one shared `Response`, because a
 * `Response` body can only be read once: feature 063 added a second request
 * (the fire-and-forget stereo measurement), and a single mock value would have
 * one of the two fail on whichever arrived second.
 */
function stubModesFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) =>
      Promise.resolve(
        url.endsWith('/analysis')
          ? jsonResponse({ l_r_correlation: 0.86, wide_stereo: false })
          : jsonResponse(sampleSeparationModes),
      ),
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
    analysis: initialAnalysisState,
    configure: initialConfigureState,
    ...appState,
  }
  return render(
    <AppStateProvider initialState={initialState}>
      <JobStateProvider initialState={{ ...initialJobState, ...jobState }}>
        {/*
          The stem player is a *view* of the tracked job's session (feature
          065); App.tsx mounts the provider beside the job event bridge, so a
          test that renders the workspace mounts it too.
        */}
        <StemSessionProvider>
          <Workspace />
        </StemSessionProvider>
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

  it('makes the whole round trip: complete, inspect, start another', async () => {
    // The gap the PR #26 review caught: `SeparationProgress` is mounted for
    // `separate` alone, so once the user opens the results the only control
    // that dispatches `results/startAnother` disappears with it. This walks
    // the real components through the real reducers to prove it does not.
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve(
          new Response(
            JSON.stringify(
              String(url).endsWith('/result')
                ? sampleResult
                : sampleSeparationModes,
            ),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
        ),
      ),
    )
    const completed = {
      ...sampleJob,
      state: 'completed' as const,
      progress: 1,
      result: sampleResult,
    }
    renderWorkspace(
      {
        phase: 'separate',
        upload: { status: 'uploaded', file: sampleAudioFile },
      },
      { job: completed },
    )

    await userEvent.click(screen.getByRole('button', { name: 'View results' }))
    expect(screen.getByText('Inspect')).toBeInTheDocument()
    expect(
      await screen.findByRole('button', { name: 'Mute vocals' }),
    ).toBeInTheDocument()

    // The escape hatch survived the phase change.
    await userEvent.click(
      screen.getByRole('button', { name: 'Start another separation' }),
    )

    expect(screen.getByText('Configure')).toBeInTheDocument()
    expect(
      screen.getByRole('region', { name: 'Separation options' }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('region', { name: 'Stem player' }),
    ).not.toBeInTheDocument()
  })

  it('mounts the stem player and the export panel in the inspect phase', async () => {
    // jsdom has no Web Audio API; the stem player's default engine builds an
    // AudioContext once the result loads.
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(sampleResult), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      ),
    )
    renderWorkspace(
      { phase: 'inspect' },
      { job: { ...sampleJob, state: 'completed', result: sampleResult } },
    )

    expect(screen.getByText('Inspect')).toBeInTheDocument()
    // Feature 023 owns StemPlayer; feature 024 owns ExportPanel.
    expect(
      screen.getByRole('region', { name: 'Stem player' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Export' })).toBeInTheDocument()
    expect(
      await screen.findByRole('button', { name: 'Mute vocals' }),
    ).toBeInTheDocument()
  })
})
