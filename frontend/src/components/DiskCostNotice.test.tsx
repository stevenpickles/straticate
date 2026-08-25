import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { DiskCostNotice } from './DiskCostNotice'
import { LARGE_DOWNLOAD_BYTES, TIGHT_HEADROOM_BYTES, diskFit } from './diskFit'
import { DiskSpaceProvider } from '../state/diskSpace'
import { getSystemStorage } from '../api/system'
import { sampleWeightsBytes } from '../test/fixtures'

vi.mock('../api/system')

const getSystemStorageMock = vi.mocked(getSystemStorage)

/** A free-space answer from the backend. */
function storage(freeBytes: number | null, totalBytes: number | null = null) {
  return { free_bytes: freeBytes, total_bytes: totalBytes }
}

/**
 * The notice with no `DiskSpaceProvider` above it — which is how every
 * component test that mounts a card or a panel in isolation renders it, and
 * how the app behaves whenever no figure is available.
 */
function renderNotice(totalBytes: number | null): HTMLElement {
  // Scoped to this render's own container, so a test may render the notice
  // more than once and still ask about one of them.
  const { container } = render(<DiskCostNotice totalBytes={totalBytes} />)
  return within(container).getByRole('note')
}

/** The notice inside a provider, once the scripted read has landed. */
async function renderWithSpace(
  totalBytes: number | null,
  freeBytes: number | null,
): Promise<HTMLElement> {
  getSystemStorageMock.mockResolvedValue(storage(freeBytes, 512 * 1024 ** 3))
  render(
    <DiskSpaceProvider>
      <DiskCostNotice totalBytes={totalBytes} />
    </DiskSpaceProvider>,
  )
  const notice = screen.getByRole('note')
  await waitFor(() => {
    expect(notice).not.toHaveTextContent(/Checking how much space/i)
  })
  return notice
}

