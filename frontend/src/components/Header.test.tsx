import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Header } from './Header'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Header', () => {
  it('shows the backend version when getVersion() resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ version: '1.2.3' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    render(<Header />)
    expect(await screen.findByText('backend v1.2.3')).toBeInTheDocument()
  })

  it('shows a graceful unavailable state when getVersion() rejects', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    )

    render(<Header />)
    expect(await screen.findByText('backend unavailable')).toBeInTheDocument()
  })
})
