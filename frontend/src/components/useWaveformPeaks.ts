/**
 * Per-stem peak envelopes for the timeline's waveform lanes.
 *
 * The engine owns decoded audio and hands it out through `getStemBuffer`
 * (feature 049); this hook is the one place that reads it. When a stem's load
 * status flips to `loaded` its samples are reduced **once** to a base bucket
 * set — {@link BASE_BUCKET_COUNT} buckets over the whole stem — and every
 * repaint after that aggregates those buckets down to the pixel width with
 * `downsamplePeaks`, never touching the samples again.
 *
 * Three rules this module exists to keep:
 *
 * 1. **Raw channel data never enters React state.** A four-stem job is tens of
 *    millions of floats; what is stored is two `Float32Array`s of
 *    {@link BASE_BUCKET_COUNT} entries per stem, which is a few tens of
 *    kilobytes for the whole mix.
 * 2. **The buffer is borrowed, not retained.** 049 documents that a stem's
 *    buffer stops being valid at the next `load()` or `dispose()`. The samples
 *    are read inside the effect and only the derived buckets outlive it.
 * 3. **The computation is abortable and aborted.** `computePeaksChunked`
 *    yields to the event loop between slices, so a long file cannot freeze the
 *    frame; unmounting, or swapping the engine, aborts whatever is still
 *    running rather than letting it publish into a torn-down tree.
 */

import { useEffect, useRef, useState } from 'react'
import type { AudioEngineBuffer, StemPlayerEngine } from '../audio/engine'
import { computePeaksChunked, type PeakBuckets } from '../audio/peaks'

/**
 * Buckets computed per stem over the whole file. Comfortably more than any
 * reasonable strip is wide, so a lane at fit zoom is always an aggregation of
 * this set rather than a stretch of it; feature 051 recomputes high-resolution
 * tiles from samples when a zoom goes past it (`needsHighResTile`).
 */
export const BASE_BUCKET_COUNT = 8192

/** Base peak sets by stem name; a stem absent from it has none yet. */
export type StemPeakMap = ReadonlyMap<string, PeakBuckets>

/** Shared empty result, so "nothing computed" is a stable identity. */
const NO_PEAKS: StemPeakMap = new Map<string, PeakBuckets>()

/**
 * Separator for the effect's dependency key. A stem name is a filename
 * component, so it can hold most things a path can — but never a NUL.
 */
const KEY_SEPARATOR = String.fromCodePoint(0)

/** Every channel of a decoded buffer, as the peak kernel wants them. */
function channelsOf(buffer: AudioEngineBuffer): Float32Array[] {
  const channels: Float32Array[] = []
  for (let index = 0; index < buffer.numberOfChannels; index += 1) {
    channels.push(buffer.getChannelData(index))
  }
  return channels
}

/**
 * Base peak sets for every stem that has decoded.
 *
 * `loadedNames` should hold exactly the stems whose engine status is
 * `loaded`, in any order. It is joined into a string for the effect's
 * dependency so a new array with the same names does not restart the work —
 * the engine snapshot is rebuilt on every mute toggle, and recomputing peaks
 * then would be the most expensive no-op in the app.
 */
export function useWaveformPeaks(
  engine: StemPlayerEngine | null,
  loadedNames: readonly string[],
): StemPeakMap {
  const [peaks, setPeaks] = useState<StemPeakMap>(NO_PEAKS)
  // What has been computed for *this* engine. Held in a ref rather than read
  // from state so the effect does not depend on its own output.
  const computed = useRef<{
    engine: StemPlayerEngine | null
    buckets: Map<string, PeakBuckets>
  }>({ engine: null, buckets: new Map() })
  const key = loadedNames.join(KEY_SEPARATOR)

  useEffect(() => {
    if (computed.current.engine !== engine) {
      // A different engine means different audio: nothing computed carries
      // over, and the old buffers are gone in any case.
      computed.current = { engine, buckets: new Map() }
      setPeaks(NO_PEAKS)
    }
    if (engine === null || key === '') {
      return
    }

    const controller = new AbortController()
    let live = true
    const names = key.split(KEY_SEPARATOR)

    const run = async (): Promise<void> => {
      for (const name of names) {
        if (!live || computed.current.buckets.has(name)) {
          continue
        }
        const buffer = engine.getStemBuffer(name)
        if (buffer === null) {
          continue
        }
        try {
          const buckets = await computePeaksChunked(
            channelsOf(buffer),
            BASE_BUCKET_COUNT,
            { signal: controller.signal },
          )
          if (!live) {
            return
          }
          computed.current.buckets.set(name, buckets)
          // A fresh Map each time: identity is what tells the memoised lanes
          // that something they draw has changed.
          setPeaks(new Map(computed.current.buckets))
        } catch {
          // The only rejection is the abort above, and an aborted run has
          // nothing to publish.
          return
        }
      }
    }
    void run()

    return () => {
      live = false
      controller.abort()
    }
  }, [engine, key])

  return peaks
}
