/**
 * Peak (min/max envelope) computation for waveform rendering.
 *
 * A stem is millions of sample frames and a timeline is hundreds of pixel
 * columns, so nothing draws samples directly: the samples are reduced once to
 * a *bucket* per column — the minimum and maximum in that column's slice of
 * the range — and the view draws two-ended bars from those. This module is
 * that reduction and nothing else.
 *
 * **Pure by design.** No DOM, no React, no `AudioContext`, no engine import
 * beyond the buffer type: a peak computation is arithmetic over
 * `Float32Array`s, and keeping it that way is what lets it be tested exactly
 * (an impulse must land in one bucket, not "about one") and, later, moved
 * into a worker without rewriting it.
 *
 * **Three entry points, one kernel.** {@link computePeaks} is the synchronous
 * kernel over an arbitrary sample range. {@link computePeaksChunked} walks the
 * whole buffer in slices, yielding between them so a long file cannot freeze
 * the frame, and produces bit-for-bit the same buckets as the kernel would —
 * `min`/`max` fold associatively, so where the slice boundaries fall cannot
 * change the answer. {@link downsamplePeaks} aggregates an existing bucket set
 * instead of re-reading samples, which is how a zoom-out or a scroll repaints
 * without touching the audio again.
 *
 * **Bucket boundaries are floats.** Integer boundaries (`i * span / count`
 * rounded per bucket) leave the last bucket short whenever `count` does not
 * divide the range, which shows up as a waveform that dies away at its right
 * edge. Boundaries are computed in floating point and the last bucket is
 * closed on the end of the range, so the buckets tile the range exactly.
 */

/**
 * The min/max envelope of one sample range, one entry per bucket.
 *
 * Channels are collapsed: `mins[i]` is the minimum across *every* channel in
 * bucket `i` and `maxes[i]` the maximum, which is the envelope a single
 * mono-looking waveform lane draws.
 */
export interface PeakBuckets {
  /** Minimum sample value per bucket, in `-1..1`. */
  readonly mins: Float32Array
  /** Maximum sample value per bucket, in `-1..1`. */
  readonly maxes: Float32Array
  /** First sample frame these buckets cover (inclusive). */
  readonly startSample: number
  /** Last sample frame these buckets cover (exclusive). */
  readonly endSample: number
}

/** Options for {@link computePeaksChunked}. */
export interface ChunkedPeakOptions {
  /**
   * How many sample frames to read between yields. The default is about a
   * million, which is a few milliseconds of arithmetic — long enough that the
   * yields cost nothing, short enough that a frame is never missed by much.
   */
  readonly sliceSamples?: number
  /** Abandons the computation between slices. */
  readonly signal?: AbortSignal
  /**
   * How to hand control back between slices. Defaults to a macrotask
   * (`setTimeout(…, 0)`), which is what actually lets the browser paint; a
   * test injects a resolved promise instead so nothing waits on a real timer.
   */
  readonly yieldToEventLoop?: () => Promise<void>
}

/** Default sample frames per slice in {@link computePeaksChunked}. */
const DEFAULT_SLICE_SAMPLES = 1_048_576

/** Yield a macrotask, so the browser can paint between slices. */
function yieldMacrotask(): Promise<void> {
  return new Promise<void>((resolve) => {
    setTimeout(resolve, 0)
  })
}

/**
 * Whether the caller has given up. A function rather than an inline test:
 * the flag is read again after every `await`, and TypeScript would otherwise
 * narrow it to `false` for the rest of the loop from the first check.
 */
function isAborted(signal: AbortSignal | undefined): boolean {
  return signal?.aborted === true
}

/** The rejection an aborted computation produces. */
function abortError(): Error {
  if (typeof DOMException === 'function') {
    return new DOMException('Peak computation aborted', 'AbortError')
  }
  const error = new Error('Peak computation aborted')
  error.name = 'AbortError'
  return error
}

