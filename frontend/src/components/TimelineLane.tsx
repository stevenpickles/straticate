/**
 * One stem's waveform lane: a single viewport-sized canvas painted from that
 * stem's peak envelope.
 *
 * Three rules make this cheap enough to have four of on screen at once.
 *
 * 1. **The canvas is the size of the strip, never the size of the file.** A
 *    zoomed-in ten-minute mix would otherwise want a canvas tens of thousands
 *    of pixels wide per stem; instead the window is aggregated down to the
 *    strip's width on every viewport change (`downsamplePeaks`), which is an
 *    arithmetic pass over a few thousand buckets. Zoomed past the base peaks'
 *    own resolution the aggregation would be a *stretch* rather than a
 *    reduction, so feature 051 hands the lane a high-resolution `tile` of the
 *    visible window instead — same canvas, same columns, sharper source.
 * 2. **It repaints on state, not on frames.** The effect's dependencies are
 *    exactly the things that change what is painted — peaks, viewport, device
 *    pixel ratio, audibility, the stem's own length, and (067) the lane's
 *    height in actual pixels, which moves only when the lane's own rendered
 *    box does (measured directly, {@link useMeasuredHeight} in
 *    {@link StemTimeline} — not derived from a proxy signal; see that hook's
 *    docstring for why). The playhead moves 60 times a second and never
 *    touches this component; it is a transformed div in {@link StemTimeline}.
 * 3. **It is `React.memo`d over primitives.** The parent re-renders on every
 *    engine snapshot (a mute toggle, a transport change); with the props
 *    below, only the lane that actually changed re-renders, and only the lane
 *    that actually changed repaints.
 *
 * **A short stem draws short.** Every lane shares one time axis — the longest
 * stem's — so a stem that ends early paints across only its own fraction of
 * the width and leaves the rest of the lane empty, which is the honest picture
 * of "there is no audio here".
 */

import { memo, useEffect, useRef } from 'react'
import type { PeakBuckets } from '../audio/peaks'
import { downsamplePeaks } from '../audio/peaks'
import {
  drawWaveform,
  sameTileRange,
  tileRangeFor,
  timeToX,
  visibleSeconds,
  type TimelineViewport,
} from './timelineGeometry'
import type { WaveformTile } from './useWaveformPeaks'

/**
 * Height of one waveform lane, in `rem`. It is also the height of the lane's
 * header row, which is what keeps the two columns aligned — so it has to
 * leave room for a stem name, the Mute/Solo toggles, and (054) a level
 * fader.
 *
 * **`rem`, not a fixed pixel count, since feature 067.** Feature 050 shipped
 * this as `LANE_HEIGHT_PX = 64`, and by 054 the header's three rows filled it
 * to within about a pixel *at the default 16 px browser root font* — at
 * larger root fonts (a real accessibility setting, not a hypothetical one)
 * the rem-sized rows outgrew the fixed box and `overflow: hidden` clipped
 * them, worse the larger the root font. A `rem` height grows the *box* at
 * exactly the rate its rem-sized contents grow, so the slack measured at
 * 16 px holds at every root font instead of shrinking through zero.
 *
 * `4.75rem` is measured, not derived: it is the smallest quarter-`rem` step
 * that left every one of `.stem-timeline-lane-header`'s three rows unclipped
 * at every one of 16/17/18/20 px root fonts in a real Chromium, *after* the
 * header's own padding and row gap were retightened the same way 054 already
 * tightened them once (see `.stem-timeline-lane-header` in
 * `StemTimeline.css`) to fund the fader's WCAG-sized pointer target (see
 * `.stem-timeline-lane-fader`, same file) — a couple of pixels of margin
 * over the exact fit, deliberately, since font rendering is not identical
 * across platforms and this repository's CI runs a different one than any
 * one contributor's machine. The before/after measurements at all four root
 * sizes are in `docs/features/067-lane-height-a11y.md`.
 */
export const LANE_HEIGHT_REM = 4.75

/**
 * Colours, as CSS custom properties with hard fallbacks. The properties are
 * the app's design tokens (`index.css`); the fallbacks are their current
 * values, and they are what jsdom uses, since `getComputedStyle` there
 * resolves no custom property.
 */
const AUDIBLE_COLOR = { token: '--color-accent', fallback: '#7aa2f7' }
const SILENCED_COLOR = { token: '--color-text-muted', fallback: '#9a9aa5' }

