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
      {tickTimes(viewport).map((time) => (
        <span
          className="stem-timeline-tick"
          key={time}
          style={{
            transform: `translateX(${String(timeToX(viewport, time))}px)`,
          }}
        >
          {formatDuration(time)}
        </span>
      ))}
    </div>
  )
}
