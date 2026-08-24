import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import App from './App'
import { FakeWebSocket } from './test/mockWebSocket'

/** Records every socket the app opens, so nothing reaches the network. */
class RecordingWebSocket extends FakeWebSocket {
  static instances: RecordingWebSocket[] = []

  constructor(url: string) {
    super(url)
    RecordingWebSocket.instances.push(this)
  }
}

describe('App', () => {
  beforeEach(() => {
    RecordingWebSocket.instances = []
    vi.stubGlobal('WebSocket', RecordingWebSocket)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ version: '0.1.0' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the header with the app name', async () => {
    render(<App />)
    expect(
      screen.getByRole('heading', { name: 'Straticate' }),
    ).toBeInTheDocument()
    expect(screen.getByText('Extricate the layers')).toBeInTheDocument()
    // Wait for the backend indicator to settle to avoid act() warnings.
    expect(await screen.findByText(/backend v0\.1\.0/)).toBeInTheDocument()
  })

  it('renders the workspace with the initial workflow phase', async () => {
    render(<App />)
    expect(screen.getByRole('main')).toBeInTheDocument()
    expect(screen.getByText('Select')).toBeInTheDocument()
    await screen.findByText(/backend v0\.1\.0/)
  })

  it('opens the job event socket once for the session', async () => {
    const { unmount } = render(<App />)
    await screen.findByText(/backend v0\.1\.0/)

    expect(RecordingWebSocket.instances).toHaveLength(1)
    expect(RecordingWebSocket.instances[0]?.url).toMatch(
      /^ws:\/\/.+\/api\/v1\/ws$/,
    )

    unmount()
    expect(RecordingWebSocket.instances[0]?.closed).toBe(true)
  })
})
