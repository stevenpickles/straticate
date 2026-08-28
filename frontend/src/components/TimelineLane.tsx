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
 *    arithmetic pass over a few thousand buckets.
 * 2. **It repaints on state, not on frames.** The effect's dependencies are
 *    exactly the things that change what is painted — peaks, viewport, device
 *    pixel ratio, audibility, the stem's own length. The playhead moves 60
 *    times a second and never touches this component; it is a transformed div
 *    in {@link StemTimeline}.
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
  timeToX,
  visibleSeconds,
  type TimelineViewport,
} from './timelineGeometry'

/**
 * Height of one waveform lane, in CSS pixels. It is also the height of the
 * lane's header row, which is what keeps the two columns aligned — so it has
 * to leave room for a stem name and a pair of toggles.
 */
export const LANE_HEIGHT_PX = 64

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
  /** The shared window every lane is drawn against. */
  readonly viewport: TimelineViewport
  /** Backing-store scale. */
  readonly devicePixelRatio: number
  /** Whether the stem is currently heard, after mute/solo resolution. */
  readonly audible: boolean
  /** This stem's own length, which may be shorter than the axis. */
  readonly stemDurationSeconds: number
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
  viewport,
  devicePixelRatio,
  audible,
  stemDurationSeconds,
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
    canvas.height = Math.max(1, Math.round(LANE_HEIGHT_PX * devicePixelRatio))
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
    // …and the window, expressed as fractions of the stem's own peak set.
    const startFraction = viewport.scrollSeconds / stemDurationSeconds
    const endFraction =
      (viewport.scrollSeconds + visibleSeconds(viewport)) / stemDurationSeconds
    const visible = downsamplePeaks(peaks, startFraction, endFraction, columns)

    drawWaveform(
      context,
      visible,
      stemWidthPx,
      LANE_HEIGHT_PX,
      devicePixelRatio,
      resolveColor(canvas, audible ? AUDIBLE_COLOR : SILENCED_COLOR),
    )
  }, [peaks, viewport, devicePixelRatio, audible, stemDurationSeconds])

  return (
    <canvas
      aria-hidden="true"
      className="stem-timeline-canvas"
      data-stem={name}
      ref={canvasRef}
      style={{ height: `${String(LANE_HEIGHT_PX)}px` }}
    />
  )
}

/**
 * One stem's waveform lane. Memoised: every prop is a primitive or an
 * immutable value the parent keeps stable, so a snapshot change that does not
 * touch this stem costs nothing.
 */
export const TimelineLane = memo(TimelineLaneImpl)
