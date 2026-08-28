# [051] Timeline zoom and pan

Branch: `051-timeline-zoom-pan`
Status: PR OPEN
Dependencies: 050
PR: #78

## Objective

The stem timeline stops being a fixed picture of the whole file: it zooms
horizontally about the point the user is looking at, pans, keeps a moving
playhead in view, and redraws its waveforms from the samples once a pixel
column is finer than the whole-file peaks are.

## Scope

- **`useTimelineGeometry.ts`** — five named movements over 049's pure
  transforms: `zoomIn(anchorX?)`, `zoomOut(anchorX?)`, `zoomToFit()`,
  `panBy(deltaSeconds)`, `scrollTo(seconds)`. Still the only place a window
  changes, and still clamped by `clampViewport` on every one of them.
- **`StemTimeline.tsx`** — a native non-passive `wheel` listener on the track
  strip (Ctrl+wheel zooms about the cursor, a plain or shifted wheel pans),
  three toolbar buttons in the corner cell, `+`/`-` on the focused surface, a
  draggable scroll thumb under the ruler, playhead auto-follow, and the window
  published as `data-zoom` / `data-scroll-seconds` on the strip.
- **`timelineGeometry.ts`** — `maxZoom` exported (a control has to know where
  the limit is), plus `tileRangeFor` / `sameTileRange`: the slice of a stem a
  lane is about to paint, which doubles as a tile's cache key.
- **`useWaveformPeaks.ts`** — `useWaveformTiles`: per-stem high-resolution
  tiles computed from `engine.getStemBuffer` through `computePeaks`, scheduled
  on an animation frame and kept in a four-deep per-stem LRU.
- **`TimelineLane.tsx`** — draws a tile when one covers exactly the window it
  is painting, and aggregates the base peaks otherwise.
- **`TimelineRuler.tsx`** — verified against a moved window; the label that
  would be clipped at the right edge now hangs to the left of its mark.
- **E2E** — `Workflow.strip` / `.ticks` / `.zoomIn` / `.zoomOut` / `.zoomFit` /
  `.window()`, and a zoom stage in `separation.spec.ts`.

## Out of scope

- Audible scrub (052), loop regions (053), level faders and the lane header
  (054 — a parallel branch owns that region).
- Any engine, backend, contract or `index.css` change. Vertical zoom, a
  waveform worker, and persisting the window across a phase change.

## Expected modules/files

- `frontend/src/components/StemTimeline.tsx`, `.css`, `.test.tsx`
- `frontend/src/components/useTimelineGeometry.ts`
- `frontend/src/components/useWaveformPeaks.ts`
- `frontend/src/components/TimelineLane.tsx`, `TimelineRuler.tsx`
- `frontend/src/components/timelineGeometry.ts`, `.test.ts`
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts`
- `docs/features/051-timeline-zoom-pan.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **Ctrl+wheel zooms about the cursor**, through a native non-passive
      listener that `preventDefault`s (so the browser does not zoom the page
      instead) and is removed with the element it was attached to. 100 px along
      a 400 px strip of a minute is 0:15, and one notch leaves 0:15 a quarter
      of the way along a 40 s window — asserted in seconds, not pixels.
- [x] **Toolbar and keyboard zoom, anchored on the playhead.** "Zoom in",
      "Zoom out" and "Zoom to fit" in the corner cell, `+`/`=` and `-`/`_` on
      the focused timeline. All four anchor on the playhead when it is on
      screen and on the middle of the window when it is not.
- [x] **Both ends clamp, and say so.** Zooming out stops at the whole file;
      zooming in stops at one second on screen (a zoom of 60 for a minute-long
      mix). "Zoom out" and "Zoom to fit" are disabled at fit, "Zoom in" at the
      limit.
- [x] **Pan by wheel and by thumb.** A plain or shifted wheel pans by whichever
      delta the device sends, and only when there is something to pan — a
      fitted timeline leaves the wheel to the page, un-`preventDefault`ed. The
      thumb mirrors `visibleSeconds/duration` and `scrollSeconds/duration` and
      drags against the whole file, with the gesture ref cleared on release.
