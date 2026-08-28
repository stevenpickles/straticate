/**
 * The timeline's time axis: absolutely-positioned tick labels along the top of
 * the track stack.
 *
 * DOM rather than canvas, deliberately. The ruler is a handful of short text
 * runs that change only when the viewport does, so the browser's text
 * rendering is both sharper and cheaper than painting glyphs ourselves — and
 * it leaves the ruler as a real box that feature 053 can hang a loop-region
 * drag off, instead of a picture of one.
 *
 * **Everything about a tick comes from the viewport**, which is what makes a
 * zoomed or panned window label itself: `tickStepSeconds` picks the spacing
 * from the ladder at the current scale, the first tick is the first multiple
 * of that spacing at or after `scrollSeconds`, and each label is placed with
 * `timeToX`, which subtracts the scroll. Nothing here assumes the window
 * starts at zero or spans the file.
 *
 * `aria-hidden`, like the lanes: the timeline's accessible surface is the seek
 * slider in `StemTimeline`, whose `aria-valuetext` says the same thing in a
 * form a screen reader can act on. A row of loose numbers would only add
 * noise.
 */

import { formatDuration } from '../format'
import {
  pxPerSecond,
  tickStepSeconds,
  timeToX,
  visibleSeconds,
  type TimelineViewport,
} from './timelineGeometry'

/** Props for {@link TimelineRuler}. */
export interface TimelineRulerProps {
  /** The window the ruler labels. */
  readonly viewport: TimelineViewport
}

/**
 * Ceiling on how many labels are laid out, whatever the arithmetic says.
 * `tickStepSeconds` already keeps marks about 64 px apart, so this can only
 * be reached by a degenerate viewport — and a degenerate
 * viewport should render a poor ruler, not thousands of DOM nodes.
 */
const MAX_TICKS = 256

/**
 * How close to the right edge a tick has to be before its label is hung to the
 * *left* of its mark instead of the right. A label that starts within this
 * much of the edge would otherwise be clipped by the strip's `overflow:
 * hidden` — which is exactly what happened to `1:00` on a fitted minute-long
 * mix, since the last tick of a fitted view sits on the final pixel.
 */
const LABEL_WIDTH_PX = 40

/** The tick times visible in `viewport`, coarsest step that still fits. */
function tickTimes(viewport: TimelineViewport): number[] {
  if (pxPerSecond(viewport) <= 0) {
    return []
  }
  const step = tickStepSeconds(viewport)
  const end = Math.min(
    viewport.durationSeconds,
    viewport.scrollSeconds + visibleSeconds(viewport),
  )
  const first = Math.ceil(viewport.scrollSeconds / step)
  const times: number[] = []
  for (let index = first; times.length < MAX_TICKS; index += 1) {
    const time = index * step
    if (time > end) {
      break
    }
    times.push(time)
  }
  return times
}

/** The time axis for a {@link TimelineViewport}. */
export function TimelineRuler({ viewport }: TimelineRulerProps) {
  return (
    <div className="stem-timeline-ruler" aria-hidden="true">
      {tickTimes(viewport).map((time) => {
        const x = timeToX(viewport, time)
        const flush = x > viewport.widthPx - LABEL_WIDTH_PX
        return (
          <span
            className={
              flush
                ? 'stem-timeline-tick stem-timeline-tick-flush'
                : 'stem-timeline-tick'
            }
            key={time}
            style={{ transform: `translateX(${String(x)}px)` }}
          >
            {formatDuration(time)}
          </span>
        )
      })}
    </div>
  )
}
