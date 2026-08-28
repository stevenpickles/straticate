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
 *
 * ## High-resolution tiles (feature 051)
 *
 * Zoomed in far enough, one pixel column covers fewer samples than one base
 * bucket does, and aggregating the base set would draw a blocky picture of an
 * envelope the samples know in more detail. Past that point — 049's
 * `needsHighResTile` — {@link useWaveformTiles} computes a *tile*: the visible
 * window of one stem, straight from its samples, one bucket per column. Three
 * rules keep that affordable:
 *
 * - **A tile is computed on the next animation frame, not per event.** A wheel
 *   storm is dozens of viewport changes a second; scheduling collapses them
 *   into one computation of the window that was actually landed on, and a
 *   superseded schedule is cancelled rather than published.
 * - **The last few tiles per stem are kept** ({@link MAX_TILES_PER_STEM}), so
 *   zooming out and back, or panning to and fro, redraws from memory. They are
 *   keyed on the range they cover, which is what makes reuse safe.
 * - **The base path is untouched below the threshold.** At fit zoom nothing
 *   here computes anything and the lanes aggregate the base buckets exactly as
 *   feature 050 left them.
 */

import { useEffect, useRef, useState } from 'react'
import type { AudioEngineBuffer, StemPlayerEngine } from '../audio/engine'
import {
  computePeaks,
  computePeaksChunked,
  type PeakBuckets,
} from '../audio/peaks'
import {
  needsHighResTile,
  sameTileRange,
  tileRangeFor,
  type TileRange,
  type TimelineViewport,
} from './timelineGeometry'

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

/**
 * How many computed tiles are kept per stem, most recently used first. Four
 * covers the movements a user makes in a second — a zoom in and back out, a
 * pan away and back — without holding more than a few hundred kilobytes.
 */
export const MAX_TILES_PER_STEM = 4

/** One stem's peaks over one window of it, with the range they cover. */
export interface WaveformTile {
  /** The slice of the stem these buckets describe. */
  readonly range: TileRange
  /** One bucket per pixel column of that slice. */
  readonly peaks: PeakBuckets
}

/** High-resolution tiles by stem name; a stem absent from it has none. */
export type StemTileMap = ReadonlyMap<string, WaveformTile>

/** Shared empty result, so "no tiles needed" is a stable identity. */
const NO_TILES: StemTileMap = new Map<string, WaveformTile>()

/** One stem, as the tile hook needs to see it. */
export interface TileStem {
  /** The stem's contract name. */
  readonly name: string
  /** Its own length in seconds, which may be shorter than the axis. */
  readonly durationSeconds: number
}

/** What has been computed for one engine. */
interface TileCache {
  engine: StemPlayerEngine | null
  /** Sample rate per stem: a decoded buffer's rate never changes. */
  readonly rates: Map<string, number>
  /** Up to {@link MAX_TILES_PER_STEM} tiles per stem, newest first. */
  readonly tiles: Map<string, WaveformTile[]>
}

/** An empty cache for `engine`. */
function emptyCache(engine: StemPlayerEngine | null): TileCache {
  return { engine, rates: new Map(), tiles: new Map() }
}

/**
 * The cached tile for `range`, or a freshly computed one, or `null` when the
 * stem's buffer is gone. A hit is moved to the front of the stem's list, which
 * is the whole of the eviction policy.
 */
function tileFor(
  engine: StemPlayerEngine,
  cache: TileCache,
  name: string,
  range: TileRange,
): WaveformTile | null {
  const held = cache.tiles.get(name) ?? []
  const hit = held.findIndex((tile) => sameTileRange(tile.range, range))
  if (hit >= 0) {
    const [tile] = held.splice(hit, 1)
    if (tile !== undefined) {
      held.unshift(tile)
      cache.tiles.set(name, held)
      return tile
    }
  }
  const buffer = engine.getStemBuffer(name)
  if (buffer === null) {
    return null
  }
  const rate = buffer.sampleRate
  const tile: WaveformTile = {
    range,
    peaks: computePeaks(
      channelsOf(buffer),
      Math.round(range.startSeconds * rate),
      Math.round(range.endSeconds * rate),
      range.buckets,
    ),
  }
  held.unshift(tile)
  held.length = Math.min(held.length, MAX_TILES_PER_STEM)
  cache.tiles.set(name, held)
  return tile
}

