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
 * │   ├── .stem-timeline-toolbar      corner cell: zoom in/out/fit (051)
 * │   └── .stem-timeline-lane-header  name, status, Mute/Solo,
 * │                                   .stem-timeline-lane-fader (054)
 * └── .stem-timeline-tracks         the strip; its width is the viewport
 *     ├── .stem-timeline-ruler        tick labels   → 053: loop-region drag
 *     ├── .stem-timeline-scrollbar    the scroll thumb (051)
 *     ├── .stem-timeline-lanes        one canvas per stem
 *     ├── .stem-timeline-playhead     transform-moved div
 *     └── .stem-timeline-surface      role="slider" → 052: audible scrub
 * ```
 *
 * ## Zoom and pan (feature 051)
 *
 * The window is `useTimelineGeometry`'s and nothing here writes it directly:
 * Ctrl+wheel, the toolbar's three buttons, `+`/`-` on the focused surface, a
 * plain or shifted wheel, the scroll thumb and the playhead's own auto-follow
 * all go through that hook's named movements, which clamp. Four consequences
 * are worth knowing before changing any of them:
 *
 * - **The wheel listener is native and non-passive.** React's synthetic
 *   `onWheel` is registered passively, so `preventDefault` inside it is
 *   ignored and Ctrl+wheel zooms the *browser* instead of the timeline. The
 *   listener is therefore attached by hand to the track strip, and removed
 *   with it.
 * - **There is no `overflow-x`.** The canvases are viewport-sized rather than
 *   file-sized (see `TimelineLane`), so there is nothing for the browser to
 *   scroll; `.stem-timeline-scrollbar` is a thumb whose width and offset
 *   mirror the window, dragged with the same synchronously-cleared-ref idiom
 *   as the seek gesture.
 * - **Buttons and keys zoom about the playhead** when it is on screen, and
 *   about the middle of the window when it is not — the point the user is
 *   looking at in each case. A wheel zoom anchors on the cursor instead.
 * - **Auto-follow is a page flip, and it never fights a pan.** It moves only
 *   when the *position* has changed and left the window, so panning away from
 *   a paused playhead stays where it was put; when playback then carries the
 *   playhead out of view the window jumps so it reappears just inside the left
 *   edge, which is one flip per window rather than a repaint every frame.
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
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
} from 'react'
import type { StemLoadStatus, StemPlayerEngine } from '../audio/engine'
import { formatDuration } from '../format'
import { LANE_HEIGHT_PX, TimelineLane } from './TimelineLane'
import { TimelineRuler } from './TimelineRuler'
import {
  maxZoom,
  pxPerSecond,
  visibleSeconds,
  xToTime,
  timeToX,
} from './timelineGeometry'
import { useTimelineGeometry } from './useTimelineGeometry'
import { useWaveformPeaks, useWaveformTiles } from './useWaveformPeaks'
import './StemTimeline.css'

/** Height of the time ruler, in CSS pixels. */
const RULER_HEIGHT_PX = 22

/** Height of the scroll thumb's track, in CSS pixels. */
const SCROLLBAR_HEIGHT_PX = 10

/** Seconds an arrow key moves the playhead. */
const FINE_STEP_SECONDS = 1

/** Seconds a shifted arrow key moves the playhead. */
const COARSE_STEP_SECONDS = 5

/**
 * Where auto-follow puts the playhead when it has to flip the window: a tenth
 * of the way in, so the flip leaves most of the window ahead of the audio and
 * a little of what was just heard behind it.
 */
const FOLLOW_MARGIN = 0.1

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
  const {
    viewport,
    devicePixelRatio,
    trackRef,
    zoomIn,
    zoomOut,
    zoomToFit,
    panBy,
    scrollTo,
  } = useTimelineGeometry(durationSeconds)
  const loaded = stems.filter((stem) => stem.status === 'loaded')
  const peaks = useWaveformPeaks(
    engine,
    loaded.map((stem) => stem.name),
  )
  const tiles = useWaveformTiles(engine, loaded, viewport)

  /** The playhead's offset on the strip, which may be off either end of it. */
  const playheadOffsetX = timeToX(viewport, positionSeconds)

  /**
   * What a control with no cursor position of its own zooms about: the
   * playhead while it is on screen, the middle of the window otherwise.
   */
  const zoomAnchorX =
    playheadOffsetX >= 0 && playheadOffsetX <= viewport.widthPx
      ? playheadOffsetX
      : viewport.widthPx / 2

  const canZoomIn = viewport.zoom < maxZoom(durationSeconds)
  const canZoomOut = viewport.zoom > 1

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

  // -------------------------------------------------------------------------
  // Zoom and pan
  // -------------------------------------------------------------------------

  /**
   * The track strip, once it is mounted. State rather than a ref because the
   * wheel listener has to be attached to it in an effect, and an effect cannot
   * be woken by a ref changing.
   */
  const [tracks, setTracks] = useState<HTMLElement | null>(null)

  const attachTracks = useCallback(
    (element: HTMLDivElement | null) => {
      trackRef(element)
      setTracks(element)
    },
    [trackRef],
  )

  const handleWheel = useCallback(
    (event: WheelEvent, element: HTMLElement) => {
      if (viewport.durationSeconds <= 0 || viewport.widthPx <= 0) {
        return
      }
      if (event.ctrlKey || event.metaKey) {
        // The gesture the platform reserves for zooming — and the reason this
        // listener is registered non-passively, since letting it through
        // zooms the whole page instead.
        event.preventDefault()
        const anchorX = event.clientX - element.getBoundingClientRect().left
        if (event.deltaY < 0) {
          zoomIn(anchorX)
        } else if (event.deltaY > 0) {
          zoomOut(anchorX)
        }
        return
      }
      if (viewport.zoom <= 1) {
        // Nothing to pan: the whole file is already on screen, so the wheel
        // belongs to the page.
        return
      }
      const scale = pxPerSecond(viewport)
      // A trackpad's horizontal delta if it sent one — a shifted wheel usually
      // arrives that way already — and its vertical delta otherwise, because
      // the only axis this timeline has is time.
      const deltaPx = event.deltaX === 0 ? event.deltaY : event.deltaX
      if (scale <= 0 || deltaPx === 0) {
        return
      }
      event.preventDefault()
      panBy(deltaPx / scale)
    },
    [viewport, zoomIn, zoomOut, panBy],
  )

  // The handler is held in a ref so the listener below is attached once per
  // element rather than re-attached on every viewport change.
  const wheelHandler = useRef(handleWheel)
  useEffect(() => {
    wheelHandler.current = handleWheel
  }, [handleWheel])

  useEffect(() => {
    if (tracks === null) {
      return
    }
    const listener = (event: WheelEvent): void => {
      wheelHandler.current(event, tracks)
    }
    tracks.addEventListener('wheel', listener, { passive: false })
    return () => {
      tracks.removeEventListener('wheel', listener)
    }
  }, [tracks])

  /**
   * Where the scroll thumb was grabbed, or `null` when it is not being
   * dragged. The same ref idiom as the seek gesture — cleared synchronously on
   * release — though scrolling is not seeking, so this one is free to act on
   * every move.
   */
  const thumbDrag = useRef<{ pointerX: number; scrollSeconds: number } | null>(
    null,
  )

  const beginThumbDrag = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (viewport.durationSeconds <= 0 || viewport.widthPx <= 0) {
        return
      }
      thumbDrag.current = {
        pointerX: event.clientX,
        scrollSeconds: viewport.scrollSeconds,
      }
      const thumb = event.currentTarget
      if (typeof thumb.setPointerCapture === 'function') {
        try {
          thumb.setPointerCapture(event.pointerId)
        } catch {
          // Same as the seek surface: the drag still works, it just stops
          // tracking once the pointer leaves the element.
        }
      }
    },
    [viewport],
  )

  const updateThumbDrag = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const start = thumbDrag.current
      if (start === null) {
        return
      }
      // The thumb's track spans the whole file across the strip's width, so a
      // pixel of travel is always the same number of seconds whatever the zoom.
      const secondsPerPx = viewport.durationSeconds / viewport.widthPx
      scrollTo(
        start.scrollSeconds + (event.clientX - start.pointerX) * secondsPerPx,
      )
    },
    [viewport, scrollTo],
  )

  const endThumbDrag = useCallback(() => {
    thumbDrag.current = null
  }, [])

  /**
   * Keep a moving playhead in view, and only a moving one. The position has to
   * have *changed* for this to fire, so a pan that leaves the playhead behind
   * is left alone until playback next carries it out of the window.
   */
  const followedPosition = useRef(positionSeconds)
  useEffect(() => {
    const previous = followedPosition.current
    followedPosition.current = positionSeconds
    if (positionSeconds === previous || viewport.zoom <= 1) {
      return
    }
    const visible = visibleSeconds(viewport)
    if (
      positionSeconds >= viewport.scrollSeconds &&
      positionSeconds <= viewport.scrollSeconds + visible
    ) {
      return
    }
    scrollTo(positionSeconds - visible * FOLLOW_MARGIN)
  }, [positionSeconds, viewport, scrollTo])

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      // Zoom is a view control, not a transport one: it works while the stems
      // are still decoding, which is when a user is most likely to be looking
      // around the picture rather than driving it.
      if (event.key === '+' || event.key === '=') {
        event.preventDefault()
        zoomIn(zoomAnchorX)
        return
      }
      if (event.key === '-' || event.key === '_') {
        event.preventDefault()
        zoomOut(zoomAnchorX)
        return
      }
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
    [
      ready,
      positionSeconds,
      durationSeconds,
      onSeek,
      onTogglePlayback,
      zoomIn,
      zoomOut,
      zoomAnchorX,
    ],
  )

  const playheadX = clamp(playheadOffsetX, 0, viewport.widthPx)

  /** The thumb mirrors the window: how much of the file, and how far in. */
  const windowFraction =
    viewport.durationSeconds > 0
      ? visibleSeconds(viewport) / viewport.durationSeconds
      : 1
  const scrollFraction =
    viewport.durationSeconds > 0
      ? viewport.scrollSeconds / viewport.durationSeconds
      : 0

  return (
    <div className="stem-timeline">
      <div className="stem-timeline-headers">
        {/*
          Corner cell, as tall as the ruler and the scroll thumb together so
          the header column and the lane column stay aligned to the pixel.
        */}
        <div
          className="stem-timeline-toolbar"
          style={{
            height: `${String(RULER_HEIGHT_PX + SCROLLBAR_HEIGHT_PX)}px`,
          }}
        >
          <button
            type="button"
            className="stem-timeline-zoom"
            aria-label="Zoom out"
            disabled={!canZoomOut}
            onClick={() => {
              zoomOut(zoomAnchorX)
            }}
          >
            −
          </button>
          <button
            type="button"
            className="stem-timeline-zoom"
            aria-label="Zoom in"
            disabled={!canZoomIn}
            onClick={() => {
              zoomIn(zoomAnchorX)
            }}
          >
            +
          </button>
          <button
            type="button"
            className="stem-timeline-zoom stem-timeline-zoom-fit"
            aria-label="Zoom to fit"
            disabled={!canZoomOut}
            onClick={zoomToFit}
          >
            Fit
          </button>
        </div>
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

      {/*
        The window is on the strip itself as data, because it is what the
        ruler, the lanes, the thumb and every seek are derived from — one
        attribute pair that says which seconds are on screen.
      */}
      <div
        className="stem-timeline-tracks"
        ref={attachTracks}
        data-zoom={String(Math.round(viewport.zoom * 1000) / 1000)}
        data-scroll-seconds={String(
          Math.round(viewport.scrollSeconds * 1000) / 1000,
        )}
      >
        <div
          className="stem-timeline-ruler-row"
          style={{ height: `${String(RULER_HEIGHT_PX)}px` }}
        >
          <TimelineRuler viewport={viewport} />
        </div>

        <div
          aria-hidden="true"
          className="stem-timeline-scrollbar"
          style={{ height: `${String(SCROLLBAR_HEIGHT_PX)}px` }}
        >
          <div
            className="stem-timeline-scroll-thumb"
            data-testid="stem-timeline-scroll-thumb"
            style={{
              left: `${String(scrollFraction * 100)}%`,
              width: `${String(windowFraction * 100)}%`,
            }}
            onPointerDown={beginThumbDrag}
            onPointerMove={updateThumbDrag}
            onPointerUp={endThumbDrag}
            onPointerCancel={endThumbDrag}
          />
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
                  tile={tiles.get(stem.name) ?? null}
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
          style={{
            top: `${String(RULER_HEIGHT_PX + SCROLLBAR_HEIGHT_PX)}px`,
          }}
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
