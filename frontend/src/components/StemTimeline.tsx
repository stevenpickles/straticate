/**
 * The stem player's transport surface: one waveform lane per stem on a shared
 * time axis, a playhead, and a seek control that is also the timeline itself.
 *
 * ## Region map
 *
 * The markup is divided into regions so the four features that build on this
 * one attach without touching each other's areas:
 *
 * ```text
 * .stem-timeline
 * ├── .stem-timeline-headers        one row per stem, aligned to the lanes
 * │   ├── .stem-timeline-toolbar      corner cell   → 051: zoom controls
 * │   └── .stem-timeline-lane-header  name, status, Mute/Solo,
 * │                                   .stem-timeline-lane-fader (054)
 * └── .stem-timeline-tracks         the strip; its width is the viewport
 *     ├── .stem-timeline-ruler        tick labels   → 053: loop-region drag
 *     ├── .stem-timeline-lanes        one canvas per stem
 *     ├── .stem-timeline-playhead     transform-moved div
 *     └── .stem-timeline-surface      role="slider" → 052: audible scrub
 * ```
 *
 * ## The invariants it inherits from feature 023
 *
 * **Exactly one `seek` per pointer gesture.** Seeking restarts every
 * `AudioBufferSourceNode` in the mix; doing that on each `pointermove` is
 * dozens of teardown/rebuild cycles a second, each opening a fresh scheduling
 * lookahead, which is audible gapping rather than scrubbing. So a drag moves
 * only what is *displayed* — the playhead and the readout — and the seek is
 * committed once, on release, through a ref that is cleared **synchronously**
 * so a duplicate release event cannot commit a second one. Feature 052 adds
 * the audible preview by extending the same three gesture functions
 * (`beginGesture` / `updateGesture` / `commitGesture`); it does not need
 * another seek path.
 *
 * **The playhead is not a repaint.** It is one absolutely-positioned div moved
 * with `transform: translateX(…)` from the position the player already updates
 * on animation frames — a compositor-only change. The canvases repaint only
 * when what they *draw* changes (peaks, viewport, dpr, audibility), never per
 * frame.
 *
 * ## Accessibility
 *
 * The transparent interaction layer **is** the seek control: `role="slider"`,
 * focusable, with `aria-valuenow` / `aria-valuetext` tracking the playhead and
 * arrow/Home/End/Space keyboard transport. The canvases and the ruler are
 * `aria-hidden` — they are a picture of the same information.
 */

import {
  useCallback,
  useRef,
  type KeyboardEvent,
  type PointerEvent,
} from 'react'
import type { StemLoadStatus, StemPlayerEngine } from '../audio/engine'
import { formatDuration } from '../format'
import { LANE_HEIGHT_PX, TimelineLane } from './TimelineLane'
import { TimelineRuler } from './TimelineRuler'
import { xToTime, timeToX } from './timelineGeometry'
import { useTimelineGeometry } from './useTimelineGeometry'
import { useWaveformPeaks } from './useWaveformPeaks'
import './StemTimeline.css'

/** Height of the time ruler, in CSS pixels. */
const RULER_HEIGHT_PX = 22

/** Seconds an arrow key moves the playhead. */
const FINE_STEP_SECONDS = 1

/** Seconds a shifted arrow key moves the playhead. */
const COARSE_STEP_SECONDS = 5

/** One stem, as the timeline needs to see it. */
export interface StemTimelineStem {
  /** The stem's contract name. */
  readonly name: string
  /** Whether its audio loaded, is still loading, or failed. */
  readonly status: StemLoadStatus
  /** Whether the user muted it. */
  readonly muted: boolean
  /** Whether the user soloed it. */
  readonly soloed: boolean
  /** Whether it is currently heard, after mute/solo resolution. */
  readonly audible: boolean
  /**
   * Playback level in `0..1`, independent of mute/solo — a muted or
   * soloed-out stem still reports its true level, because `audible` is what
   * carries whether it is currently heard.
   */
  readonly level: number
  /** Its own length in seconds, which may be shorter than the axis. */
  readonly durationSeconds: number
}

