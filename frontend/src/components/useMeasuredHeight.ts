/**
 * The rendered height of one attached DOM element, in actual CSS pixels,
 * kept in sync with `ResizeObserver` — this codebase's own idiom for
 * watching layout, already used for width by `trackRef`/`widthPx` in
 * {@link useTimelineGeometry}, which this hook mirrors for height instead.
 *
 * **Replaces `useRootFontSize` (067's first attempt).** That hook multiplied
 * `TimelineLane.LANE_HEIGHT_REM` by the root font size, refreshed on window
 * `resize` — the only DOM event available, on the theory that a root-font
 * change reflows the page and a reflow of the page above the timeline
 * changes the window's content box. A controlled Chromium probe (part of a
 * cross-model review) found that theory false: changing the root font size
 * does **not** fire `resize` at all — `innerWidth`/`innerHeight` are
 * unaffected by a font-size-only reflow, and nothing else on the page here
 * happens to depend on viewport size in a way that would fire it either. The
 * consequence was silent and specific to the exact users this feature
 * serves: a mid-session font-size change grew the lane's `rem`-sized CSS box
 * (for free, since the browser recomputes `rem` layout on its own) while the
 * canvas backing store — still multiplying `LANE_HEIGHT_REM` by a root font
 * size that had gone stale — did not, stretching the waveform vertically by
 * roughly the same ratio as the font change (measured ~25% at 16→20px).
 *
 * This hook does not try to find a better proxy signal; it measures the box
 * itself instead of re-deriving its size from a signal that might not fire.
 * Whatever grows or shrinks the attached element's rendered height — a
 * `rem`-driven font-size change (probed: 76px → 95px across a change that
 * fired zero `resize` events), a window dragged between monitors at
 * different scale factors, anything else — reaches {@link MeasuredHeight.heightPx}
 * because `ResizeObserver` watches the element's own box, not a stand-in for
 * it.
 */
import { useCallback, useRef, useState } from 'react'

/** What {@link useMeasuredHeight} hands back. */
export interface MeasuredHeight {
  /**
   * Callback ref: attach to exactly one element to measure it. Attaching it
   * elsewhere moves the observer, exactly as {@link useTimelineGeometry}'s
   * `trackRef` documents for width.
   */
  readonly ref: (element: HTMLElement | null) => void
  /**
   * The attached element's current rendered height, in CSS pixels. `0`
   * before anything has been attached — there is no real box to report yet,
   * matching `widthPx`'s own starting state in {@link useTimelineGeometry}.
   */
  readonly heightPx: number
}

/**
 * Measure one element's rendered height and keep it current.
 *
 * Mirrors `trackRef` in `useTimelineGeometry.ts`: a `ResizeObserver.observe`
 * would still miss the very first paint (there is nothing to report a
 * resize *from* yet), so the callback ref also measures once on attach with
 * `getBoundingClientRect`, which additionally covers jsdom and any other
 * environment with no `ResizeObserver` at all — the first paint there still
 * gets a real (or stubbed) number instead of staying at `0` forever.
 */
export function useMeasuredHeight(): MeasuredHeight {
  const [heightPx, setHeightPx] = useState(0)
  const observerRef = useRef<ResizeObserver | null>(null)

  const ref = useCallback((element: HTMLElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (element === null) {
      return
    }
    setHeightPx(element.getBoundingClientRect().height)
    if (typeof ResizeObserver === 'undefined') {
      return
    }
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (entry !== undefined) {
        setHeightPx(entry.contentRect.height)
      }
    })
    observer.observe(element)
    observerRef.current = observer
  }, [])

  return { ref, heightPx }
}