/**
 * The stems and lengths a tile run needs, encoded so the effect can depend on
 * a string. Lengths matter as well as names: a stem's decoded duration
 * replaces the contract's when it arrives, and that moves its window.
 */
function tileKey(stems: readonly TileStem[]): string {
  return stems
    .map(
      (stem) => `${stem.name}${KEY_SEPARATOR}${String(stem.durationSeconds)}`,
    )
    .join(KEY_SEPARATOR)
}

/** The inverse of {@link tileKey}. */
function parseTileKey(key: string): TileStem[] {
  const parts = key.split(KEY_SEPARATOR)
  const stems: TileStem[] = []
  for (let index = 0; index + 1 < parts.length; index += 2) {
    stems.push({
      name: parts[index] ?? '',
      durationSeconds: Number(parts[index + 1]),
    })
  }
  return stems
}

/**
 * High-resolution peak tiles for the stems whose lanes are currently zoomed in
 * past the base resolution.
 *
 * Returns an empty map — the same one, so identity is stable — whenever the
 * viewport is coarse enough that the base peaks are the better answer, which
 * is every viewport at fit zoom. The map is only ever *published* from an
 * animation frame, so a burst of wheel events costs one computation and not
 * one per event.
 */
export function useWaveformTiles(
  engine: StemPlayerEngine | null,
  stems: readonly TileStem[],
  viewport: TimelineViewport,
): StemTileMap {
  const [tiles, setTiles] = useState<StemTileMap>(NO_TILES)
  const cache = useRef<TileCache>(emptyCache(null))
  // What was last published, mirrored where the effect can read it without
  // depending on it — the same reason `useWaveformPeaks` holds its buckets in
  // a ref rather than reading its own state back.
  const published = useRef<StemTileMap>(NO_TILES)
  const key = tileKey(stems)

  useEffect(() => {
    if (cache.current.engine !== engine) {
      // Different audio: rates and tiles alike are about buffers that are gone.
      cache.current = emptyCache(engine)
    }
    const publish = (next: StemTileMap): void => {
      published.current = next
      setTiles(next)
    }
    const drop = (): void => {
      if (published.current.size > 0) {
        publish(NO_TILES)
      }
    }
    if (engine === null || key === '') {
      drop()
      return
    }

    const wanted = new Map<string, TileRange>()
    for (const stem of parseTileKey(key)) {
      let rate = cache.current.rates.get(stem.name)
      if (rate === undefined) {
        const buffer = engine.getStemBuffer(stem.name)
        if (buffer === null) {
          continue
        }
        rate = buffer.sampleRate
        cache.current.rates.set(stem.name, rate)
      }
      if (!needsHighResTile(viewport, rate, BASE_BUCKET_COUNT)) {
        continue
      }
      const range = tileRangeFor(viewport, stem.durationSeconds)
      if (range !== null) {
        wanted.set(stem.name, range)
      }
    }
    if (wanted.size === 0) {
      drop()
      return
    }
    const alreadyDrawn =
      published.current.size === wanted.size &&
      [...wanted].every(([name, range]) =>
        sameTileRange(published.current.get(name)?.range ?? null, range),
      )
    if (alreadyDrawn) {
      return
    }

    // One frame's grace: a wheel storm lands dozens of viewports here, and
    // only the last of them is worth reading samples for.
    let live = true
    const frame = requestAnimationFrame(() => {
      if (!live) {
        return
      }
      const next = new Map<string, WaveformTile>()
      for (const [name, range] of wanted) {
        const tile = tileFor(engine, cache.current, name, range)
        if (tile !== null) {
          next.set(name, tile)
        }
      }
      publish(next)
    })
    return () => {
      live = false
      cancelAnimationFrame(frame)
    }
  }, [engine, key, viewport])

  return tiles
}
