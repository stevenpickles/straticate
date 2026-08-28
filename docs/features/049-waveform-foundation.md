# [049] Waveform foundation (peaks, geometry, engine seam)

Branch: `049-waveform-foundation`
Status: PR OPEN
Dependencies: 023
PR: #71

## Objective

Everything a waveform timeline needs *below* the component layer, with zero
visible change: the audio engine hands out decoded stem buffers, a pure module
reduces samples to a min/max envelope, a second pure module answers every
"where does this time sit" question and paints the envelope, and both test
doubles — audio and canvas — can drive all of it in jsdom. Feature 050 builds
the visible timeline on top and should need no new plumbing to do it.

## Scope

- **Engine seam.** `AudioEngineBuffer` widened from `{ duration }` to the
  members a waveform actually reads (`length`, `numberOfChannels`,
  `sampleRate`, `getChannelData`), and `getStemBuffer(name)` added to
  `StemPlayerEngine` / `StemAudioEngine`.
- **`frontend/src/audio/peaks.ts`** — `computePeaks`, `computePeaksChunked`,
  `downsamplePeaks`, `PeakBuckets`.
- **`frontend/src/components/timelineGeometry.ts`** — `TimelineViewport` and
  the pure functions over it, plus `WaveformDrawContext` and `drawWaveform`.
- **Test doubles** — `fakeAudioContext.ts` extended with `FakeAudioBuffer` and
  `stemBytesWithSamples`; new `fakeCanvasContext.ts`.

## Out of scope

- Any UI change. `StemPlayer.tsx`, `StemPlayer.css` and `Workspace.tsx` are
  untouched; nothing in this feature is reachable from the running app yet.
- Scrub preview, loop regions, zoom UI (052 / 053 / 051).
- Peaks in the engine or its snapshot — see the decisions below.
- Any backend or E2E change.

## Expected modules/files

- `frontend/src/audio/engine.ts` — widened `AudioEngineBuffer`,
  `getStemBuffer` on the interface and the class
- `frontend/src/audio/peaks.ts`, `frontend/src/audio/peaks.test.ts` — new
- `frontend/src/components/timelineGeometry.ts`,
  `frontend/src/components/timelineGeometry.test.ts` — new
- `frontend/src/test/fakeCanvasContext.ts` — new
- `frontend/src/test/fakeAudioContext.ts` — `FakeAudioBuffer`,
  `stemBytesWithSamples`, `decodeAudioData`
- `frontend/src/audio/engine.test.ts` — a `StemAudioEngine stem buffers`
  section
- `frontend/src/components/StemPlayer.test.tsx` — one line: its `FakeEngine`
  implements the widened `StemPlayerEngine`