/** Props for {@link StemTimeline}. */
export interface StemTimelineProps {
  /** Every stem to give a lane, in the order the result served them. */
  readonly stems: readonly StemTimelineStem[]
  /** Full extent of the shared time axis — the longest stem. */
  readonly durationSeconds: number
  /**
   * Where to draw the playhead: the audio clock normally, the drag position
   * while a gesture is in flight. The player owns that choice.
   */
  readonly positionSeconds: number
  /** Whether the transport can be driven yet. */
  readonly ready: boolean
  /** The engine whose decoded buffers the lanes are drawn from. */
  readonly engine: StemPlayerEngine | null
  /** A drag moved: update the displayed position, do not seek. */
  readonly onScrub: (seconds: number) => void
  /** A drag was cancelled: drop the displayed position. */
  readonly onScrubCancel: () => void
  /** Commit a seek. Fires **once** per pointer gesture, and once per keypress. */
  readonly onSeek: (seconds: number) => void
  /** Space on the focused timeline. */
  readonly onTogglePlayback: () => void
  /** Mute button in a lane header. */
  readonly onToggleMute: (name: string) => void
  /** Solo button in a lane header. */
  readonly onToggleSolo: (name: string) => void
  /**
   * The level fader in a lane header. Fires on every `change` event, not on
   * release: unlike a seek, a level change is a gain write, not a source
   * rebuild, so there is no teardown/reschedule cost to batch against — the
   * fader can (and should) drive `engine.setLevel` continuously.
   */
  readonly onSetLevel: (name: string, value: number) => void
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) {
    return min
  }
  return Math.min(Math.max(value, min), max)
}

/**
 * The header's second line: how long the stem is, or how far off that is.
 * A failed stem gets none — its lane already says "Unavailable" across the
 * full width, and saying it twice on one row is noise.
 */
function statusLabel(stem: StemTimelineStem): string | null {
  if (stem.status === 'error') {
    return null
  }
  return stem.status === 'loaded'
    ? formatDuration(stem.durationSeconds)
    : 'Loading…'
}

