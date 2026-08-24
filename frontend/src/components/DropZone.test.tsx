import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Workspace } from './Workspace'
import { AppStateProvider } from '../state/appState'
import { JobStateProvider } from '../state/jobState'
import { installMockXhr, lastXhr, MockXMLHttpRequest } from '../test/mockXhr'
import { sampleAudioFile, sampleSeparationModes } from '../test/fixtures'

// Reaching the configure phase mounts SeparationOptions (feature 011),
// which loads the mode catalog; this suite is about the drop zone.
vi.mock('../api/modes', () => ({
  listSeparationModes: vi.fn(() => Promise.resolve(sampleSeparationModes)),
}))

function renderWorkspace() {
  return render(
    <AppStateProvider>
      <JobStateProvider>
        <Workspace />
      </JobStateProvider>
    </AppStateProvider>,
  )
}

function makeFile(name = 'song.wav'): File {
  return new File(['RIFF....WAVE'], name, { type: 'audio/wav' })
}

function getDropZone(): HTMLElement {
  return screen.getByRole('region', { name: 'Audio file selection' })
}

function getFileInput(): HTMLInputElement {
  const input = getDropZone().querySelector('input[type="file"]')
  if (!(input instanceof HTMLInputElement)) {
    throw new Error('file input not found')
  }
  return input
}

beforeEach(() => {
  installMockXhr()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DropZone', () => {
  it('renders both the drop prompt and the file picker button', () => {
    renderWorkspace()
    expect(screen.getByText('Drop a music file here')).toBeInTheDocument()
    expect(screen.getByText('or')).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Choose a File' }),
    ).toBeInTheDocument()
    expect(getFileInput().accept).toContain('.flac')
  })

  it('starts an upload when a file is chosen via the picker', () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })

    expect(MockXMLHttpRequest.instances).toHaveLength(1)
    expect(lastXhr().url).toBe('/api/v1/audio')
    expect(screen.getByText(/Uploading/)).toBeInTheDocument()
  })

  it('starts an upload when a file is dropped, taking the first of many', () => {
    renderWorkspace()
    fireEvent.drop(getDropZone(), {
      dataTransfer: {
        files: [makeFile('first.flac'), makeFile('second.flac')],
        types: ['Files'],
      },
    })

    expect(MockXMLHttpRequest.instances).toHaveLength(1)
    const body = lastXhr().sentBody as FormData
    expect((body.get('file') as File).name).toBe('first.flac')
  })

  it('highlights on dragover with files and clears on dragleave', () => {
    renderWorkspace()
    const zone = getDropZone()

    fireEvent.dragOver(zone, { dataTransfer: { files: [], types: ['Files'] } })
    expect(zone.className).toContain('drop-zone-active')

    fireEvent.dragLeave(zone)
    expect(zone.className).not.toContain('drop-zone-active')
  })

  it('ignores drags that carry no files', () => {
    renderWorkspace()
    const zone = getDropZone()

    fireEvent.dragOver(zone, {
      dataTransfer: { files: [], types: ['text/plain'] },
    })
    expect(zone.className).not.toContain('drop-zone-active')

    fireEvent.drop(zone, {
      dataTransfer: { files: [], types: ['text/uri-list'] },
    })
    expect(MockXMLHttpRequest.instances).toHaveLength(0)
  })

  it('shows determinate progress from upload progress events', () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })

    act(() => {
      lastXhr().emitUploadProgress(50, 100)
    })

    const bar = screen.getByRole('progressbar', { name: 'Upload progress' })
    expect(bar).toHaveAttribute('aria-valuenow', '50')
    expect(screen.getByText('Uploading… 50%')).toBeInTheDocument()
  })

  it('shows an indeterminate bar when the length is not computable', () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })

    act(() => {
      lastXhr().emitUploadProgress(1024, 0, false)
    })

    const bar = screen.getByRole('progressbar', { name: 'Upload progress' })
    expect(bar).not.toHaveAttribute('aria-valuenow')
    expect(bar.className).toContain('progress-indeterminate')
  })

  it('stores the AudioFile and advances to configure on success', async () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })
    await act(async () => {
      lastXhr().respond(201, JSON.stringify(sampleAudioFile))
    })

    expect(await screen.findByText('Configure')).toBeInTheDocument()
    expect(screen.getByText('Midnight Train.flac')).toBeInTheDocument()
    expect(screen.queryByText('Drop a music file here')).not.toBeInTheDocument()
  })

  it('shows the backend message on a 413 envelope and allows retry', async () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })
    await act(async () => {
      lastXhr().respond(
        413,
        JSON.stringify({
          error: {
            code: 'audio_too_large',
            message: 'The uploaded file exceeds the maximum allowed size.',
          },
        }),
      )
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'The uploaded file exceeds the maximum allowed size.',
    )
    // The idle affordances are back: retrying works.
    expect(
      screen.getByRole('button', { name: 'Choose a File' }),
    ).toBeInTheDocument()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })
    expect(MockXMLHttpRequest.instances).toHaveLength(2)
    expect(screen.getByText(/Uploading/)).toBeInTheDocument()
  })

  it('cancel aborts the upload and returns the drop zone to idle', async () => {
    renderWorkspace()
    fireEvent.change(getFileInput(), { target: { files: [makeFile()] } })

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(lastXhr().aborted).toBe(true)
    expect(
      await screen.findByText('Drop a music file here'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('opens the file picker when the button is activated with the keyboard', async () => {
    renderWorkspace()
    const clickSpy = vi.spyOn(getFileInput(), 'click')

    const button = screen.getByRole('button', { name: 'Choose a File' })
    button.focus()
    await userEvent.keyboard('{Enter}')

    expect(clickSpy).toHaveBeenCalled()
  })
})