- `docs/features/049-waveform-foundation.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **`getStemBuffer` exposes decoded buffers, and a real `AudioBuffer` still
      satisfies the widened interface.** The type check is the proof: the
      engine's default context factory is `() => new AudioContext()`, so a real
      `AudioContext` must be assignable to `AudioEngineContext`, whose
      `decodeAudioData` returns `AudioEngineBuffer`. `npm run typecheck` passes
      with no cast anywhere on that path.
- [x] **`peaks.ts` and `timelineGeometry.ts` are pure and fully unit-tested.**
      No DOM, no React, no engine import except `PeakBuckets` as a type; 67
      tests across the two modules.
- [x] **`fakeAudioContext.ts` extended, not forked; every pre-existing test
      passes unchanged.** `stemBytes(d)` still decodes to duration `d`, because
      `FakeAudioBuffer` keeps `duration = byteLength / 100`. Full suite: 874
      tests, 40 files, green.
- [x] **`fakeCanvasContext.ts` provides a recording double usable by 050.**
      `FakeCanvasContext2D` records `fillRects` / `clearRects` / `transforms`,
      and `installFakeCanvas()` puts one behind `getContext` for a component
      test that never touches the drawing code directly.
- [x] **Feature doc, own ROADMAP row, five frontend gates green** —
      `format:check`, `lint`, `typecheck`, `test`, `build`.

## Required tests

- **Peaks** (`peaks.test.ts`, 26): an impulse lands in exactly one bucket, and
  in the mins when it is negative; a linear ramp gives strictly rising maxes
  whose last bucket reaches the end of the range; every one of 100 sample
  positions is found exactly once with 7 buckets over 100 samples;
  multi-channel collapse; sub-ranges, clamped ranges, empty ranges, zero and
  negative bucket counts; chunked equals the sync kernel exactly, at two slice
  sizes and with a single bucket larger than a slice; the yield is called once
  per slice; abort mid-flight and abort-before-start both reject with an
  `AbortError`; downsampling is exact against a known base, agrees with
  recomputing from samples when the counts divide, keeps a partly covered edge
  bucket, and clamps out-of-range fractions.
- **Geometry** (`timelineGeometry.test.ts`, 41): `xToTime(timeToX(t)) ≈ t` at
  several times; the `zoomedAt` anchor invariant in both directions; clamping
  at both ends, at both zoom limits, and for a file shorter than the minimum
  window; the tick ladder at fit, at three zoom levels and on an hour-long
  file; `needsHighResTile` either side of its threshold; `drawWaveform`
  against the recording double — transform, clear, one `fillRect` per column
  in the passed colour, the 0.92-headroom geometry of a full-scale bucket, the
  hairline for silence, an asymmetric bucket, and nothing painted when there is
  nothing to paint.
- **Engine** (`engine.test.ts`, +5): `getStemBuffer` round-trips authored
  samples through the fake loader and `decodeAudioData`; `null` for an unknown
  name, before load, for a stem whose fetch failed, and after `dispose()`.

### Proved to fail first

Both regressions were run against a deliberately broken implementation before
the real one, and the stubs were then reverted (`git diff` carries no trace).

Stub 1 — `getStemBuffer` returns `null` unconditionally:

```text
× hands back the decoded samples of a loaded stem
    AssertionError: expected undefined to be 1 // Object.is equality
× is null for a stem whose audio failed to load
    AssertionError: expected null not to be null
× is null after disposal
    AssertionError: expected null not to be null
```

Stub 2 — integer bucket width (`Math.floor(span / count)` per bucket), the
naive kernel this module exists to replace:

```text
× gives a linear ramp monotonically rising maxes
    AssertionError: expected 0.9909999966621399 to be close to 0.999,
    received difference is 0.008000003337860107, but expected 0.000005
× covers the whole range when the bucket count does not divide it
    AssertionError: sample 98: expected [] to have a length of 1 but got +0
× repeats samples rather than starving buckets when zoomed past 1:1
    AssertionError: expected [ +0, +0, +0, +0, +0, +0 ] to deeply equal
    [ +0, +0, 0.25, 0.25, 0.5, 0.5 ]
