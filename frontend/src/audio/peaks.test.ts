import { describe, expect, it, vi } from 'vitest'
import {
  computePeaks,
  computePeaksChunked,
  downsamplePeaks,
  type PeakBuckets,
} from './peaks'

/** A channel of `length` samples produced by `sample(index)`. */
function channel(
  length: number,
  sample: (index: number) => number,
): Float32Array {
  const data = new Float32Array(length)
  for (let index = 0; index < length; index += 1) {
    data[index] = sample(index)
  }
  return data
}

/** Silence with a single non-zero sample at `at`. */
function impulse(length: number, at: number, amplitude = 1): Float32Array {
  return channel(length, (index) => (index === at ? amplitude : 0))
}

/** A yield that resolves immediately: nothing here waits on wall-clock time. */
function immediateYield(): Promise<void> {
  return Promise.resolve()
}

/** Plain arrays, so failures print numbers rather than typed-array dumps. */
function values(buckets: PeakBuckets): { mins: number[]; maxes: number[] } {
  return { mins: [...buckets.mins], maxes: [...buckets.maxes] }
}

describe('computePeaks', () => {
  it('lands an impulse in exactly one bucket', () => {
    const peaks = computePeaks([impulse(100, 42)], 0, 100, 10)

    expect(values(peaks).maxes).toEqual([0, 0, 0, 0, 1, 0, 0, 0, 0, 0])
    expect(values(peaks).mins).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
  })

  it('finds a negative impulse in the mins and nowhere else', () => {
    const peaks = computePeaks([impulse(100, 7, -1)], 0, 100, 10)

    expect(values(peaks).mins).toEqual([-1, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    expect(values(peaks).maxes).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
  })

  it('gives a linear ramp monotonically rising maxes', () => {
    const ramp = channel(1000, (index) => index / 1000)

    const peaks = computePeaks([ramp], 0, 1000, 16)

    const maxes = [...peaks.maxes]
    expect(maxes).toHaveLength(16)
    for (let index = 1; index < maxes.length; index += 1) {
      expect(maxes[index]).toBeGreaterThan(maxes[index - 1] ?? 0)
    }
    // The last bucket must reach the end of the range, not stop short of it.
    expect(maxes[15]).toBeCloseTo(0.999, 5)
  })

  it('covers the whole range when the bucket count does not divide it', () => {
    // 100 samples in 7 buckets: an impulse on any sample must be found, and
    // found once, or the buckets are not tiling the range.
    for (let at = 0; at < 100; at += 1) {
      const peaks = computePeaks([impulse(100, at)], 0, 100, 7)
      const hits = [...peaks.maxes].filter((value) => value === 1)
      expect(hits, `sample ${at}`).toHaveLength(1)
    }
  })

  it('honours a sub-range of the samples', () => {
    const peaks = computePeaks([impulse(100, 5)], 20, 60, 4)

    expect(peaks.startSample).toBe(20)
    expect(peaks.endSample).toBe(60)
    expect([...peaks.maxes]).toEqual([0, 0, 0, 0])
  })

  it('collapses every channel into one envelope', () => {
    const left = channel(40, (index) => (index < 20 ? 0.5 : 0))
    const right = channel(40, (index) => (index < 20 ? -0.75 : 0))

    const peaks = computePeaks([left, right], 0, 40, 2)

    expect(values(peaks)).toEqual({ mins: [-0.75, 0], maxes: [0.5, 0] })
  })

  it('repeats samples rather than starving buckets when zoomed past 1:1', () => {
    const peaks = computePeaks([channel(3, (index) => index / 4)], 0, 3, 6)

    expect(peaks.maxes).toHaveLength(6)
    expect([...peaks.maxes]).toEqual([0, 0, 0.25, 0.25, 0.5, 0.5])
  })

  it('answers an empty range with zero-filled buckets', () => {
    const peaks = computePeaks([channel(100, () => 1)], 50, 50, 5)

    expect(values(peaks)).toEqual({
      mins: [0, 0, 0, 0, 0],
      maxes: [0, 0, 0, 0, 0],
    })
    expect(peaks.startSample).toBe(50)
    expect(peaks.endSample).toBe(50)
  })

  it('clamps a range that runs past the samples', () => {
    const peaks = computePeaks([channel(10, () => 0.5)], -5, 500, 2)

    expect(peaks.startSample).toBe(0)
    expect(peaks.endSample).toBe(10)
    expect([...peaks.maxes]).toEqual([0.5, 0.5])
  })

  it('clamps the bucket count to at least one', () => {
    expect(computePeaks([channel(10, () => 1)], 0, 10, 0).maxes).toHaveLength(1)
    expect(computePeaks([channel(10, () => 1)], 0, 10, -3).maxes).toHaveLength(
      1,
    )
  })

  it('has no buckets to fill when there are no channels', () => {
    const peaks = computePeaks([], 0, 100, 3)

    expect(values(peaks)).toEqual({ mins: [0, 0, 0], maxes: [0, 0, 0] })
  })
})

describe('computePeaksChunked', () => {
  const noisy = channel(5000, (index) => Math.sin(index / 13) * 0.8)
  const second = channel(5000, (index) => Math.cos(index / 7) * 0.6)

  it('produces exactly what the synchronous kernel does', async () => {
    const chunked = await computePeaksChunked([noisy, second], 97, {
      sliceSamples: 64,
      yieldToEventLoop: immediateYield,
    })

    expect(chunked).toEqual(computePeaks([noisy, second], 0, 5000, 97))
  })

  it('is unaffected by where the slice boundaries fall', async () => {
    const fine = await computePeaksChunked([noisy], 40, {
      sliceSamples: 7,
      yieldToEventLoop: immediateYield,
    })
    const coarse = await computePeaksChunked([noisy], 40, {
      sliceSamples: 4096,
      yieldToEventLoop: immediateYield,
    })

    expect(fine).toEqual(coarse)
  })

  it('yields between slices', async () => {
    const yielded = vi.fn(immediateYield)

    await computePeaksChunked([noisy], 10, {
      sliceSamples: 1000,
      yieldToEventLoop: yielded,
    })

    // 5000 samples in 1000-sample slices: the fifth exhausts the buffer.
    expect(yielded).toHaveBeenCalledTimes(5)
  })

  it('splits a single bucket that is larger than a slice', async () => {
    const chunked = await computePeaksChunked([noisy], 1, {
      sliceSamples: 500,
      yieldToEventLoop: immediateYield,
    })

    expect(chunked).toEqual(computePeaks([noisy], 0, 5000, 1))
  })

  it('rejects with an AbortError when aborted mid-flight', async () => {
    const controller = new AbortController()
    let slices = 0
    const pending = computePeaksChunked([noisy], 64, {
      sliceSamples: 100,
      signal: controller.signal,
      yieldToEventLoop: () => {
        slices += 1
        if (slices === 2) {
          controller.abort()
        }
        return Promise.resolve()
      },
    })

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    // It stopped where it was told to, rather than finishing all 50 slices.
    expect(slices).toBe(2)
  })

  it('rejects without reading a sample when the signal is already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    const yielded = vi.fn(immediateYield)

    await expect(
      computePeaksChunked([noisy], 8, {
        signal: controller.signal,
        sliceSamples: 10,
        yieldToEventLoop: yielded,
      }),
    ).rejects.toMatchObject({ name: 'AbortError' })
    expect(yielded).not.toHaveBeenCalled()
  })

  it('answers an empty buffer with zero-filled buckets', async () => {
    const peaks = await computePeaksChunked([new Float32Array(0)], 4, {
      yieldToEventLoop: immediateYield,
    })

    expect(values(peaks)).toEqual({ mins: [0, 0, 0, 0], maxes: [0, 0, 0, 0] })
    expect(peaks.endSample).toBe(0)
  })
})

describe('downsamplePeaks', () => {
  /** Eight base buckets over 800 samples, each with a distinct envelope. */
  const base: PeakBuckets = {
    mins: Float32Array.from([-0.1, -0.2, -0.3, -0.4, -0.5, -0.6, -0.7, -0.8]),
    maxes: Float32Array.from([0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]),
    startSample: 0,
    endSample: 800,
  }

  it('aggregates pairs of base buckets exactly', () => {
    const peaks = downsamplePeaks(base, 0, 1, 4)

    expect(values(peaks).mins.map((value) => Number(value.toFixed(6)))).toEqual(
      [-0.2, -0.4, -0.6, -0.8],
    )
    expect(
      values(peaks).maxes.map((value) => Number(value.toFixed(6))),
    ).toEqual([0.8, 0.6, 0.4, 0.2])
    expect(peaks.startSample).toBe(0)
    expect(peaks.endSample).toBe(800)
  })

  it('reports the sample range of the fractional window', () => {
    const peaks = downsamplePeaks(base, 0.25, 0.75, 2)

    expect(peaks.startSample).toBe(200)
    expect(peaks.endSample).toBe(600)
    expect(
      values(peaks).maxes.map((value) => Number(value.toFixed(6))),
    ).toEqual([0.6, 0.4])
  })

  it('returns the base envelope unchanged at the same bucket count', () => {
    const peaks = downsamplePeaks(base, 0, 1, 8)

    expect(peaks.mins).toEqual(base.mins)
    expect(peaks.maxes).toEqual(base.maxes)
  })

  it('agrees with recomputing from samples when buckets divide evenly', () => {
    const samples = channel(960, (index) => Math.sin(index / 11))
    const fine = computePeaks([samples], 0, 960, 60)

    const coarse = downsamplePeaks(fine, 0, 1, 12)

    expect(coarse.maxes).toEqual(computePeaks([samples], 0, 960, 12).maxes)
    expect(coarse.mins).toEqual(computePeaks([samples], 0, 960, 12).mins)
  })

  it('keeps a partly covered edge bucket rather than dropping it', () => {
    // 8 base buckets into 3: the last output bucket must still reach bucket 7.
    const peaks = downsamplePeaks(base, 0, 1, 3)

    expect(Number(peaks.mins[2]?.toFixed(6))).toBe(-0.8)
    expect(Number(peaks.maxes[2]?.toFixed(6))).toBe(0.3)
  })

  it('clamps fractions outside 0..1', () => {
    const peaks = downsamplePeaks(base, -1, 5, 2)

    expect(peaks.startSample).toBe(0)
    expect(peaks.endSample).toBe(800)
    expect(Number(peaks.mins[1]?.toFixed(6))).toBe(-0.8)
  })

  it('answers an empty window with zero-filled buckets', () => {
    const peaks = downsamplePeaks(base, 0.5, 0.5, 3)

    expect(values(peaks)).toEqual({ mins: [0, 0, 0], maxes: [0, 0, 0] })
    expect(peaks.startSample).toBe(400)
    expect(peaks.endSample).toBe(400)
  })

  it('answers an empty base with zero-filled buckets', () => {
    const empty: PeakBuckets = {
      mins: new Float32Array(0),
      maxes: new Float32Array(0),
      startSample: 0,
      endSample: 0,
    }

    expect(values(downsamplePeaks(empty, 0, 1, 2))).toEqual({
      mins: [0, 0],
      maxes: [0, 0],
    })
  })
})
