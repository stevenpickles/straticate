import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useRootFontSize } from './useRootFontSize'

/** Stub `getComputedStyle(document.documentElement).fontSize` at `px`. */
function stubRootFontPx(px: number): void {
  vi.spyOn(window, 'getComputedStyle').mockImplementation(
    (element: Element) =>
      ({
        fontSize: element === document.documentElement ? `${String(px)}px` : '',
        getPropertyValue: () => '',
      }) as unknown as CSSStyleDeclaration,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useRootFontSize', () => {
  it('falls back to 16 in an environment with no measurable root font', () => {
    // jsdom resolves no computed `fontSize` at all — the fallback every unit
    // test in this repository (and every environment with no layout engine)
    // sees, matching the browser default.
    const { result } = renderHook(() => useRootFontSize())
    expect(result.current).toBe(16)
  })

  it('reads the real root font size when one is measurable', () => {
    stubRootFontPx(20)
    const { result } = renderHook(() => useRootFontSize())
    expect(result.current).toBe(20)
  })

  it('refreshes on a window resize, and only on a resize', () => {
    stubRootFontPx(16)
    const { result } = renderHook(() => useRootFontSize())
    expect(result.current).toBe(16)

    // A root-font change reflows the page and fires `resize` — the
    // practical signal the hook documents using, since there is no DOM
    // event for the root font size itself.
    stubRootFontPx(18)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(result.current).toBe(18)
  })

  it('removes its resize listener on unmount, not leaving one behind', () => {
    stubRootFontPx(16)
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const { unmount } = renderHook(() => useRootFontSize())
    unmount()

    expect(removeSpy).toHaveBeenCalledWith('resize', expect.any(Function))
  })
})
