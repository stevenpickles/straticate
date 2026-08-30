/**
 * The browser's root font size, in CSS pixels — the value every `rem` unit
 * on the page resolves against.
 *
 * Feature 067 made the timeline's lane height rem-relative
 * ({@link TimelineLane.LANE_HEIGHT_REM}) so a user's browser-level font-size
 * setting scales the header box along with the rows inside it, instead of
 * clipping them at a fixed pixel height. The header and lane *boxes* scale
 * for free — that is what `rem` is for, and it costs nothing here. A
 * `<canvas>`'s backing store is the one part of the timeline that does not:
 * `canvas.width`/`canvas.height` are plain integers, not CSS lengths, so
 * whatever sizes the canvas has to know the lane's height in actual pixels,
 * and has to find out again when the root font size changes.
 *
 * **`resize` is the practical signal, not a precise one.** There is no DOM
 * event for "the root font size changed" — the standard font-size setting
 * lives in browser chrome the page cannot observe directly. A root-font
 * change reflows the whole document, though, and a reflow of the page above
 * the timeline changes the window's content box, which fires `resize`. An
 * ordinary window resize fires the same event for an unrelated reason; this
 * hook does not try to tell the two apart, because it does not need to —
 * re-reading `getComputedStyle` on a resize that changed nothing just
 * produces the same number again.
 */
import { useEffect, useState } from 'react'

/**
 * jsdom implements no rendering engine, so `getComputedStyle` there resolves
 * no font size at all; this is the value every test — and every environment
 * with no layout engine — falls back to, matching the browser default.
 */
const FALLBACK_ROOT_FONT_PX = 16

/** Read the root element's current computed font size, in CSS pixels. */
function readRootFontPx(): number {
  if (
    typeof document === 'undefined' ||
    typeof getComputedStyle !== 'function'
  ) {
    return FALLBACK_ROOT_FONT_PX
  }
  const parsed = parseFloat(getComputedStyle(document.documentElement).fontSize)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : FALLBACK_ROOT_FONT_PX
}

/**
 * The current root font size in CSS pixels, refreshed on every window
 * `resize`. Starts from the real value (or {@link FALLBACK_ROOT_FONT_PX} in
 * an environment with no layout engine) so the first paint already uses it —
 * there is no separate "unmeasured" state to render around.
 */
export function useRootFontSize(): number {
  const [rootFontPx, setRootFontPx] = useState(readRootFontPx)

  useEffect(() => {
    if (typeof window === 'undefined') {
      return
    }
    const onResize = (): void => {
      setRootFontPx(readRootFontPx())
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
    }
  }, [])

  return rootFontPx
}
