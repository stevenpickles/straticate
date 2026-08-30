/**
 * The timeline's viewport state: how wide the strip is, how sharp the display
 * is, and which window of the file is on screen.
 *
 * Feature 049 made the *arithmetic* of a viewport pure (`timelineGeometry.ts`).
 * This hook is the small amount of browser state that arithmetic needs — a
 * measured width, a device pixel ratio, and the zoom/scroll pair — kept in one
 * place so the components below it can stay declarative.
 *
 * **Every viewport change goes through here.** The state is the full
 * `{ zoom, scrollSeconds }` pair and the only way to change it is
 * {@link TimelineGeometry.applyToViewport}, which takes one of 049's pure
 * transforms (`zoomedAt`, `panned`) and stores its result. Feature 051 added
 * the five named movements the controls actually call — {@link
 * TimelineGeometry.zoomIn}, `zoomOut`, `zoomToFit`, `panBy` and `scrollTo` —
 * each of them one line over that same seam, so there is still exactly one
 * place a window can move and exactly one place it is clamped.
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
 *
 * **The window can outlive the hook (feature 065).** Pass a
 * {@link TimelineWindowStore} and the `{ zoom, scrollSeconds }` pair is seeded
 * from it on mount and written back through on every change, so a timeline
 * that is unmounted and mounted again — the Inspect UI left and re-entered —
 * comes back looking at the same seconds. The store is a plain ref pair held by
 * whoever owns the session, deliberately *not* React state: a wheel tick must
 * not re-render anything above the timeline. With no store the hook behaves
 * exactly as it did before, opening on the whole file.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  clampViewport,
  panned,
  zoomedAt,
  type TimelineViewport,
} from './timelineGeometry'

/**
 * How much one zoom step magnifies. A step of about half again is small enough
 * that a wheel notch feels continuous and large enough that a button click is
 * worth making — four of them are an order of magnitude.
 */
export const ZOOM_STEP = 1.5

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
  /**
   * Magnify by one {@link ZOOM_STEP} about `anchorX`, keeping the time under
   * that offset where it is. Anchoring defaults to the middle of the window,
   * which is what a control with no cursor position to offer should use.
   */
  readonly zoomIn: (anchorX?: number) => void
  /** Shrink by one {@link ZOOM_STEP} about `anchorX`, as {@link zoomIn}. */
  readonly zoomOut: (anchorX?: number) => void
  /** Back to the whole file, from the start. */
  readonly zoomToFit: () => void
  /** Scroll the window by `deltaSeconds`, clamped to the material. */
  readonly panBy: (deltaSeconds: number) => void
  /** Put `seconds` at the left edge of the window, clamped to the material. */
  readonly scrollTo: (seconds: number) => void
}

/** The window into the material, independent of how wide the strip is. */
export interface TimelineWindow {
  readonly zoom: number
  readonly scrollSeconds: number
}

/** Whole file, from the start: where a timeline opens, and where Fit goes. */
export const WHOLE_FILE: TimelineWindow = { zoom: 1, scrollSeconds: 0 }

/**
 * Somewhere for the window to live that is not this hook — see the module
 * docstring. Implemented over a ref by the owner of the session, so writing to
 * it costs no render; `get()` is read **once**, to seed the hook's state.
 */
export interface TimelineWindowStore {
  /** The window to open on. */
  get(): TimelineWindow
  /** Record a window the hook has just moved to. */
  set(next: TimelineWindow): void
}

/** The display's pixel ratio, defaulting to 1 wherever it is unreadable. */
function readDevicePixelRatio(): number {
  if (typeof window === 'undefined') {
    return 1
  }
  const ratio = window.devicePixelRatio
  return Number.isFinite(ratio) && ratio > 0 ? ratio : 1
}

/**
 * Viewport state for a timeline over `durationSeconds` of material.
 *
 * `windowStore`, when given, is where the `{ zoom, scrollSeconds }` pair is
 * seeded from and written back to, so the window survives this hook being
 * unmounted (feature 065).
 */
export function useTimelineGeometry(
  durationSeconds: number,
  windowStore?: TimelineWindowStore | null,
): TimelineGeometry {
  const [widthPx, setWidthPx] = useState(0)
  // Seeded once, from the store when there is one: a re-entered timeline opens
  // on the window it was left at rather than back at the whole file.
  const [timelineWindow, setTimelineWindow] = useState<TimelineWindow>(
    () => windowStore?.get() ?? WHOLE_FILE,
  )
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

  // One of exactly **two** places a window changes — `zoomToFit` below is the
  // other — which is what makes the write-through to `windowStore` complete.
  // Adding a third would have to write through as well, or a re-entered
  // timeline would open on a stale window.
  //
  // The write happens inside the updater because that is where the new window
  // exists. It is idempotent (the same `current` yields the same `next`), so
  // React re-running the updater — StrictMode, a discarded render — records
  // the same pair rather than a different one.
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
        const moved = { zoom: next.zoom, scrollSeconds: next.scrollSeconds }
        windowStore?.set(moved)
        return moved
      })
    },
    [durationSeconds, widthPx, windowStore],
  )

  const zoomBy = useCallback(
    (factor: number, anchorX?: number) => {
      applyToViewport((current) =>
        // No anchor offered — a toolbar button, a key — means the middle of
        // the window, which is the point a user reads a zoom as growing from.
        zoomedAt(current, factor, anchorX ?? current.widthPx / 2),
      )
    },
    [applyToViewport],
  )

  const zoomIn = useCallback(
    (anchorX?: number) => {
      zoomBy(ZOOM_STEP, anchorX)
    },
    [zoomBy],
  )

  const zoomOut = useCallback(
    (anchorX?: number) => {
      zoomBy(1 / ZOOM_STEP, anchorX)
    },
    [zoomBy],
  )

  const zoomToFit = useCallback(() => {
    setTimelineWindow((current) => {
      if (
        current.zoom === WHOLE_FILE.zoom &&
        current.scrollSeconds === WHOLE_FILE.scrollSeconds
      ) {
        // Same window: keep the identity so nothing downstream repaints, and
        // leave the store alone — it already holds this pair.
        return current
      }
      windowStore?.set(WHOLE_FILE)
      return WHOLE_FILE
    })
  }, [windowStore])

  const panBy = useCallback(
    (deltaSeconds: number) => {
      applyToViewport((current) => panned(current, deltaSeconds))
    },
    [applyToViewport],
  )

  const scrollTo = useCallback(
    (seconds: number) => {
      applyToViewport((current) =>
        clampViewport({ ...current, scrollSeconds: seconds }),
      )
    },
    [applyToViewport],
  )

  return {
    viewport,
    devicePixelRatio,
    trackRef,
    applyToViewport,
    zoomIn,
    zoomOut,
    zoomToFit,
    panBy,
    scrollTo,
  }
}
