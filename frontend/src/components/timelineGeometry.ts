/**
 * The arithmetic of a zoomable waveform timeline, and the drawing routine
 * that paints one.
 *
 * A timeline is a viewport onto a duration: a window of seconds mapped onto a
 * strip of pixels. Every question a timeline component asks — where does this
 * time sit, what time did the user click, where does the window go when they
 * zoom on a point, how far apart should the tick labels be — is that mapping,
 * and none of it needs a DOM. Keeping it here as pure functions over an
 * immutable {@link TimelineViewport} means the component that arrives in
 * feature 050 holds one small state object and asks this module everything
 * else, and the awkward cases (zoom anchored under the cursor, scrolling to
 * the very end, a duration shorter than the minimum window) are unit-tested
 * without rendering anything.
 *
 * **Zoom is a ratio, not a pixel count.** `zoom === 1` fits the whole file in
 * the viewport whatever its width, so a resize does not change what is on
 * screen, and the visible window is always `duration / zoom` seconds.
 *
 * **Drawing takes a structural context.** {@link drawWaveform} writes through
 * {@link WaveformDrawContext} — the four calls it makes on a 2D canvas
 * context — for the same reason the audio engine takes an
 * `AudioEngineContext`: jsdom has no canvas rendering, so a recording double
 * (`src/test/fakeCanvasContext.ts`) stands in and the tests assert on the
 * rectangles that *would* have been painted.
 */

import type { PeakBuckets } from '../audio/peaks'

/** An immutable view of what part of a file the timeline is showing. */
export interface TimelineViewport {
  /** Full extent of the material, in seconds. */
  readonly durationSeconds: number
  /** Width of the timeline strip, in CSS pixels. */
  readonly widthPx: number
  /** `1` fits the whole file; `n` shows `duration / n` seconds. */
  readonly zoom: number
  /** Seconds of material scrolled past the left edge. */
  readonly scrollSeconds: number
}

/**
 * The narrowest window zooming may produce, in seconds. A second of audio
 * across the full width is already far past sample-per-pixel on any real
 * file, and stopping there keeps `pxPerSecond` finite.
 */
const MIN_VISIBLE_SECONDS = 1

/** Tick spacings a timeline may use, coarsest last. */
const TICK_LADDER = [
  0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800,
] as const

/** Minimum gap between tick marks, in pixels, before the next step up. */
const MIN_TICK_SPACING_PX = 64

/** Fraction of the half-height a waveform is allowed to fill. */
const WAVEFORM_HEADROOM = 0.92

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) {
    return min
  }
  return Math.min(Math.max(value, min), max)
}

/** Seconds of material on screen: the whole file at `zoom === 1`. */
export function visibleSeconds(viewport: TimelineViewport): number {
  const duration = Math.max(0, viewport.durationSeconds)
  const zoom = Math.max(1, viewport.zoom)
  if (duration <= 0 || !Number.isFinite(zoom)) {
    return 0
  }
  return duration / zoom
}

/** Pixels per second at the current zoom, or `0` when nothing is visible. */
export function pxPerSecond(viewport: TimelineViewport): number {
  const visible = visibleSeconds(viewport)
  if (visible <= 0 || viewport.widthPx <= 0) {
    return 0
  }
  return viewport.widthPx / visible
}

/**
 * The x offset of `seconds` from the strip's left edge. Times outside the
 * window map outside `[0, widthPx)`, deliberately: a caller that wants the
 * playhead clipped is better placed to decide that than this function.
 */
export function timeToX(viewport: TimelineViewport, seconds: number): number {
  return (seconds - viewport.scrollSeconds) * pxPerSecond(viewport)
}

/** The time under an x offset, clamped to `[0, duration]`. */
export function xToTime(viewport: TimelineViewport, x: number): number {
  const duration = Math.max(0, viewport.durationSeconds)
  const scale = pxPerSecond(viewport)
  if (scale <= 0) {
    return clamp(viewport.scrollSeconds, 0, duration)
  }
  return clamp(viewport.scrollSeconds + x / scale, 0, duration)
}

/** The largest zoom that keeps the window at or above the minimum. */
function maxZoom(durationSeconds: number): number {
  const duration = Math.max(0, durationSeconds)
  if (duration <= MIN_VISIBLE_SECONDS) {
    // A file shorter than the minimum window cannot be zoomed at all: it
    // already fits, and zooming in would show more window than material.
    return 1
  }
  return duration / MIN_VISIBLE_SECONDS
}

/**
 * The nearest legal viewport: zoom within `[1, maxZoom]` and scroll within
 * `[0, duration - visible]`, so the window can never show past either end nor
 * shrink below {@link MIN_VISIBLE_SECONDS}.
 */
export function clampViewport(viewport: TimelineViewport): TimelineViewport {
  const durationSeconds = Math.max(0, viewport.durationSeconds)
  const widthPx = Math.max(0, viewport.widthPx)
  const zoom = clamp(viewport.zoom, 1, maxZoom(durationSeconds))
  const visible = visibleSeconds({ ...viewport, durationSeconds, zoom })
  const scrollSeconds = clamp(
    viewport.scrollSeconds,
    0,
    Math.max(0, durationSeconds - visible),
  )
  return { durationSeconds, widthPx, zoom, scrollSeconds }
}

