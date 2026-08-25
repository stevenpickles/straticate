import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Workspace } from './Workspace'
import {
  AppStateProvider,
  initialConfigureState,
  type AppState,
} from '../state/appState'
import { JobStateProvider } from '../state/jobState'
import {
  sampleAudioFile,
  sampleBuiltInModel,
  sampleSeparationModes,
} from '../test/fixtures'
import type { AudioFile } from '../api/types'
import { deleteAudio } from '../api/audio'

vi.mock('../api/audio', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/audio')>()),
  deleteAudio: vi.fn(() => Promise.resolve()),
}))

// The configure phase also mounts SeparationOptions (feature 011), which
// loads the catalog on mount; this suite is about the metadata summary.
vi.mock('../api/modes', () => ({
  listSeparationModes: vi.fn(() => Promise.resolve(sampleSeparationModes)),
}))

// …and reads the selected tier's model to see whether its weights need
// installing (feature 035). A model that needs no download renders nothing.
vi.mock('../api/models', () => ({
  listModels: vi.fn(() => Promise.resolve([sampleBuiltInModel])),
  getModel: vi.fn(() => Promise.resolve(sampleBuiltInModel)),
  installModel: vi.fn(() => Promise.resolve(sampleBuiltInModel)),
  removeModelWeights: vi.fn(() => Promise.resolve(sampleBuiltInModel)),
}))

const deleteAudioMock = vi.mocked(deleteAudio)

/** Render the workspace already in the configure phase with `file` uploaded. */
function renderConfigured(file: AudioFile = sampleAudioFile) {
  const initialState: AppState = {
    phase: 'configure',
    upload: { status: 'uploaded', file },
    configure: initialConfigureState,
  }
  return render(
    <AppStateProvider initialState={initialState}>
      <JobStateProvider>
        <Workspace />
      </JobStateProvider>
    </AppStateProvider>,
  )
}

/** The `<dd>` value rendered for a metadata label, or `null` when absent. */
function fieldValue(label: string): string | null {
  const term = screen.queryByText(label)
  return term?.nextElementSibling?.textContent ?? null
}

beforeEach(() => {
  deleteAudioMock.mockReset()
  deleteAudioMock.mockResolvedValue(undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AudioSummary', () => {
  it('shows the filename prominently', () => {
    renderConfigured()
    expect(
      screen.getByRole('heading', { name: 'Midnight Train.flac' }),
    ).toBeInTheDocument()
  })

  it('renders every present metadata field with display formatting', () => {
    renderConfigured()

    expect(fieldValue('Duration')).toBe('3:47')
    expect(fieldValue('Format')).toBe('FLAC')
    expect(fieldValue('Channels')).toBe('Stereo')
    expect(fieldValue('Sample Rate')).toBe('44.1 kHz')
    expect(fieldValue('Bit Depth')).toBe('24 bit')
    expect(fieldValue('Bit Rate')).toBe('1411 kbps')
    expect(fieldValue('Size')).toBe('42.7 MB')
  })

  it('omits the bit-depth row for a lossy file and keeps the bit rate', () => {
    const lossy: AudioFile = {
      ...sampleAudioFile,
      filename: 'Midnight Train.mp3',
      size_bytes: 9109504,
      metadata: {
        ...sampleAudioFile.metadata,
        container: 'mp3',
        codec: 'mp3',
        bit_depth: null,
        bit_rate_bps: 320000,
      },
    }
    renderConfigured(lossy)

    expect(screen.queryByText('Bit Depth')).not.toBeInTheDocument()
    expect(fieldValue('Format')).toBe('MP3')
    expect(fieldValue('Bit Rate')).toBe('320 kbps')
    expect(fieldValue('Size')).toBe('8.7 MB')
  })

  it('omits the bit-rate row when the backend reported none', () => {
    const noBitRate: AudioFile = {
      ...sampleAudioFile,
      metadata: { ...sampleAudioFile.metadata, bit_rate_bps: null },
    }
    renderConfigured(noBitRate)

    expect(screen.queryByText('Bit Rate')).not.toBeInTheDocument()
    expect(fieldValue('Bit Depth')).toBe('24 bit')
  })

  it('choose a different file resets to select and deletes the upload', async () => {
    renderConfigured()

    await userEvent.click(
      screen.getByRole('button', { name: 'Choose a different file' }),
    )

    expect(deleteAudioMock).toHaveBeenCalledWith(sampleAudioFile.id)
    expect(await screen.findByText('Select')).toBeInTheDocument()
    expect(screen.getByText('Drop a music file here')).toBeInTheDocument()
    expect(screen.queryByText('Midnight Train.flac')).not.toBeInTheDocument()
  })

  it('resets the UI even when the backend delete fails', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    deleteAudioMock.mockRejectedValue(new Error('audio_not_found'))
    renderConfigured()

    await userEvent.click(
      screen.getByRole('button', { name: 'Choose a different file' }),
    )

    expect(
      await screen.findByText('Drop a music file here'),
    ).toBeInTheDocument()
    expect(screen.queryByText('Midnight Train.flac')).not.toBeInTheDocument()
    expect(warn).toHaveBeenCalled()
  })
})
