import { describe, expect, it, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

describe('Header model library control', () => {
  function stubVersion(): void {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ version: '1.2.3' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
  }

  it('offers the way into the model library from every workflow phase', async () => {
    stubVersion()
    const onToggle = vi.fn()
    render(<Header onToggleLibrary={onToggle} />)

    const button = screen.getByRole('button', { name: 'Models' })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(button)
    expect(onToggle).toHaveBeenCalledTimes(1)
    await screen.findByText('backend v1.2.3')
  })

  it('announces and labels itself as the way back out while it is open', async () => {
    stubVersion()
    render(<Header libraryOpen onToggleLibrary={vi.fn()} />)

    const button = screen.getByRole('button', { name: 'Close models' })
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(button).toHaveAttribute('aria-controls', 'model-library')
    await screen.findByText('backend v1.2.3')
  })

  it('points at nothing rather than at an absent element when closed', async () => {
    stubVersion()
    render(<Header />)

    expect(screen.getByRole('button', { name: 'Models' })).not.toHaveAttribute(
      'aria-controls',
    )
    await screen.findByText('backend v1.2.3')
  })
})