/** Zero-filled buckets, the answer for an empty range. */
function emptyBuckets(
  bucketCount: number,
  startSample: number,
  endSample: number,
): PeakBuckets {
  return {
    mins: new Float32Array(bucketCount),
    maxes: new Float32Array(bucketCount),
    startSample,
    endSample,
  }
}

/** At least one bucket, and a whole number of them. */
function normaliseBucketCount(bucketCount: number): number {
  if (!Number.isFinite(bucketCount)) {
    return 1
  }
  return Math.max(1, Math.floor(bucketCount))
}

/** The longest channel, which bounds the range any of them can cover. */
function channelLength(channels: readonly Float32Array[]): number {
  return channels.reduce(
    (longest, channel) => Math.max(longest, channel.length),
    0,
  )
}

/**
 * The half-open sample range of bucket `index`.
 *
 * Boundaries are floats and the last bucket closes on `end`, so the buckets
 * tile `[start, end)` exactly however badly `count` divides it. Zoomed in far
 * enough that a bucket would cover less than one sample, neighbouring buckets
 * repeat the same sample rather than coming back empty.
 */
function bucketRange(
  start: number,
  end: number,
  count: number,
  index: number,
): { from: number; to: number } {
  const span = end - start
  const from = Math.min(Math.floor(start + (span * index) / count), end - 1)
  const to =
    index === count - 1
      ? end
      : Math.min(
          end,
          Math.max(from + 1, Math.floor(start + (span * (index + 1)) / count)),
        )
  return { from, to }
}

/** Fold `[from, to)` of every channel into a running min/max pair. */
function scanRange(
  channels: readonly Float32Array[],
  from: number,
  to: number,
  running: { min: number; max: number },
): void {
  for (const channel of channels) {
    const stop = Math.min(to, channel.length)
    for (let index = from; index < stop; index += 1) {
      const sample = channel[index] ?? 0
      if (sample < running.min) {
        running.min = sample
      }
      if (sample > running.max) {
        running.max = sample
      }
    }
  }
}

/** Clamp a requested range to what the channels actually hold. */
function clampRange(
  channels: readonly Float32Array[],
  startSample: number,
  endSample: number,
): { start: number; end: number } {
  const length = channelLength(channels)
  const start = Math.min(Math.max(Math.floor(startSample), 0), length)
  const end = Math.min(Math.max(Math.floor(endSample), start), length)
  return { start, end }
}

/**
 * The min/max envelope of `[startSample, endSample)` in `bucketCount`
 * buckets — the synchronous kernel every other function here agrees with.
 *
 * The range is clamped to the channels' length, `bucketCount` to at least
 * one, and an empty range answers with zero-filled buckets rather than a
 * shorter array, so a caller never has to special-case a stem that has not
 * decoded yet.
 */
export function computePeaks(
  channels: readonly Float32Array[],
  startSample: number,
  endSample: number,
  bucketCount: number,
): PeakBuckets {
  const count = normaliseBucketCount(bucketCount)
  const { start, end } = clampRange(channels, startSample, endSample)
  if (end <= start || channels.length === 0) {
    return emptyBuckets(count, start, end)
  }

  const mins = new Float32Array(count)
  const maxes = new Float32Array(count)
  for (let index = 0; index < count; index += 1) {
    const { from, to } = bucketRange(start, end, count, index)
    const running = { min: Infinity, max: -Infinity }
    scanRange(channels, from, to, running)
    mins[index] = Number.isFinite(running.min) ? running.min : 0
    maxes[index] = Number.isFinite(running.max) ? running.max : 0
  }
  return { mins, maxes, startSample: start, endSample: end }
}

/**
 * {@link computePeaks} over the whole buffer, in slices, yielding between
 * them.
 *
 * The result is exactly what the synchronous kernel would return for the same
 * arguments: the same bucket boundaries are used, and each bucket's min/max
 * is folded across however many slices it happens to span. Aborting between
 * slices rejects promptly with an `AbortError`; a signal already aborted on
 * entry rejects without reading a sample.
 */
