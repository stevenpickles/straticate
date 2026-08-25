import { describe, expect, it } from 'vitest'
import { installPercent, toPercent } from './installProgress'
import { sampleWeightsBytes } from '../test/fixtures'
import type { ModelInstallation } from '../api/types'

/** An `installation` block with only the fields these helpers read. */
function block(patch: Partial<ModelInstallation>): ModelInstallation {
  return {
    state: 'downloading',
    requires_download: true,
    total_bytes: sampleWeightsBytes,
    downloaded_bytes: null,
    progress: null,
    error: null,
    ...patch,
  }
}

describe('toPercent', () => {
  it('rounds a 0..1 fraction to a whole percentage', () => {
    expect(toPercent(0.25)).toBe(25)
    expect(toPercent(0.606)).toBe(61)
  })

  it('clamps out-of-range and non-finite values rather than rendering them', () => {
    expect(toPercent(1.4)).toBe(100)
    expect(toPercent(-1)).toBe(0)
    expect(toPercent(Number.NaN)).toBe(0)
  })
})

describe('installPercent', () => {
  it('prefers the backend’s own progress figure', () => {
    expect(installPercent(block({ progress: 0.25 }))).toBe(25)
  })

  it('falls back to the byte counts the figure is computed from', () => {
    expect(
      installPercent(
        block({ downloaded_bytes: sampleWeightsBytes / 2, progress: null }),
      ),
    ).toBe(50)
  })

  it('is indeterminate rather than zero when neither is known', () => {
    // A bar at 0% claims the transfer has not started; `null` says the
    // fraction is unknown, which is the truth.
    expect(
      installPercent(block({ progress: null, total_bytes: null })),
    ).toBeNull()
    expect(installPercent(null)).toBeNull()
    expect(installPercent(undefined)).toBeNull()
  })

  it('does not divide by a zero total', () => {
    expect(
      installPercent(block({ total_bytes: 0, downloaded_bytes: 0 })),
    ).toBeNull()
  })
})