- [x] **A moving playhead is followed; a panned window is not fought.**
      Auto-follow fires only when the position has changed *and* left the
      window, and flips the window so the playhead reappears a tenth of the way
      in.
- [x] **A click still lands on the second it points at, zoomed and panned.**
      Proved to fail first — output below.
- [x] **The ruler rescales and repositions.** The step comes from
      `tickStepSeconds` at the current scale and the first tick from
      `scrollSeconds`; a fitted minute keeps its ten-second ladder and a 5 s
      window gets a one-second one.
- [x] **Zoomed past the base resolution, lanes are drawn from samples**, once
      per frame rather than once per wheel event, from a four-deep per-stem
      LRU, and never at fit zoom.
- [x] **Five frontend gates green**, and the E2E tier run locally (25 passed,
      including the new zoom stage).

## Required tests

**Unit — `timelineGeometry.test.ts` (47)**, extended for the pure functions
this feature added: `maxZoom` is the zoom `clampViewport` stops at and is 1 for
material shorter than the minimum window; `tileRangeFor` is the window itself
for a full-length stem, stops at a stem that ends inside the window, and is
`null` for one that ended before it; `sameTileRange` treats a one-second pan as
a different picture.

**Unit — `StemTimeline.test.tsx` (22)**, thirteen of them new. Every zoom case
reads the window off `data-zoom` / `data-scroll-seconds`, which is what the
ruler, the lanes and every seek are derived from:

