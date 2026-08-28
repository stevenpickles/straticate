/**
 * A recording fake of the slice of the 2D canvas API the waveform drawing
 * code uses.
 *
 * jsdom has no rendering backend: `canvas.getContext('2d')` returns `null`
 * unless the native `canvas` package is installed, and even then nothing can
 * be asserted about pixels. So waveform drawing writes through the
 * `WaveformDrawContext` structural interface and tests substitute this,
 * exactly as `fakeAudioContext.ts` substitutes for the Web Audio API:
 * every call is recorded, and a test asserts on **what would have been
 * painted** — the rectangles, their order, and the fill style in force when
 * each was issued.
 *
 * Extend this file rather than forking it; a second canvas double would be a
 * second set of conventions to keep in step.
 */

import { vi } from 'vitest'
import type { WaveformDrawContext } from '../components/timelineGeometry'

/** One rectangle, as the caller gave it. */
export interface RecordedRect {
  readonly x: number
  readonly y: number
  readonly width: number
  readonly height: number
}

/** One `fillRect`, with the fill style that was in force at the time. */
export interface RecordedFillRect extends RecordedRect {
  readonly fillStyle: string | CanvasGradient | CanvasPattern
}

/** One `setTransform(a, b, c, d, e, f)` call. */
export interface RecordedTransform {
  readonly a: number
  readonly b: number
  readonly c: number
  readonly d: number
  readonly e: number
  readonly f: number
}

/** A recording `CanvasRenderingContext2D`. */
export class FakeCanvasContext2D implements WaveformDrawContext {
  /** The current fill style, as the last assignment left it. */
  fillStyle: string | CanvasGradient | CanvasPattern = '#000000'
  /** Every `fillRect` call, in order. */
  readonly fillRects: RecordedFillRect[] = []
  /** Every `clearRect` call, in order. */
  readonly clearRects: RecordedRect[] = []
  /** Every `setTransform` call, in order. */
  readonly transforms: RecordedTransform[] = []

  clearRect(x: number, y: number, width: number, height: number): void {
    this.clearRects.push({ x, y, width, height })
  }

  fillRect(x: number, y: number, width: number, height: number): void {
    this.fillRects.push({ x, y, width, height, fillStyle: this.fillStyle })
  }

  setTransform(
    a: number,
    b: number,
    c: number,
    d: number,
    e: number,
    f: number,
  ): void {
    this.transforms.push({ a, b, c, d, e, f })
  }

  /** Forget everything recorded so far, keeping the current fill style. */
  reset(): void {
    this.fillRects.length = 0
    this.clearRects.length = 0
    this.transforms.length = 0
  }
}

/**
 * Make every `canvas.getContext(…)` in the test return one
 * {@link FakeCanvasContext2D}, and return it so the test can read what was
 * drawn. Pair with `vi.restoreAllMocks()` in `afterEach`, the way the other
 * `vi.spyOn` suites do.
 */
export function installFakeCanvas(): FakeCanvasContext2D {
  const context = new FakeCanvasContext2D()
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as unknown as CanvasRenderingContext2D,
  )
  return context
}