/** The waveform timeline: lanes, ruler, playhead and the seek control. */
export function StemTimeline({
  stems,
  durationSeconds,
  positionSeconds,
  ready,
  engine,
  onScrub,
  onScrubCancel,
  onSeek,
  onTogglePlayback,
  onToggleMute,
  onToggleSolo,
  onSetLevel,
}: StemTimelineProps) {
  const { viewport, devicePixelRatio, trackRef } =
    useTimelineGeometry(durationSeconds)
  const peaks = useWaveformPeaks(
    engine,
    stems.filter((stem) => stem.status === 'loaded').map((stem) => stem.name),
  )

  /**
   * The live gesture's position, or `null` when no pointer is down. A ref
   * rather than state because it must be readable and clearable *within* one
   * event handler, before React has re-rendered anything — that is what makes
   * a second release event a no-op instead of a second seek.
   */
  const gesture = useRef<number | null>(null)

  const secondsAt = useCallback(
    (event: PointerEvent<HTMLDivElement>): number => {
      const rect = event.currentTarget.getBoundingClientRect()
      return xToTime(viewport, event.clientX - rect.left)
    },
    [viewport],
  )

  const beginGesture = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (!ready) {
        return
      }
      const seconds = secondsAt(event)
      gesture.current = seconds
      // Capture so a drag that leaves the strip keeps arriving here. jsdom
      // implements neither the method nor `pointerId`, hence the guard.
      const surface = event.currentTarget
      if (typeof surface.setPointerCapture === 'function') {
        try {
          surface.setPointerCapture(event.pointerId)
        } catch {
          // A pointer the browser has already released; the gesture still
          // works, it just stops tracking outside the element.
        }
      }
      onScrub(seconds)
    },
    [ready, secondsAt, onScrub],
  )

  const updateGesture = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (gesture.current === null) {
        return
      }
      const seconds = secondsAt(event)
      gesture.current = seconds
      onScrub(seconds)
    },
    [secondsAt, onScrub],
  )

  const commitGesture = useCallback(() => {
    const seconds = gesture.current
    if (seconds === null) {
      return
    }
    // Cleared before the seek, synchronously: this is the whole mechanism.
    gesture.current = null
    onSeek(seconds)
  }, [onSeek])

  const abandonGesture = useCallback(() => {
    if (gesture.current === null) {
      return
    }
    gesture.current = null
    onScrubCancel()
  }, [onScrubCancel])

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (!ready) {
        return
      }
      const step = event.shiftKey ? COARSE_STEP_SECONDS : FINE_STEP_SECONDS
      const move = (seconds: number): void => {
        event.preventDefault()
        // A keyboard commit ends any pointer gesture still in flight: were
        // the ref left set, the eventual pointerup would find it and fire a
        // second, stale seek over this one.
        gesture.current = null
        onSeek(clamp(seconds, 0, durationSeconds))
      }
      switch (event.key) {
        case 'ArrowLeft':
          move(positionSeconds - step)
          return
        case 'ArrowRight':
          move(positionSeconds + step)
          return
        case 'Home':
          move(0)
          return
        case 'End':
          move(durationSeconds)
          return
        case ' ':
          // The transport is where a media keyboard user expects it, and the
          // timeline is the element they are already focused on.
          event.preventDefault()
          onTogglePlayback()
          return
        default:
      }
    },
    [ready, positionSeconds, durationSeconds, onSeek, onTogglePlayback],
  )

  const playheadX = clamp(
    timeToX(viewport, positionSeconds),
    0,
    viewport.widthPx,
  )

  return (
    <div className="stem-timeline">
      <div className="stem-timeline-headers">
        {/* Corner cell, aligned with the ruler: feature 051's toolbar. */}
        <div
          className="stem-timeline-toolbar"
          style={{ height: `${String(RULER_HEIGHT_PX)}px` }}
        />
        {stems.map((stem) => (
          <div
            className="stem-timeline-lane-header"
            key={stem.name}
            style={{ height: `${String(LANE_HEIGHT_PX)}px` }}
          >
            <div className="stem-timeline-lane-label">
              <span className="stem-player-stem-name">{stem.name}</span>
              {statusLabel(stem) !== null && (
                <span className="stem-player-stem-detail">
                  {statusLabel(stem)}
                </span>
              )}
            </div>
            <div className="stem-timeline-lane-controls">
              <button
                type="button"
                className="stem-player-toggle"
                aria-label={`Mute ${stem.name}`}
                aria-pressed={stem.muted}
                disabled={stem.status !== 'loaded'}
                onClick={() => {
                  onToggleMute(stem.name)
                }}
              >
                Mute
              </button>
              <button
                type="button"
                className="stem-player-toggle"
                aria-label={`Solo ${stem.name}`}
                aria-pressed={stem.soloed}
                disabled={stem.status !== 'loaded'}
                onClick={() => {
                  onToggleSolo(stem.name)
                }}
              >
                Solo
              </button>
            </div>
            <input
              type="range"
              className="stem-timeline-lane-fader"
              min={0}
              max={1}
              step={0.01}
              value={stem.level}
              aria-label={`${stem.name} level`}
              aria-valuetext={`${String(Math.round(stem.level * 100))}%`}
              disabled={stem.status !== 'loaded'}
              onChange={(event) => {
                onSetLevel(stem.name, Number(event.target.value))
              }}
            />
          </div>
        ))}
      </div>

      <div className="stem-timeline-tracks" ref={trackRef}>
        <div
          className="stem-timeline-ruler-row"
          style={{ height: `${String(RULER_HEIGHT_PX)}px` }}
        >
          <TimelineRuler viewport={viewport} />
        </div>

        <div className="stem-timeline-lanes">
          {stems.map((stem) => (
            <div
              className={
                stem.audible
                  ? 'stem-timeline-lane'
                  : 'stem-timeline-lane stem-timeline-lane-silenced'
              }
              key={stem.name}
              style={{ height: `${String(LANE_HEIGHT_PX)}px` }}
            >
              {stem.status === 'error' ? (
                <p className="stem-timeline-lane-placeholder">Unavailable</p>
              ) : (
                <TimelineLane
                  name={stem.name}
                  peaks={peaks.get(stem.name) ?? null}
                  viewport={viewport}
                  devicePixelRatio={devicePixelRatio}
                  audible={stem.audible}
                  stemDurationSeconds={stem.durationSeconds}
                />
              )}
            </div>
          ))}
        </div>

        <div
          aria-hidden="true"
          className="stem-timeline-playhead"
          data-testid="stem-timeline-playhead"
          style={{ transform: `translateX(${String(playheadX)}px)` }}
        />

        <div
          className="stem-timeline-surface"
          role="slider"
          tabIndex={0}
          aria-label="Seek"
          aria-orientation="horizontal"
          aria-valuemin={0}
          aria-valuemax={durationSeconds}
          aria-valuenow={Math.round(positionSeconds * 100) / 100}
          aria-valuetext={`${formatDuration(positionSeconds)} of ${formatDuration(durationSeconds)}`}
          aria-disabled={!ready}
          style={{ top: `${String(RULER_HEIGHT_PX)}px` }}
          onPointerDown={beginGesture}
          onPointerMove={updateGesture}
          onPointerUp={commitGesture}
          onPointerCancel={abandonGesture}
          onKeyDown={handleKeyDown}
        />
      </div>
    </div>
  )
}