/** Props for {@link TimelineLane}. */
export interface TimelineLaneProps {
  /** The stem this lane draws, for the canvas's label and React's key. */
  readonly name: string
  /** Whole-stem base peaks, or `null` while the stem is still decoding. */
  readonly peaks: PeakBuckets | null
  /**
   * A high-resolution tile of the visible window, computed from samples when
   * the zoom has gone past the base peaks' resolution (feature 051), or `null`
   * to draw from {@link TimelineLaneProps.peaks}. A tile whose range does not
   * match the current window is ignored rather than stretched: a stale picture
   * of the wrong seconds is worse than a coarse picture of the right ones.
   */
  readonly tile: WaveformTile | null
  /** The shared window every lane is drawn against. */
  readonly viewport: TimelineViewport
  /** Backing-store scale. */
  readonly devicePixelRatio: number
  /** Whether the stem is currently heard, after mute/solo resolution. */
  readonly audible: boolean
  /** This stem's own length, which may be shorter than the axis. */
  readonly stemDurationSeconds: number
  /**
   * The lane's own rendered box height, in actual CSS pixels — measured
   * directly off the box with `ResizeObserver` ({@link useMeasuredHeight} in
   * {@link StemTimeline}), not derived from {@link LANE_HEIGHT_REM} and a
   * proxy signal. What the canvas backing store needs, since
   * `canvas.width`/`canvas.height` are plain integers and take no part in
   * `rem`'s own scaling. It changes only when the measured box actually
   * changes, which is a state change like any other the draw effect below
   * already depends on (peaks, viewport, dpr, audibility) — the canvas still
   * never repaints per animation frame; the playhead moving 60 times a
   * second touches none of these.
   */
  readonly laneHeightPx: number
}

/** Resolve a design token off an element, falling back to its literal value. */
function resolveColor(
  element: Element,
  color: { token: string; fallback: string },
): string {
  const value = getComputedStyle(element).getPropertyValue(color.token).trim()
  return value === '' ? color.fallback : value
}

function TimelineLaneImpl({
  name,
  peaks,
  tile,
  viewport,
  devicePixelRatio,
  audible,
  stemDurationSeconds,
  laneHeightPx,
}: TimelineLaneProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (canvas === null) {
      return
    }
    const cssWidth = Math.max(0, Math.floor(viewport.widthPx))
    // Assigning `width`/`height` resets the backing store *and* the transform,
    // which is what guarantees a repaint never leaves a previous, longer
    // waveform showing past the end of a shorter one.
    canvas.width = Math.max(1, Math.round(cssWidth * devicePixelRatio))
    canvas.height = Math.max(1, Math.round(laneHeightPx * devicePixelRatio))
    if (peaks === null || cssWidth === 0 || stemDurationSeconds <= 0) {
      // Nothing to draw yet — and nothing asked of the canvas, which is what
      // keeps a stem that never decodes from reaching for a 2D context at all.
      return
    }

    // The stem's own extent, expressed on the shared axis…
    const stemWidthPx = Math.min(
      cssWidth,
      Math.max(0, timeToX(viewport, stemDurationSeconds)),
    )
    const columns = Math.floor(stemWidthPx)
    if (columns === 0) {
      return
    }
    const context = canvas.getContext('2d')
    if (context === null) {
      // jsdom with no rendering backend and no fake installed.
      return
    }
    // …and the window, from the sharpest source that covers it. A tile is
    // used only when it is a tile *of this window*; otherwise the base peaks
    // are aggregated as fractions of the stem's own peak set.
    const startFraction = viewport.scrollSeconds / stemDurationSeconds
    const endFraction =
      (viewport.scrollSeconds + visibleSeconds(viewport)) / stemDurationSeconds
    const visible =
      tile !== null &&
      sameTileRange(tile.range, tileRangeFor(viewport, stemDurationSeconds))
        ? tile.peaks
        : downsamplePeaks(peaks, startFraction, endFraction, columns)

    drawWaveform(
      context,
      visible,
      stemWidthPx,
      laneHeightPx,
      devicePixelRatio,
      resolveColor(canvas, audible ? AUDIBLE_COLOR : SILENCED_COLOR),
    )
  }, [
    peaks,
    tile,
    viewport,
    devicePixelRatio,
    audible,
    stemDurationSeconds,
    laneHeightPx,
  ])

  return (
    <canvas
      aria-hidden="true"
      className="stem-timeline-canvas"
      data-stem={name}
      ref={canvasRef}
      style={{ height: `${String(LANE_HEIGHT_REM)}rem` }}
    />
  )
}

/**
 * One stem's waveform lane. Memoised: every prop is a primitive or an
 * immutable value the parent keeps stable, so a snapshot change that does not
 * touch this stem costs nothing.
 */
export const TimelineLane = memo(TimelineLaneImpl)
