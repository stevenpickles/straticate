import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useMeasuredHeight } from './useMeasuredHeight'

/**
 * A resize observer whose callbacks a test delivers by hand — the same
 * pattern `StemPlayer.test.tsx` uses for the width observer this hook
 * mirrors.
 */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  readonly targets: Element[] = []
  readonly callback: ResizeObserverCallback
  disconnected = false

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeResizeObserver.instances.push(this)
  }

  observe(target: Element): void {
    this.targets.push(target)
  }

  unobserve(): void {
    // Nothing here observes twice.
  }

  disconnect(): void {
    this.disconnected = true
    this.targets.length = 0
  }
}

function stubResizeObserver(): void {
  FakeResizeObserver.instances = []
  vi.stubGlobal('ResizeObserver', FakeResizeObserver)
}

/** Stub one element's `getBoundingClientRect().height`. */
function stubHeight(element: Element, height: number): void {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({
    height,
  } as DOMRect)
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('useMeasuredHeight', () => {
  it('reports 0 with nothing attached yet', () => {
    const { result } = renderHook(() => useMeasuredHeight())
    expect(result.current.heightPx).toBe(0)
  })

  it('measures the attached element via getBoundingClientRect, even with no ResizeObserver', () => {
    // No stub here — this is the real jsdom default every other test in
    // this suite runs under (no ResizeObserver at all), the same
    // environment `useTimelineGeometry`'s `trackRef` falls back for.
    const { result } = renderHook(() => useMeasuredHeight())
    const element = document.createElement('div')
    stubHeight(element, 76)

    act(() => {
      result.current.ref(element)
    })

    expect(result.current.heightPx).toBe(76)
  })

  it('follows a later resize reported by the observer', () => {
    stubResizeObserver()
    const { result } = renderHook(() => useMeasuredHeight())
    const element = document.createElement('div')
    stubHeight(element, 76)

    act(() => {
      result.current.ref(element)
    })
    expect(result.current.heightPx).toBe(76)

    // A root-font change grows the box without touching the window at all —
    // the exact case `resize` missed. The fake delivers it the way a real
    // `ResizeObserver` would: a `contentRect` for the element it is
    // watching, with no `resize` event anywhere in the loop.
    const observer = FakeResizeObserver.instances.at(-1)
    act(() => {
      observer?.callback(
        [
          {
            target: element,
            contentRect: { height: 95 },
          } as unknown as ResizeObserverEntry,
        ],
        observer as unknown as ResizeObserver,
      )
    })

    expect(result.current.heightPx).toBe(95)
  })

  it('disconnects the same observer instance it created, when the element detaches', () => {
    stubResizeObserver()
    const { result } = renderHook(() => useMeasuredHeight())
    const element = document.createElement('div')
    stubHeight(element, 76)

    act(() => {
      result.current.ref(element)
    })
    const observer = FakeResizeObserver.instances.at(-1)
    expect(observer).toBeDefined()
    expect(observer?.disconnected).toBe(false)

    act(() => {
      result.current.ref(null)
    })

    // Not just "some observer was disconnected" — the one instance this
    // attach created, pinned by reference rather than by a generic spy on
    // the constructor.
    expect(observer?.disconnected).toBe(true)
    expect(FakeResizeObserver.instances).toHaveLength(1)
  })

  it('disconnects the previous observer before creating a new one on reattachment', () => {
    stubResizeObserver()
    const { result } = renderHook(() => useMeasuredHeight())
    const first = document.createElement('div')
    stubHeight(first, 76)
    act(() => {
      result.current.ref(first)
    })
    const firstObserver = FakeResizeObserver.instances.at(-1)

    const second = document.createElement('div')
    stubHeight(second, 84)
    act(() => {
      result.current.ref(second)
    })
    const secondObserver = FakeResizeObserver.instances.at(-1)

    expect(firstObserver).not.toBe(secondObserver)
    expect(firstObserver?.disconnected).toBe(true)
    expect(result.current.heightPx).toBe(84)
  })
})
