import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { getSystemStorage } from '../api/system'
import type { StorageReport } from '../api/types'
import {
  DiskSpaceProvider,
  STORAGE_MAX_AGE_MS,
  useDiskSpace,
  type DiskSpaceHandle,
} from './diskSpace'

vi.mock('../api/system')

const getSystemStorageMock = vi.mocked(getSystemStorage)

/** A consumer that reads on mount, exactly as `DiskCostNotice` does. */
function Reader({ label = 'reader' }: { readonly label?: string }) {
  const space = useDiskSpace()
  const { ensureRead } = space
  useEffect(() => {
    ensureRead()
  }, [ensureRead])
  return (
    <p data-testid={label}>
      {space.status}:{space.freeBytes ?? 'null'}/{space.totalBytes ?? 'null'}
    </p>
  )
}

/** A consumer that never asks for a figure. */
function Bystander() {
  useDiskSpace()
  return <p data-testid="bystander">nothing to install here</p>
}

/** Hands the handle out so a test can drive `noteDiskChanged` itself. */
function Probe({
  onHandle,
}: {
  readonly onHandle: (h: DiskSpaceHandle) => void
}) {
  const space = useDiskSpace()
  onHandle(space)
  return null
}

function reading(
  freeBytes: number | null,
  totalBytes: number | null = null,
): StorageReport {
  return { free_bytes: freeBytes, total_bytes: totalBytes }
}

beforeEach(() => {
  getSystemStorageMock.mockReset()
  getSystemStorageMock.mockResolvedValue(reading(2_000, 10_000))
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('DiskSpaceProvider', () => {
  it('asks for nothing until something offers an install', async () => {
    render(
      <DiskSpaceProvider>
        <Bystander />
      </DiskSpaceProvider>,
    )

    expect(await screen.findByTestId('bystander')).toBeInTheDocument()
    expect(getSystemStorageMock).not.toHaveBeenCalled()
  })

  it('reads once when an install is offered', async () => {
    render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:2000/10000')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
  })

  it('collapses simultaneous mounts into a single request', async () => {
    render(
      <DiskSpaceProvider>
        <Reader label="a" />
        <Reader label="b" />
        <Reader label="c" />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('a')).toHaveTextContent('known')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('c')).toHaveTextContent('known:2000/10000')
  })

  it('reports the backend’s documented unknown as unavailable, not as zero', async () => {
    getSystemStorageMock.mockResolvedValue(reading(null, null))
    render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent(
        'unavailable:null/null',
      )
    })
  })

  it('reports a failed request as unavailable too', async () => {
    getSystemStorageMock.mockRejectedValue(new Error('offline'))
    render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('unavailable')
    })
  })

  it('a full disk is a known figure of zero', async () => {
    getSystemStorageMock.mockResolvedValue(reading(0, 10_000))
    render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:0/10000')
    })
  })

  it('re-reads when the disk demonstrably changed', async () => {
    const held: { current: DiskSpaceHandle | null } = { current: null }
    render(
      <DiskSpaceProvider>
        <Reader />
        <Probe
          onHandle={(h) => {
            held.current = h
          }}
        />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:2000')
    })

    getSystemStorageMock.mockResolvedValue(reading(1_000, 10_000))
    await act(async () => {
      held.current?.noteDiskChanged()
    })

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:1000')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(2)
  })

  it('keeps the figure on screen while it is being re-read', async () => {
    const held: { current: DiskSpaceHandle | null } = { current: null }
    render(
      <DiskSpaceProvider>
        <Reader />
        <Probe
          onHandle={(h) => {
            held.current = h
          }}
        />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:2000')
    })

    // A read that never settles: the held figure must not blank out under it,
    // or a comparison the user is reading would vanish for a round trip.
    getSystemStorageMock.mockReturnValue(new Promise(() => undefined))
    act(() => {
      held.current?.noteDiskChanged()
    })

    expect(screen.getByTestId('reader')).toHaveTextContent('known:2000')
  })

  it('takes a fresh reading when a change lands mid-request', async () => {
    // The read in flight was started before the disk changed, so its answer
    // describes the world as it was; another one has to follow it.
    const first: {
      resolve: ((value: StorageReport) => void) | null
    } = { resolve: null }
    getSystemStorageMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          first.resolve = resolve
        }),
    )
    const held: { current: DiskSpaceHandle | null } = { current: null }
    render(
      <DiskSpaceProvider>
        <Reader />
        <Probe
          onHandle={(h) => {
            held.current = h
          }}
        />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
    })

    act(() => {
      held.current?.noteDiskChanged()
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)

    getSystemStorageMock.mockResolvedValue(reading(1_000, 10_000))
    await act(async () => {
      first.resolve?.(reading(2_000, 10_000))
    })

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:1000')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(2)
  })

  it('reuses a fresh reading when the next install is offered', async () => {
    // Leaving the model library and coming back — or switching quality tier —
    // remounts the notice. A figure taken seconds ago is still the answer.
    const { rerender } = render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
    })

    rerender(
      <DiskSpaceProvider>
        <Bystander />
      </DiskSpaceProvider>,
    )
    rerender(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByTestId('reader')).toHaveTextContent('known:2000')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
  })

  it('re-reads for an affordance that appears once the figure has gone stale', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { rerender } = render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
    })

    // Nothing is scheduled — the clock moving on its own changes nothing.
    await act(async () => {
      vi.advanceTimersByTime(STORAGE_MAX_AGE_MS * 4)
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)

    // It is the *next install offered* that reads again.
    rerender(
      <DiskSpaceProvider>
        <Reader />
        <Reader label="later" />
      </DiskSpaceProvider>,
    )
    await waitFor(() => {
      expect(getSystemStorageMock).toHaveBeenCalledTimes(2)
    })
  })

  it('sets no timer of its own', async () => {
    vi.useFakeTimers()
    render(
      <DiskSpaceProvider>
        <Reader />
      </DiskSpaceProvider>,
    )
    await act(async () => {
      await Promise.resolve()
    })

    // AGENTS.md principle 3, and the reasoning 025 and 035 recorded: no poll.
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('useDiskSpace without a provider', () => {
  it('is unavailable and asks for nothing', async () => {
    render(<Reader />)

    expect(await screen.findByTestId('reader')).toHaveTextContent(
      'unavailable:null/null',
    )
    expect(getSystemStorageMock).not.toHaveBeenCalled()
  })
})