- zoom: Ctrl+wheel about the cursor (and the ruler's labels and the first
  tick's `translateX` with it); a finer tick ladder further in; the toolbar and
  `+`/`-` anchoring on the playhead; Fit returning the whole file *and* the
  original ruler; clamping at both ends with the buttons' disabled states; a
  fitted timeline leaving a wheel un-`preventDefault`ed for the page.
- pan: plain, shifted and horizontal wheels, and the clamp at the end of the
  file; a thumb drag with its width and offset, and a stray move after release
  scrolling nothing; auto-follow flipping the window, then *not* moving when a
  pan is followed by the same position.
- seeking: a click at 300 px of a zoomed, panned strip commits 0:40 — the
  reading that ignores the scroll says 0:45 and the one that ignores the zoom
  says 0:55.
- tiles: eight wheel events read the samples **once**, on the next frame; a pan
  away and back reads nothing the second time; and at maximum zoom the impulse
  in the fixture is painted across four columns, where the base peaks would
  have smeared it across fifteen.

**E2E — `separation.spec.ts`.** A new stage between playback and export: four
"Zoom in" clicks magnify by 1.5⁴ anchored on the playhead the previous stage
left at 0:16, the ruler drops off the ten-second grid, a click a quarter of the
way along the strip lands within a quarter-second of
`scrollSeconds + visibleSeconds/4` (and the readout agrees), and "Zoom to fit"
restores the window and the original tick labels exactly. The tier was run
locally against the real backend and the fake separator: 25 passed.

### Proved to fail first

The seek regression was run against a deliberately broken `xToTime` that
dropped `scrollSeconds` from its answer, then reverted:

```text
× seeks to the absolute second under the pointer, zoomed and panned
    AssertionError: expected [ 30 ] to deeply equal [ 40 ]

  - Expected
  + Received

    [
  -   40,
  +   30,
    ]
```

Ten seconds of scroll, silently dropped — the same class of defect as clicking
a zoomed waveform in an editor and hearing the wrong bar.

## Notes / decisions

1. **The wheel listener is native, and that is not an optimisation.** React
   registers `onWheel` passively, so `preventDefault` inside it is ignored and
   Ctrl+wheel zooms the *browser*. The listener is attached in an effect to the
   track strip, which is held in state (not a ref) precisely so the effect can
   be woken when it mounts; the handler itself lives in a ref updated on every
   render, so a viewport change does not detach and reattach a DOM listener.

2. **Zoom anchors on the playhead, not the viewport centre.** For the wheel the
   anchor is the cursor, which is what makes it feel attached to the material.
   For a button or a key there is no cursor, and the assignment left the choice
   open: the playhead won, because it is the one point on screen the user is
   already tracking. When it is off screen — after a pan — the anchor falls
   back to the middle of the window.

3. **Auto-follow is a page flip, and it is gated on the *position* moving.**
   Following smoothly would mean writing the viewport on every animation frame,
   which would repaint every lane sixty times a second — the exact cost feature
   050 was built to avoid. So the window jumps when the playhead leaves it, and
   puts it a tenth of the way in (`FOLLOW_MARGIN`), which leaves most of the
   new window ahead of the audio. The gate is what stops it fighting a manual
   pan: the effect returns unless `positionSeconds` has actually changed, so
   panning away from a paused playhead sticks until playback next carries it
   out of view.

4. **No `overflow-x`, deliberately.** The canvases are viewport-sized rather
   than file-sized (050's rule), so there is no wide content for the browser to
   scroll; a native scrollbar would need a strip tens of thousands of pixels
   wide per stem. The thumb is therefore drawn: its width is
   `visibleSeconds/duration`, its offset `scrollSeconds/duration`, and its drag
   maps a pixel of travel to `duration/widthPx` seconds whatever the zoom. It
   uses the seek gesture's synchronously-cleared ref, though for a different
   reason: scrolling is not seeking, so a continuous drag *should* scroll
   continuously; the ref is about release, not about coalescing.

5. **The toolbar cell grew by the thumb's height, and had to.** The header
   column and the lane column stay aligned only because the corner cell is
   exactly as tall as everything above the first lane. Inserting a 10 px
   scrollbar row under the ruler therefore raised the toolbar to
   `RULER_HEIGHT_PX + SCROLLBAR_HEIGHT_PX`, and moved the interaction surface's
   `top` by the same amount so the thumb is not covered by the seek layer.
   Feature 054 adding anything to a *lane* header is unaffected; anything added
   to the corner cell has to keep inside that box (050, note 11).

6. **A tile is keyed on the range it covers, and the lane checks the key.** The
   hook and `TimelineLane` both derive the range from the viewport with the
   same pure function, so a tile that arrives for a window the user has already
   left simply does not match and the lane falls back to the base peaks. That
   is what makes the frame's-worth of latency safe: a stale tile is never
   stretched over the wrong seconds, it is ignored.

7. **The sample rate is cached per stem, so a coarse viewport reads no
   buffers.** `needsHighResTile` needs a rate, and asking the engine for a
   buffer on every viewport change to get one would have made "no tile needed"
   cost a buffer lookup per stem per wheel event. A decoded stem's rate never
   changes, so it is read once and remembered — which is also what lets the
   tests count buffer reads and prove the debounce.

8. **Four tiles per stem.** Enough for the movements a user makes in a second
   (in and out, away and back) at a few hundred kilobytes for a four-stem mix.
   The eviction policy is "move a hit to the front, trim the tail", which is a
   proper LRU because there is only one access path.

9. **The ruler needed no arithmetic fix.** It already derived its step from
   `tickStepSeconds` at the current scale, started at the first multiple of
   that step at or after `scrollSeconds`, and placed each label with `timeToX`
   — none of which assumes a window starting at zero. What it did need was the
   cosmetic fix the assignment allowed: the final label of a fitted view sits
   on the last pixel and was clipped by the strip's `overflow: hidden`, so a
   tick within 40 px of the right edge now hangs its label to the left of its
   mark (`.stem-timeline-tick-flush`).

10. **Zoom keys work before the transport does.** `+` and `-` are handled ahead
    of the `ready` guard: zoom is a view control, and a user looking at lanes
    that are still decoding has no reason to be refused. Seeking, Space and the
    arrows stay behind the guard exactly as 050 left them.

11. **Noticed, not touched.** The playhead spans the scroll thumb's row as well
    as the ruler and the lanes, because it is one absolutely positioned div
    from `top: 0` — visible as a hairline crossing the thumb's track. Splitting
    it would cost a second element and a second transform per frame; it looked
    right in the browser during the E2E run, so it was left alone. Also
    unchanged: 050's known limitation that the playhead does not survive a
    phase change, and the two `getContext` warnings jsdom prints during
    `StemPlayer.test.tsx` (present on `dev` before this branch, confirmed by
    running the suite against a stashed tree).
