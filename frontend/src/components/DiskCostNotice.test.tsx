import { describe, expect, it } from 'vitest'
import { render, within } from '@testing-library/react'
import { DiskCostNotice, LARGE_DOWNLOAD_BYTES } from './DiskCostNotice'
import { sampleWeightsBytes } from '../test/fixtures'

/** The notice, as a user reads it. */
function renderNotice(totalBytes: number | null): HTMLElement {
  // Scoped to this render's own container, so a test may render the notice
  // more than once and still ask about one of them.
  const { container } = render(<DiskCostNotice totalBytes={totalBytes} />)
  return within(container).getByRole('note')
}

describe('DiskCostNotice', () => {
  it('names what the install will write, in the app’s own units', () => {
    expect(renderNotice(sampleWeightsBytes)).toHaveTextContent(
      '870 MB will be written to the machine running Straticate.',
    )
  })

  it('says plainly that the free space cannot be checked from here', () => {
    // The browser cannot see the backend's disk, and the one figure it *can*
    // obtain (`navigator.storage.estimate`) is about its own origin's quota —
    // a different number about a different disk. Saying so is the honest
    // option; omitting the subject is not.
    expect(renderNotice(sampleWeightsBytes)).toHaveTextContent(
      /cannot check that machine.s free space from the browser/i,
    )
  })

  it('says so when the model publishes no size at all', () => {
    const notice = renderNotice(null)
    expect(notice).toHaveTextContent('This model publishes no download size.')
    expect(notice).toHaveTextContent(/cannot check/i)
  })

  it('marks a large download as one worth pausing over', () => {
    expect(renderNotice(LARGE_DOWNLOAD_BYTES)).toHaveClass('disk-cost-large')
    expect(renderNotice(LARGE_DOWNLOAD_BYTES - 1)).not.toHaveClass(
      'disk-cost-large',
    )
  })

  it('treats an unpublished size as at least as serious as a large one', () => {
    expect(renderNotice(null)).toHaveClass('disk-cost-large')
  })
})
