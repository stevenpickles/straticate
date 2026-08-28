import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  clampViewport,
  drawWaveform,
  maxZoom,
  needsHighResTile,
  panned,
  pxPerSecond,
  sameTileRange,
  tickStepSeconds,
  tileRangeFor,
  timeToX,
  visibleSeconds,
  xToTime,
  zoomedAt,
  type TimelineViewport,
  type WaveformDrawContext,
} from './timelineGeometry'
import type { PeakBuckets } from '../audio/peaks'
import {
  FakeCanvasContext2D,
  installFakeCanvas,
} from '../test/fakeCanvasContext'

/** A three-minute file across an 800 px strip, fitted. */
function viewport(overrides: Partial<TimelineViewport> = {}): TimelineViewport {
  return {
    durationSeconds: 180,
    widthPx: 800,
    zoom: 1,
    scrollSeconds: 0,
    ...overrides,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('timeline scale', () => {
  it('fits the whole file at zoom 1', () => {
    expect(visibleSeconds(viewport())).toBe(180)
    expect(pxPerSecond(viewport())).toBeCloseTo(800 / 180, 10)
  })

  it('halves the visible window for every doubling of zoom', () => {
    expect(visibleSeconds(viewport({ zoom: 2 }))).toBe(90)
    expect(visibleSeconds(viewport({ zoom: 4 }))).toBe(45)
  })

  it('has no scale at all before a duration is known', () => {
    expect(visibleSeconds(viewport({ durationSeconds: 0 }))).toBe(0)
    expect(pxPerSecond(viewport({ durationSeconds: 0 }))).toBe(0)
    expect(pxPerSecond(viewport({ widthPx: 0 }))).toBe(0)
  })
})

describe('time and pixel conversion', () => {
  it('round-trips a time through x and back', () => {
    const view = viewport({ zoom: 6, scrollSeconds: 42 })

    for (const seconds of [42, 45.5, 50, 59, 71.9]) {
      expect(xToTime(view, timeToX(view, seconds))).toBeCloseTo(seconds, 9)
    }
  })

  it('puts the scroll position at the left edge', () => {
    expect(timeToX(viewport({ zoom: 3, scrollSeconds: 30 }), 30)).toBe(0)
  })

  it('places a time outside the window outside the strip', () => {
    const view = viewport({ zoom: 3, scrollSeconds: 60 })

    expect(timeToX(view, 30)).toBeLessThan(0)
    expect(timeToX(view, 179)).toBeGreaterThan(view.widthPx)
  })

  it('clamps a click past either end to the material', () => {
    const view = viewport()

    expect(xToTime(view, -400)).toBe(0)
    expect(xToTime(view, 100_000)).toBe(180)
  })

  it('reads the scroll position when there is no scale', () => {
    expect(xToTime(viewport({ durationSeconds: 0 }), 400)).toBe(0)
  })
})

describe('clampViewport', () => {
  it('leaves a legal viewport alone', () => {
    const view = viewport({ zoom: 4, scrollSeconds: 30 })
    expect(clampViewport(view)).toEqual(view)
  })

  it('refuses to zoom out past the whole file', () => {
    expect(clampViewport(viewport({ zoom: 0.25 })).zoom).toBe(1)
  })

  it('stops zooming in when the window reaches one second', () => {
    const clamped = clampViewport(viewport({ zoom: 10_000 }))

    expect(clamped.zoom).toBe(180)
    expect(visibleSeconds(clamped)).toBe(1)
  })

  it('cannot zoom a file shorter than the minimum window', () => {
    const clamped = clampViewport(
      viewport({ durationSeconds: 0.4, zoom: 50, scrollSeconds: 0.2 }),
    )

    expect(clamped.zoom).toBe(1)
    expect(clamped.scrollSeconds).toBe(0)
  })

  it('holds the scroll between the start and the last full window', () => {
    expect(
      clampViewport(viewport({ zoom: 4, scrollSeconds: -20 })).scrollSeconds,
    ).toBe(0)
    // Zoom 4 shows 45 s, so the furthest left edge is 135 s.
    expect(
      clampViewport(viewport({ zoom: 4, scrollSeconds: 999 })).scrollSeconds,
    ).toBe(135)
  })

  it('pins the scroll to zero when the whole file fits', () => {
    expect(clampViewport(viewport({ scrollSeconds: 50 })).scrollSeconds).toBe(0)
  })

  it('treats a non-finite zoom as fitted', () => {
    expect(clampViewport(viewport({ zoom: Number.NaN })).zoom).toBe(1)
  })
})

describe('zoomedAt', () => {
  it('keeps the time under the anchor under the anchor', () => {
    const view = viewport({ zoom: 2, scrollSeconds: 30 })
    const anchorX = 600
    const before = xToTime(view, anchorX)

    const zoomed = zoomedAt(view, 2, anchorX)

    expect(zoomed.zoom).toBe(4)
    expect(xToTime(zoomed, anchorX)).toBeCloseTo(before, 9)
  })

  it('holds the anchor through a zoom out as well', () => {
    const view = viewport({ zoom: 8, scrollSeconds: 60 })
    const anchorX = 200
    const before = xToTime(view, anchorX)

    const zoomed = zoomedAt(view, 0.5, anchorX)

    expect(zoomed.zoom).toBe(4)
    expect(xToTime(zoomed, anchorX)).toBeCloseTo(before, 9)
  })

  it('stops at the start when the anchor is near the beginning', () => {
    const zoomed = zoomedAt(viewport({ zoom: 2, scrollSeconds: 0 }), 0.5, 40)

    expect(zoomed.zoom).toBe(1)
    expect(zoomed.scrollSeconds).toBe(0)
  })

  it('stops at the end when the anchor is near it', () => {
    const view = viewport({ zoom: 4, scrollSeconds: 135 })

    const zoomed = zoomedAt(view, 0.5, 790)

    expect(zoomed.zoom).toBe(2)
    expect(zoomed.scrollSeconds).toBe(90)
  })

  it('respects the zoom limits', () => {
    expect(zoomedAt(viewport({ zoom: 100 }), 100, 400).zoom).toBe(180)
    expect(zoomedAt(viewport({ zoom: 1 }), 0.01, 400).zoom).toBe(1)
  })
})

describe('panned', () => {
  it('moves the window by the given number of seconds', () => {
    const view = viewport({ zoom: 4, scrollSeconds: 30 })
    expect(panned(view, 12).scrollSeconds).toBe(42)
    expect(panned(view, -12).scrollSeconds).toBe(18)
  })

  it('stops at both ends', () => {
    const view = viewport({ zoom: 4, scrollSeconds: 30 })
    expect(panned(view, -1000).scrollSeconds).toBe(0)
    expect(panned(view, 1000).scrollSeconds).toBe(135)
  })

  it('ignores a non-finite delta', () => {
    const view = viewport({ zoom: 4, scrollSeconds: 30 })
    expect(panned(view, Number.NaN).scrollSeconds).toBe(30)
  })
})

describe('tickStepSeconds', () => {
  it('picks a coarse step when the whole file is fitted', () => {
    // 800 px over 180 s is 4.44 px/s; 15 s is the first step past 64 px.
    expect(tickStepSeconds(viewport())).toBe(15)
  })

  it('picks a finer step as the view zooms in', () => {
    expect(tickStepSeconds(viewport({ zoom: 4 }))).toBe(5)
    expect(tickStepSeconds(viewport({ zoom: 20 }))).toBe(1)
    expect(tickStepSeconds(viewport({ zoom: 180 }))).toBe(0.1)
  })

  it('falls back to the coarsest step for an hour-long file', () => {
    expect(tickStepSeconds(viewport({ durationSeconds: 36_000 }))).toBe(1800)
  })

  it('returns a usable step with no scale at all', () => {
    expect(tickStepSeconds(viewport({ durationSeconds: 0 }))).toBe(1800)
  })
})

describe('needsHighResTile', () => {
  const sampleRate = 44_100

  it('is false while the view matches the base resolution', () => {
    expect(needsHighResTile(viewport(), sampleRate, 800)).toBe(false)
  })

  it('is true once a pixel covers fewer samples than a base bucket', () => {
    expect(needsHighResTile(viewport({ zoom: 2 }), sampleRate, 800)).toBe(true)
  })

  it('is false when the base peaks are finer than the strip', () => {
    expect(needsHighResTile(viewport({ zoom: 2 }), sampleRate, 4000)).toBe(
      false,
    )
  })

  it('is false for a viewport or buffer that cannot be drawn', () => {
    expect(
      needsHighResTile(viewport({ durationSeconds: 0 }), sampleRate, 800),
    ).toBe(false)
    expect(needsHighResTile(viewport(), 0, 800)).toBe(false)
    expect(needsHighResTile(viewport(), sampleRate, 0)).toBe(false)
  })
})

describe('maxZoom', () => {
  it('is the zoom at which one second fills the strip', () => {
    expect(maxZoom(180)).toBe(180)
    // …and it is where `clampViewport` stops, which is what a control that
    // greys itself out at the limit relies on.
    expect(clampViewport(viewport({ zoom: 10_000 })).zoom).toBe(maxZoom(180))
  })

  it('is 1 for material no longer than the minimum window', () => {
    expect(maxZoom(1)).toBe(1)
    expect(maxZoom(0.4)).toBe(1)
    expect(maxZoom(0)).toBe(1)
  })
})

describe('tileRangeFor', () => {
  it('is the window itself for a stem that spans the axis', () => {
    expect(tileRangeFor(viewport({ zoom: 4, scrollSeconds: 45 }), 180)).toEqual(
      { startSeconds: 45, endSeconds: 90, buckets: 800 },
    )
  })

  it('stops at a stem that ends inside the window', () => {
    // Half a minute of stem against a 45 s window from the start: it covers
    // the first two thirds of the strip and nothing after that.
    expect(tileRangeFor(viewport({ zoom: 4 }), 30)).toEqual({
      startSeconds: 0,
      endSeconds: 30,
      buckets: 533,
    })
  })

  it('is nothing at all for a stem that has ended before the window', () => {
    expect(
      tileRangeFor(viewport({ zoom: 4, scrollSeconds: 90 }), 30),
    ).toBeNull()
    expect(tileRangeFor(viewport({ widthPx: 0 }), 180)).toBeNull()
  })

  it('answers the same range for the same window', () => {
    const view = viewport({ zoom: 6, scrollSeconds: 12 })

    expect(
      sameTileRange(tileRangeFor(view, 180), tileRangeFor({ ...view }, 180)),
    ).toBe(true)
    // A pan of a single second is a different picture, cache key and all.
    expect(
      sameTileRange(
        tileRangeFor(view, 180),
        tileRangeFor(viewport({ zoom: 6, scrollSeconds: 13 }), 180),
      ),
    ).toBe(false)
    expect(sameTileRange(null, tileRangeFor(view, 180))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

/** Peaks with one bucket per entry of `mins`/`maxes`. */
function peaksOf(mins: number[], maxes: number[]): PeakBuckets {
  return {
    mins: Float32Array.from(mins),
    maxes: Float32Array.from(maxes),
    startSample: 0,
    endSample: mins.length,
  }
}

describe('drawWaveform', () => {
  it('sets the device-pixel transform and clears before painting', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([-1, -1], [1, 1]), 2, 100, 2, '#a1b2c3')

    expect(ctx.transforms).toEqual([{ a: 2, b: 0, c: 0, d: 2, e: 0, f: 0 }])
    expect(ctx.clearRects).toEqual([{ x: 0, y: 0, width: 2, height: 100 }])
  })

  it('draws one column per pixel in the passed colour', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([0, 0, 0, 0], [1, 1, 1, 1]), 4, 100, 1, '#ff0000')

    expect(ctx.fillRects).toHaveLength(4)
    expect(ctx.fillRects.map((rect) => rect.x)).toEqual([0, 1, 2, 3])
    expect(ctx.fillRects.every((rect) => rect.width === 1)).toBe(true)
    expect(ctx.fillRects.every((rect) => rect.fillStyle === '#ff0000')).toBe(
      true,
    )
  })

  it('draws a full-scale bucket around the midline with headroom', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([-1], [1]), 1, 100, 1, '#fff')

    // 92% of the 50 px half-height either side of the middle.
    expect(ctx.fillRects[0]).toEqual({
      x: 0,
      y: 4,
      width: 1,
      height: 92,
      fillStyle: '#fff',
    })
  })

  it('draws silence as a hairline rather than nothing', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([0, 0], [0, 0]), 2, 100, 1, '#fff')

    expect(ctx.fillRects.map((rect) => rect.height)).toEqual([1, 1])
    expect(ctx.fillRects.map((rect) => rect.y)).toEqual([50, 50])
  })

  it('draws an asymmetric bucket from its minimum to its maximum', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([-0.5], [0.25]), 1, 200, 1, '#fff')

    // Half-height 100, headroom 92: max 0.25 → 23 above, min -0.5 → 46 below.
    expect(ctx.fillRects[0]?.y).toBeCloseTo(77, 6)
    expect(ctx.fillRects[0]?.height).toBeCloseTo(69, 6)
  })

  it('spreads a bucket set across a wider strip', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([-1, 0], [1, 0]), 4, 100, 1, '#fff')

    expect(ctx.fillRects.map((rect) => rect.height)).toEqual([92, 92, 1, 1])
  })

  it('clears but paints nothing when there is nothing to paint', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([], []), 100, 40, 1, '#fff')
    drawWaveform(ctx, peaksOf([0], [0]), 0, 40, 1, '#fff')

    expect(ctx.clearRects).toHaveLength(2)
    expect(ctx.fillRects).toHaveLength(0)
  })

  it('falls back to a 1:1 transform for a nonsense device ratio', () => {
    const ctx = new FakeCanvasContext2D()

    drawWaveform(ctx, peaksOf([0], [0]), 1, 10, 0, '#fff')

    expect(ctx.transforms[0]?.a).toBe(1)
  })
})

describe('WaveformDrawContext', () => {
  it('is satisfied by a real CanvasRenderingContext2D', () => {
    // Compile-time, not run-time: if the widened `fillStyle` or any of the
    // four method signatures drifted from the DOM's, this would not build.
    type RealContextFits = CanvasRenderingContext2D extends WaveformDrawContext
      ? true
      : false
    const fits: RealContextFits = true

    expect(fits).toBe(true)
  })

  it('is what installFakeCanvas puts behind getContext', () => {
    const installed = installFakeCanvas()
    const canvas = document.createElement('canvas')

    const ctx = canvas.getContext('2d') as unknown as WaveformDrawContext
    drawWaveform(ctx, peaksOf([-1], [1]), 1, 100, 1, '#fff')

    expect(ctx).toBe(installed)
    expect(installed.fillRects).toHaveLength(1)
  })
})