beforeEach(() => {
  getSystemStorageMock.mockReset()
  getSystemStorageMock.mockResolvedValue(storage(null))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('diskFit', () => {
  it('compares the download against the room it has', () => {
    expect(diskFit(1_000, 1_000 + TIGHT_HEADROOM_BYTES * 2)).toBe('fits')
    expect(diskFit(1_000, 999)).toBe('insufficient')
    expect(diskFit(1_000, 1_000 + TIGHT_HEADROOM_BYTES - 1)).toBe('tight')
    expect(diskFit(1_000, 1_000 + TIGHT_HEADROOM_BYTES)).toBe('fits')
  })

  it('treats an exact fit as tight rather than as room', () => {
    // Technically it fits; practically a machine with nothing left is a
    // machine about to have problems, and the same disk holds this app's
    // uploads, job outputs and exports.
    expect(diskFit(1_000, 1_000)).toBe('tight')
  })

  it('is unknown when either number is missing, and never "fine"', () => {
    expect(diskFit(null, 10_000)).toBe('unknown')
    expect(diskFit(1_000, null)).toBe('unknown')
    expect(diskFit(null, null)).toBe('unknown')
  })

  it('treats a full disk as a fact, not as an absent figure', () => {
    expect(diskFit(1_000, 0)).toBe('insufficient')
  })
})

describe('DiskCostNotice', () => {
  it('names what the install will write, in the app’s own units', async () => {
    expect(
      await renderWithSpace(sampleWeightsBytes, 40 * 1024 ** 3),
    ).toHaveTextContent(
      '870 MB will be written to the machine running Straticate.',
    )
  })

  it('turns the size into a comparison once the backend answers', async () => {
    const notice = await renderWithSpace(sampleWeightsBytes, 2.1 * 1024 ** 3)

    expect(notice).toHaveTextContent('870 MB will be written')
    expect(notice).toHaveTextContent('2.1 GB is free there.')
    expect(notice).not.toHaveTextContent(/cannot check/i)
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
  })

  it('says plainly when the download will not fit — and still does not stop it', async () => {
    const notice = await renderWithSpace(sampleWeightsBytes, 400 * 1024 * 1024)

    expect(notice).toHaveTextContent('400 MB is free there')
    expect(notice).toHaveTextContent('will not fit')
    expect(notice).toHaveTextContent(/starting it now will fail/i)
    expect(notice).toHaveClass('disk-cost-insufficient')
    // Nothing here is a control: refusing is the installer's business, and it
    // deliberately does not refuse (see the feature doc).
    expect(within(notice).queryByRole('button')).toBeNull()
  })

  it('a full disk is reported as no room, not as no answer', async () => {
    const notice = await renderWithSpace(sampleWeightsBytes, 0)

    expect(notice).toHaveTextContent('0 B is free there')
    expect(notice).toHaveTextContent('will not fit')
  })

  it('warns when it fits with nothing to spare', async () => {
    const notice = await renderWithSpace(
      sampleWeightsBytes,
      sampleWeightsBytes + 1024,
    )

    expect(notice).toHaveTextContent('will fit with little to spare')
    expect(notice).toHaveClass('disk-cost-large')
    expect(notice).not.toHaveClass('disk-cost-insufficient')
  })

  it('states the free space even when the model publishes no size', async () => {
    const notice = await renderWithSpace(null, 2 * 1024 ** 3)

    expect(notice).toHaveTextContent('This model publishes no download size.')
    expect(notice).toHaveTextContent('2 GB is free there.')
    // An unmeasured download could be anything, so it stays the cautious case
    // however much room there is.
    expect(notice).toHaveClass('disk-cost-large')
  })

  it('keeps 037’s honest wording when the host cannot answer', async () => {
    // The backend degrades to `null` rather than raising; the UI degrades to
    // the sentence feature 037 wrote for exactly this position.
    const notice = await renderWithSpace(sampleWeightsBytes, null)

    expect(notice).toHaveTextContent('870 MB will be written')
    expect(notice).toHaveTextContent(
      /Straticate cannot check that machine.s free space right now/i,
    )
    expect(notice).toHaveTextContent(
      /make sure there is room before installing/i,
    )
    expect(notice).toHaveClass('disk-cost-large')
  })

  it('says the same when the request itself fails', async () => {
    getSystemStorageMock.mockRejectedValue(new Error('offline'))
    render(
      <DiskSpaceProvider>
        <DiskCostNotice totalBytes={sampleWeightsBytes} />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getByRole('note')).toHaveTextContent(/cannot check/i)
    })
  })

  it('falls back to the honest wording with no provider, and asks for nothing', () => {
    // Every unit test that mounts a card or a panel in isolation is this case,
    // and so is any tree the provider does not cover: no figure, no request,
    // no comparison invented.
    const notice = renderNotice(sampleWeightsBytes)

    expect(notice).toHaveTextContent(/cannot check/i)
    expect(getSystemStorageMock).not.toHaveBeenCalled()
  })

  it('marks a large download as one worth pausing over', async () => {
    const roomy = 100 * 1024 ** 3
    expect(await renderWithSpace(LARGE_DOWNLOAD_BYTES, roomy)).toHaveClass(
      'disk-cost-large',
    )
  })

  it('leaves a small download that comfortably fits as a footnote', async () => {
    const notice = await renderWithSpace(
      LARGE_DOWNLOAD_BYTES - 1,
      100 * 1024 ** 3,
    )

    expect(notice).not.toHaveClass('disk-cost-large')
  })

  it('treats a small download as large again when the space is unknown', async () => {
    // Unknown is not "fine": the same reasoning 037 applied to an unmeasured
    // size applies to an unmeasured disk.
    const notice = await renderWithSpace(LARGE_DOWNLOAD_BYTES - 1, null)

    expect(notice).toHaveClass('disk-cost-large')
  })

  it('reads the figure once for the whole page, not once per install offered', async () => {
    getSystemStorageMock.mockResolvedValue(storage(2 * 1024 ** 3))
    render(
      <DiskSpaceProvider>
        <DiskCostNotice totalBytes={sampleWeightsBytes} />
        <DiskCostNotice totalBytes={sampleWeightsBytes} />
        <DiskCostNotice totalBytes={sampleWeightsBytes} />
      </DiskSpaceProvider>,
    )

    await waitFor(() => {
      expect(screen.getAllByRole('note')[0]).toHaveTextContent('2 GB is free')
    })
    expect(getSystemStorageMock).toHaveBeenCalledTimes(1)
  })
})