export async function computePeaksChunked(
  channels: readonly Float32Array[],
  bucketCount: number,
  options: ChunkedPeakOptions = {},
): Promise<PeakBuckets> {
  const count = normaliseBucketCount(bucketCount)
  const sliceSamples = Math.max(
    1,
    Math.floor(options.sliceSamples ?? DEFAULT_SLICE_SAMPLES),
  )
  const yieldToEventLoop = options.yieldToEventLoop ?? yieldMacrotask
  const signal = options.signal

  if (isAborted(signal)) {
    throw abortError()
  }
  const end = channelLength(channels)
  if (end === 0 || channels.length === 0) {
    return emptyBuckets(count, 0, end)
  }

  const mins = new Float32Array(count)
  const maxes = new Float32Array(count)
  let budget = sliceSamples
  for (let index = 0; index < count; index += 1) {
    const { from, to } = bucketRange(0, end, count, index)
    const running = { min: Infinity, max: -Infinity }
    // A single bucket can be the whole file (bucketCount 1), so the slice
    // budget is spent *inside* a bucket as well as between buckets.
    let cursor = from
    while (cursor < to) {
      const stop = Math.min(to, cursor + budget)
      scanRange(channels, cursor, stop, running)
      budget -= stop - cursor
      cursor = stop
      if (budget <= 0) {
        await yieldToEventLoop()
        if (isAborted(signal)) {
          throw abortError()
        }
        budget = sliceSamples
      }
    }
    mins[index] = Number.isFinite(running.min) ? running.min : 0
    maxes[index] = Number.isFinite(running.max) ? running.max : 0
  }
  return { mins, maxes, startSample: 0, endSample: end }
}

/**
 * Aggregate an existing bucket set down to `bucketCount` buckets over the
 * fractional window `[startFraction, endFraction)` of its range.
 *
 * Exact, not approximate: an output bucket is the min of the mins and the max
 * of the maxes of the base buckets it covers, so zooming out can never invent
 * a peak the samples did not have, nor lose one they did. Both fractions are
 * clamped to `0..1` and `endFraction` to at least `startFraction`.
 */
export function downsamplePeaks(
  base: PeakBuckets,
  startFraction: number,
  endFraction: number,
  bucketCount: number,
): PeakBuckets {
  const count = normaliseBucketCount(bucketCount)
  const baseCount = base.mins.length
  const low = Math.min(Math.max(startFraction, 0), 1)
  const high = Math.min(Math.max(endFraction, low), 1)
  const span = base.endSample - base.startSample
  const startSample = Math.round(base.startSample + span * low)
  const endSample = Math.round(base.startSample + span * high)
  if (baseCount === 0 || high <= low) {
    return emptyBuckets(count, startSample, endSample)
  }

  const fromBucket = low * baseCount
  const toBucket = high * baseCount
  const mins = new Float32Array(count)
  const maxes = new Float32Array(count)
  for (let index = 0; index < count; index += 1) {
    const windowSpan = toBucket - fromBucket
    const from = Math.min(
      Math.floor(fromBucket + (windowSpan * index) / count),
      baseCount - 1,
    )
    // The final output bucket closes on the end of the window, rounding *up*
    // so a partly covered base bucket at the edge still contributes.
    const to =
      index === count - 1
        ? Math.min(baseCount, Math.max(from + 1, Math.ceil(toBucket)))
        : Math.min(
            baseCount,
            Math.max(
              from + 1,
              Math.floor(fromBucket + (windowSpan * (index + 1)) / count),
            ),
          )
    let min = Infinity
    let max = -Infinity
    for (let source = from; source < to; source += 1) {
      const sourceMin = base.mins[source] ?? 0
      const sourceMax = base.maxes[source] ?? 0
      if (sourceMin < min) {
        min = sourceMin
      }
      if (sourceMax > max) {
        max = sourceMax
      }
    }
    mins[index] = Number.isFinite(min) ? min : 0
    maxes[index] = Number.isFinite(max) ? max : 0
  }
  return { mins, maxes, startSample, endSample }
}