```

`sample 98` is the whole finding: with 7 integer-width buckets over 100
samples the last 2 samples of the file are in no bucket at all, which on
screen is a waveform whose right edge fades to silence that is not there.

## Notes / decisions

1. **Peaks are not in the engine, and not in the snapshot.** The engine's
   snapshot is read by `useSyncExternalStore` on every notify, and a snapshot
   is compared by identity — putting a peak set in it would mean either
   rebuilding typed arrays on every mute toggle or hand-managing their identity
   forever, for data that changes only when a stem loads. It would also give
   the engine a second job (analysis) alongside the one it documents
   (synchronised playback). Instead the engine exposes the *buffer* and the
   view pulls samples when it wants them: one `getStemBuffer` call per stem per
   load, and the peak computation lives in a pure module that a worker could
   run later without touching the engine at all.

   The consequence 050 must respect: the buffer belongs to the engine and is
   dropped by the next `load()` or by `dispose()` (`teardownGraph` nulls it).
   Read it, derive from it, do not retain it across a load.

2. **The fake decodes samples from bytes.** `stemBytes(d)` already encoded a
   duration as `d * 100` bytes, and every existing engine test depends on
   `duration === byteLength / 100`. `FakeAudioBuffer` therefore reads **one
   mono sample per byte at 100 Hz**, which leaves that invariant exactly where
   it was while making the bytes carry real sample data —
   `stemBytesWithSamples([...])` is its inverse. No test needed changing, and
   waveform tests can author a stem sample by sample instead of consulting a
   lookup table the fake would have had to grow.

   Two edges of the encoding, both deliberate: it is one-sided (byte 128 is
   zero, so `-1` round-trips exactly and `+1` comes back as `127/128`), and a
   buffer from `stemBytes` — all zero bytes — decodes to a **constant `-1`**,
   not silence. Nothing existing reads those samples, and a test that wants
   silence should author it with `stemBytesWithSamples`.

3. **Float bucket boundaries, and why the last bucket is special.** Boundaries
   are computed in floating point and the final bucket closes on the end of the
   range, so buckets tile `[start, end)` exactly for any bucket count. Zoomed
   past one sample per bucket, neighbours repeat a sample rather than coming
   back empty. The mutation output above is what integer widths look like.

4. **Chunked equals sync exactly, not approximately.** `computePeaksChunked`
   uses the same `bucketRange` and folds each bucket's min/max across however
   many slices it spans; `min`/`max` are associative, so slice boundaries
   cannot move the answer. That is asserted with `toEqual` against the kernel,
   not with a tolerance. The slice budget is spent *inside* a bucket as well as
   between buckets, because `bucketCount: 1` over a long file is one bucket and
   would otherwise block for the whole file.

5. **The yield is injectable.** Default is `setTimeout(…, 0)` — a microtask
   yield never lets the browser paint, which is the entire point — but tests
   pass `() => Promise.resolve()`, so nothing in the suite waits on wall-clock
   time (the convention `engine.test.ts` established).

6. **0.92 headroom and the hairline.** A column reaches 92% of the half-height,
   so a full-scale sample has somewhere to be instead of merging into the
   lane's edges and looking clipped. Every column is drawn at least 1 px tall
   (`Math.max(1, …)`), so silence is a hairline through the middle: a gap in a
   waveform should mean "no stem here", never "this part is quiet". Both
   numbers are pinned by tests, so a later change to either is a deliberate one.

7. **`WaveformDrawContext.fillStyle` is the canvas union, not `string`.**
   *Deliberate deviation from the assignment*, which specified
   `fillStyle: string`. A real `CanvasRenderingContext2D` declares
   `fillStyle: string | CanvasGradient | CanvasPattern`, and a mutable property
   is invariant, so the narrower type would have made the interface one a real
   context could **not** satisfy — 050 would have had to cast at the seam,
   which is exactly what these structural interfaces exist to avoid.
   `drawWaveform` still only ever assigns a string.
   `timelineGeometry.test.ts` pins the compatibility as a compile-time
   assertion (`CanvasRenderingContext2D extends WaveformDrawContext`).

8. **`zoom` is a ratio, not pixels per second.** `zoom === 1` fits the whole
   file whatever the strip's width, so a resize does not change what is shown.
   The zoom ceiling is "the visible window never shrinks below 1 s", which for
   a file under a second means no zoom at all.

9. **`installFakeCanvas` uses `vi.spyOn`, restored by `vi.restoreAllMocks()`.**
   `getContext` is a prototype method rather than a global, so it takes the
   `spyOn` half of the suite's conventions (`AudioSummary.test.tsx`,
   `ExportPanel.test.tsx`) rather than the `stubGlobal` /
   `unstubAllGlobals` half that `StemPlayer.test.tsx` uses for
   `requestAnimationFrame`. A suite that installs both needs both restores.

10. **`StemPlayer.test.tsx` was touched, minimally.** Its `FakeEngine`
    `implements StemPlayerEngine`, so widening the interface breaks the type
    check until it gains a `getStemBuffer` returning `null`. One line; the
    component and its behaviour are untouched.

11. **Noticed, not touched.** `StemAudioEngine.getStemBuffer` returns the
    live buffer object rather than a copy, so a caller could in principle
    mutate a decoded stem through `getChannelData`. Cloning millions of samples
    per read is the wrong trade for a local single-page app; the TSDoc states
    the ownership rule instead. Nothing in the repo mutates channel data.