/**
 * Zoom by `factor` about the point under `anchorX`.
 *
 * The time under the anchor is read first and put back under the anchor
 * afterwards, which is what makes wheel-zoom feel attached to the cursor
 * rather than to the left edge. The result is clamped, so an anchor near an
 * end simply stops there.
 */
export function zoomedAt(
  viewport: TimelineViewport,
  factor: number,
  anchorX: number,
): TimelineViewport {
  const anchorTime = xToTime(viewport, anchorX)
  const zoom = clamp(
    viewport.zoom * factor,
    1,
    maxZoom(viewport.durationSeconds),
  )
  const zoomed = { ...viewport, zoom }
  const anchorFraction = viewport.widthPx > 0 ? anchorX / viewport.widthPx : 0
  const scrollSeconds = anchorTime - anchorFraction * visibleSeconds(zoomed)
  return clampViewport({ ...zoomed, scrollSeconds })
}

/** Scroll by `deltaSeconds`, clamped to the material. */
export function panned(
  viewport: TimelineViewport,
  deltaSeconds: number,
): TimelineViewport {
  const delta = Number.isFinite(deltaSeconds) ? deltaSeconds : 0
  return clampViewport({
    ...viewport,
    scrollSeconds: viewport.scrollSeconds + delta,
  })
}

/**
 * The finest tick spacing from the ladder that still leaves about
 * {@link MIN_TICK_SPACING_PX} between marks — the coarsest step when even
 * that is too tight, so a caller always gets a number it can label.
 */
export function tickStepSeconds(viewport: TimelineViewport): number {
  const scale = pxPerSecond(viewport)
  const coarsest = TICK_LADDER[TICK_LADDER.length - 1] ?? 1
  if (scale <= 0) {
    return coarsest
  }
  return (
    TICK_LADDER.find((step) => step * scale >= MIN_TICK_SPACING_PX) ?? coarsest
  )
}

/**
 * Whether the view is zoomed past the resolution of the whole-file peak set,
 * i.e. one pixel column now covers fewer samples than one base bucket does.
 * Above that point a repaint downsampled from the base peaks would draw a
 * blocky, stale envelope and the range needs recomputing from samples.
 */
export function needsHighResTile(
  viewport: TimelineViewport,
  sampleRate: number,
  baseBucketCount: number,
): boolean {
  const visible = visibleSeconds(viewport)
  const duration = Math.max(0, viewport.durationSeconds)
  if (
    visible <= 0 ||
    duration <= 0 ||
    viewport.widthPx <= 0 ||
    sampleRate <= 0 ||
    baseBucketCount <= 0
  ) {
    return false
  }
  const samplesPerPixel = (visible * sampleRate) / viewport.widthPx
  const samplesPerBaseBucket = (duration * sampleRate) / baseBucketCount
  return samplesPerPixel < samplesPerBaseBucket
}

/**
 * The subset of `CanvasRenderingContext2D` {@link drawWaveform} uses. A real
 * 2D context satisfies it; so does the recording double in
 * `src/test/fakeCanvasContext.ts`, which is the point.
 *
 * `fillStyle` carries the canvas union rather than `string` alone so a real
 * context assigns to this interface with no cast — the same rule the audio
 * engine's structural interfaces follow.
 */
export interface WaveformDrawContext {
  fillStyle: string | CanvasGradient | CanvasPattern
  clearRect(x: number, y: number, width: number, height: number): void
  fillRect(x: number, y: number, width: number, height: number): void
  setTransform(
    a: number,
    b: number,
    c: number,
    d: number,
    e: number,
    f: number,
  ): void
}

/**
 * Paint a peak envelope: one filled column per pixel, drawn from the bucket's
 * minimum to its maximum around the vertical midline.
 *
 * The transform is set from `dpr` rather than the caller scaling every
 * coordinate, so the whole routine works in CSS pixels while the backing
 * store stays sharp on a retina display; it is set (not multiplied) so a
 * repaint cannot accumulate scale.
 *
 * Two details keep the result readable. Columns reach only
 * {@link WAVEFORM_HEADROOM} of the half-height, so a full-scale sample has
 * somewhere to be rather than merging into the lane's edges. And every column
 * is at least one pixel tall, so silence draws as a hairline through the
 * middle instead of vanishing — a gap in a waveform should mean "no stem
 * here", never "this part is quiet".
 */
export function drawWaveform(
  ctx: WaveformDrawContext,
  peaks: PeakBuckets,
  widthPx: number,
  heightPx: number,
  dpr: number,
  color: string,
): void {
  const scale = Number.isFinite(dpr) && dpr > 0 ? dpr : 1
  ctx.setTransform(scale, 0, 0, scale, 0, 0)
  ctx.clearRect(0, 0, widthPx, heightPx)
  const columns = Math.max(0, Math.floor(widthPx))
  const buckets = peaks.mins.length
  if (columns === 0 || buckets === 0 || heightPx <= 0) {
    return
  }

  ctx.fillStyle = color
  const middle = heightPx / 2
  const half = middle * WAVEFORM_HEADROOM
  for (let column = 0; column < columns; column += 1) {
    // One bucket per column in the normal case; the mapping only matters
    // when a caller reuses a bucket set at a different width.
    const index = Math.min(
      buckets - 1,
      Math.floor((column * buckets) / columns),
    )
    const min = peaks.mins[index] ?? 0
    const max = peaks.maxes[index] ?? 0
    const top = middle - max * half
    const height = Math.max(1, (max - min) * half)
    ctx.fillRect(column, top, 1, height)
  }
}
