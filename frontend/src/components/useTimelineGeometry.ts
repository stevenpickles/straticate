/**
 * The timeline's viewport state: how wide the strip is, how sharp the display
 * is, and which window of the file is on screen.
 *
 * Feature 049 made the *arithmetic* of a viewport pure (`timelineGeometry.ts`).
 * This hook is the small amount of browser state that arithmetic needs — a
 * measured width, a device pixel ratio, and the zoom/scroll pair — kept in one
 * place so the components below it can stay declarative.
 *
 * **Zoom is fixed at 1 in feature 050**, which is what makes the whole file fit
 * the strip. The state is nevertheless the full `{ zoom, scrollSeconds }` pair
 * and the only way to change it is {@link TimelineGeometry.applyToViewport},
 * which takes one of 049's pure transforms (`zoomedAt`, `panned`) and stores
 * its result. Feature 051 adds the controls that call it; nothing else in the
 * timeline has to change for that.
 *
 * **Width is observed, not guessed.** One `ResizeObserver` on the track stack
 * writes `widthPx`, and the callback ref also measures once on attach so the
 * first paint is not blank (and so jsdom, which has no `ResizeObserver`, still
 * produces a usable width from a stubbed `getBoundingClientRect`).
 *
 * **Device pixel ratio is watched, not sampled.** `window.devicePixelRatio` has
 * no change event; the standard trick is a `matchMedia('(resolution: Ndppx)')`
 * query that stops matching when the ratio moves, re-armed at the new ratio
 * each time it fires — dragging a window between a retina and a non-retina
 * display then re-renders the canvases at the right backing-store size.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { clampViewport, type TimelineViewport } from './timelineGeometry'

/** A pure viewport transform, as `timelineGeometry.ts` exports them. */
export type ViewportTransform = (current: TimelineViewport) => TimelineViewport

/** What {@link useTimelineGeometry} hands back. */
export interface TimelineGeometry {
  /** The current, always-legal viewport. */
  readonly viewport: TimelineViewport
  /** Backing-store scale for every canvas in the timeline. */
  readonly devicePixelRatio: number
  /**
   * Callback ref for the element whose width *is* the timeline strip. Attach
   * it to exactly one element; attaching it elsewhere moves the observer.
   */
  readonly trackRef: (element: HTMLElement | null) => void
  /**
   * Apply one of 049's pure transforms to the viewport and keep the result.
   * The window it stores is `{ zoom, scrollSeconds }` only — duration and
   * width always come from the arguments and the observer, so a resize or a
   * new file can never leave a stale window behind. **Feature 051's seam.**
   */
  readonly applyToViewport: (transform: ViewportTransform) => void
}

/** The window into the material, independent of how wide the strip is. */
interface TimelineWindow {
  readonly zoom: number
  readonly scrollSeconds: number
}

/** Whole file, from the start: the only window feature 050 ever shows. */
const WHOLE_FILE: TimelineWindow = { zoom: 1, scrollSeconds: 0 }

/** The display's pixel ratio, defaulting to 1 wherever it is unreadable. */
function readDevicePixelRatio(): number {
  if (typeof window === 'undefined') {
    return 1
  }
  const ratio = window.devicePixelRatio
  return Number.isFinite(ratio) && ratio > 0 ? ratio : 1
}

/** Viewport state for a timeline over `durationSeconds` of material. */
export function useTimelineGeometry(durationSeconds: number): TimelineGeometry {
  const [widthPx, setWidthPx] = useState(0)
  const [timelineWindow, setTimelineWindow] =
    useState<TimelineWindow>(WHOLE_FILE)
  const [devicePixelRatio, setDevicePixelRatio] = useState(readDevicePixelRatio)
  const observerRef = useRef<ResizeObserver | null>(null)

  const trackRef = useCallback((element: HTMLElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (element === null) {
      return
    }
    // Measured once on attach as well as observed: the first paint should not
    // wait for the observer's first callback, and jsdom never sends one.
    setWidthPx(element.getBoundingClientRect().width)
    if (typeof ResizeObserver === 'undefined') {
      return
    }
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry !== undefined) {
        setWidthPx(entry.contentRect.width)
      }
    })
    observer.observe(element)
    observerRef.current = observer
  }, [])

  useEffect(() => {
    if (
      typeof window === 'undefined' ||
      typeof window.matchMedia !== 'function'
    ) {
      return
    }
    let query: MediaQueryList | null = null
    let cancelled = false
    const detach = (): void => {
      if (query !== null && typeof query.removeEventListener === 'function') {
        query.removeEventListener('change', rearm)
      }
      query = null
    }
    function rearm(): void {
      if (cancelled) {
        return
      }
      const ratio = readDevicePixelRatio()
      setDevicePixelRatio(ratio)
      detach()
      // The query matches *now* and stops matching the moment the ratio
      // moves, which is the only notification the platform offers.
      query = window.matchMedia(`(resolution: ${String(ratio)}dppx)`)
      if (typeof query.addEventListener === 'function') {
        query.addEventListener('change', rearm)
      }
    }
    rearm()
    return () => {
      cancelled = true
      detach()
    }
  }, [])

  // Memoised on the four numbers it is made of, not rebuilt per render: the
  // lanes are `React.memo`d over this object, so a fresh identity on every
  // parent render would repaint every canvas on every mute toggle.
  const viewport = useMemo(
    () =>
      clampViewport({
        durationSeconds,
        widthPx,
        zoom: timelineWindow.zoom,
        scrollSeconds: timelineWindow.scrollSeconds,
      }),
    [durationSeconds, widthPx, timelineWindow],
  )

  const applyToViewport = useCallback(
    (transform: ViewportTransform) => {
      setTimelineWindow((current) => {
        const next = transform(
          clampViewport({
            durationSeconds,
            widthPx,
            zoom: current.zoom,
            scrollSeconds: current.scrollSeconds,
          }),
        )
        return { zoom: next.zoom, scrollSeconds: next.scrollSeconds }
      })
    },
    [durationSeconds, widthPx],
  )

  return { viewport, devicePixelRatio, trackRef, applyToViewport }
}
