# [050] Stem timeline with per-stem waveform lanes

Branch: `050-stem-timeline-lanes`
Status: PR OPEN
Dependencies: 049
PR: #74

## Objective

The stem player's transport becomes an Audacity-like timeline: one waveform
lane per stem drawn from the real decoded audio on a shared time axis, a
playhead, click/drag and keyboard seeking, and a timeline that *is* the
accessible seek control. The range slider and the stem list it replaced are
gone.

## Scope

- **`StemTimeline.tsx` / `.css`** — the container: lane headers, the ruler, the
  lane stack, the playhead, and the transparent interaction layer that carries
  `role="slider"`.
- **`TimelineLane.tsx`** — one `React.memo`'d, viewport-sized canvas per stem,
  painted through 049's `drawWaveform`.
- **`TimelineRuler.tsx`** — DOM tick labels from `tickStepSeconds` +
  `formatDuration`, `aria-hidden`.
- **`useTimelineGeometry.ts`** — `{ zoom, scrollSeconds }` state, one
  `ResizeObserver` for the strip width, `matchMedia` device-pixel-ratio watch.
- **`useWaveformPeaks.ts`** — per-stem base peaks via `engine.getStemBuffer` +
  `computePeaksChunked`, abortable, cached per engine.
- **`StemPlayer.tsx`** — renders the timeline, keeps Play/Pause and the
  `formatDuration` readout, owns the displayed position.
- **E2E** — `Workflow.timeline` / `.seek` / `.playhead` / `.seekToFraction()`;
  `separation.spec.ts` migrated from `fill()` to a click and a key press.

## Out of scope

- Zoom and pan controls (051), audible scrub preview (052), loop regions (053),
  level faders (054).
- Any engine, backend, contract or `index.css` change.

## Expected modules/files

- `frontend/src/components/StemTimeline.tsx`, `.css`, `.test.tsx` — new
- `frontend/src/components/TimelineLane.tsx` — new
- `frontend/src/components/TimelineRuler.tsx` — new
- `frontend/src/components/useTimelineGeometry.ts` — new
- `frontend/src/components/useWaveformPeaks.ts` — new
- `frontend/src/components/StemPlayer.tsx`, `.css`, `.test.tsx` — rebuilt
  transport
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts` — migrated
- `docs/features/050-stem-timeline-lanes.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **Per-stem waveform lanes render from real decoded audio, for any stem
      count.** Lanes come from the result's `stems`, so two and four render
      through the same code; the samples come from `engine.getStemBuffer`
      through `computePeaksChunked`. A stem with `status === 'error'` gets a
      text placeholder instead of a canvas.
- [x] **One shared time axis, honestly drawn.** Every lane is positioned
      against `snapshot.durationSeconds`; a shorter stem paints across only its
      own fraction of the strip and leaves the rest empty.
- [x] **Click, drag and keyboard seeking, with exactly one `engine.seek` per
      pointer gesture.** Proved to fail first — output below.
- [x] **The timeline is the accessible seek control.** `role="slider"`,
      `tabIndex=0`, `aria-label="Seek"`, `aria-valuemin/max/now/valuetext`,
      `aria-disabled` until ready; ArrowLeft/Right (±1 s, ±5 s with Shift),
      Home, End, and Space for play/pause. Canvases and ruler are
      `aria-hidden`.
- [x] **Lanes repaint only on state changes; the playhead is a transform.** The
      lane effect depends on peaks, viewport, dpr, audibility and the stem's
      own length, and on nothing else; the playhead is one absolutely
      positioned div moved with `translateX`. Both are asserted.
- [x] **The old slider and stem list are gone, and what the tiers assert
      survives.** `.stem-player-stem-name` and `.stem-player-time` keep their
      classes, Mute/Solo keep their accessible names and `aria-pressed`.
- [x] **Five frontend gates green**, E2E migrated (see Required tests for what
      was actually executed).

## Required tests

**Unit — `StemPlayer.test.tsx` (54)**, extended rather than rewritten. The
`FakeEngine` now serves `FakeAudioBuffer`s from `stemBytesWithSamples`, so the
peak path runs for real in jsdom; a `FakeResizeObserver` and a fixed 400 px
`getBoundingClientRect` make `x → seconds` exact arithmetic; `installFakeCanvas`
records what each lane would have painted.

- lanes: one per stem for two- and four-stem results; drawn in the accent
  colour; redrawn in the muted colour when a stem is silenced; a placeholder
  and no canvas for a failed stem; repainted on a resize but **not** on
  animation frames.
- gestures: a seven-move drag commits one seek at the release position (and,
  over the real engine, rebuilds the source graph once); a motionless click is
  one seek; a second `pointerup` is ignored; `pointercancel` commits nothing
  and snaps back to the audio clock; the readout follows the drag and returns
  to the clock afterwards.
- keyboard: ArrowRight/ArrowLeft/Shift+ArrowRight/Home/End each commit one
  discrete seek; Space plays and pauses and seeks nothing.
- accessibility: `aria-valuemin/max` span the mix; `aria-valuenow` and
  `aria-valuetext` track the playhead; `aria-disabled` while decoding, and a
  gesture then commits nothing.
- playhead: `style.transform` is `translateX(200px)` at 30 s of a 60 s mix on
  a 400 px strip.

**Unit — `StemTimeline.test.tsx` (9)**, the component alone, one lane at a time
(the fake canvas is shared by every canvas in the tree, so attributing
rectangles to a lane means rendering one):

- a full-length stem paints one column per pixel of the strip, the last at
  `x = width - 1`; a half-length stem paints exactly half of them;
- nothing is painted before the peaks arrive, and the lane still exists;
- a failed stem gets the placeholder and a disabled Mute;
- changing one stem's audibility repaints **only** that lane, in the muted
  colour — the memoised sibling does not repaint;
- the ruler labels a minute at ten-second steps and is `aria-hidden`, as are
  the canvases;
- headers forward mute/solo by name and say `Loading…` while a stem decodes.

**E2E — `separation.spec.ts`.** The playback stage now asserts one canvas per
catalog stem, clicks a quarter of the way along the timeline and expects
`0:15` in `.stem-player-time` and in `aria-valuetext`, then presses
`ArrowRight` and expects `0:16`. `.stem-player-stem-name` still resolves to the
catalog's stem names (also asserted twice in `resync.spec.ts`, unchanged).

The tier was run locally against the real backend and the fake separator
(`npm run e2e`), and it earned its keep: the first run failed on a layout
defect no jsdom test could have seen (note 11 below).

### Proved to fail first

The one-seek-per-gesture regression was run against a deliberately broken
`updateGesture` that committed a seek on every `pointermove`, then reverted:

```text
× commits one seek for a whole drag, not one per pointer event
    AssertionError: expected [ 6, 13, 21, 27.999999999999996, …(3) ]
    to deeply equal []
    + [ 6, 13, 21, 27.999999999999996, 34, 40.99999999999999,
        46.99999999999999 ]

× rebuilds the source graph once per drag, not once per pointer event
    AssertionError: expected [ FakeSourceNode{ …(6) }, …(31) ] to have a
    length of 4 but got 32
```

Thirty-two source nodes for one drag across a four-stem mix is what the
finding sounds like: seven teardown-and-reschedule cycles, each opening a
fresh 50 ms lookahead.

## Notes / decisions

1. **Lane rows come from the result, not from the snapshot.** *Deliberate
   deviation from the assignment,* which said `snapshot.stems`. The rows have
   to exist while the stems are still decoding — the engine's snapshot is empty
   until then, and both the unit suite and the E2E tier find the stem names
   before anything has decoded. So rows come from `SeparationResult.stems`
   merged with whatever the snapshot knows, exactly as feature 023 did. The
   count still comes from the data and never from a literal, which is what
   AGENTS.md principle 6 asks for.

2. **The one-seek ref moved into `StemTimeline`, next to the pointer
   handlers.** 023 kept it in `StemPlayer` because the range input's
   `onChange`/`onPointerUp` lived there. The mechanism is unchanged — a ref
   cleared *synchronously* before the seek, so a duplicate release event is a
   no-op — but it now sits with the three functions feature 052 will extend
   (`beginGesture` / `updateGesture` / `commitGesture`). The player keeps the
   *displayed* position, because that is what the readout and the playhead
   both render.

3. **A cancelled gesture snaps back to the clock.** `pointercancel` commits
   nothing and calls `engine.currentTime()`; the previous slider had no
   equivalent, because a cancelled native drag simply left the input's value
   where it was.

4. **The silenced lane's reduced alpha is CSS, not a fill colour.** 049's
   `WaveformDrawContext` exposes `fillStyle`, `clearRect`, `fillRect` and
   `setTransform` — no alpha channel and no `globalAlpha`. So a silenced lane
   is drawn in `--color-text-muted` (which is what the tests assert) and the
   row carries `opacity: 0.45`. That also means the state reads before the
   canvas repaints, which is the behaviour the assignment asked for.

5. **The canvas is resized on every paint, deliberately.** Assigning
   `canvas.width`/`height` resets the backing store *and* the transform. That
   is what guarantees a repaint of a stem that got shorter (or a viewport that
   got narrower) cannot leave the previous, longer waveform showing past the
   end of the new one — `drawWaveform` only clears the region it is about to
   fill.

6. **`getContext` is asked for only when there is something to draw.** The
   effect assigns the canvas size, then returns early if the peaks have not
   arrived. Without that, every suite that renders the player without decoded
   audio printed a jsdom "not implemented: getContext" line per lane.

7. **The viewport object is memoised.** `clampViewport` returns a fresh object,
   and the lanes are `React.memo`d over it; rebuilding it on every render would
   have repainted every canvas on every engine snapshot — which is to say on
   every mute toggle — and quietly defeated the whole memoisation.

8. **Peaks are keyed on the *names* of the loaded stems, not the array.** The
   snapshot is rebuilt on every notify, so a new array with the same names
   arrives constantly; the effect depends on the NUL-joined name list instead,
   and a per-engine ref cache means an already-computed stem is never
   recomputed. Raw channel data never enters React state — only two
   `Float32Array`s of 8192 entries per stem.

9. **Attachment points for 051–054.** The region map is at the top of
   `StemTimeline.tsx`. In short: `.stem-timeline-toolbar` is the corner cell
   above the lane headers (051's zoom controls);
   `useTimelineGeometry.applyToViewport` takes 049's pure `zoomedAt` / `panned`
   and is the only way the window changes (051); the ruler is its own component
   and its own box (053's loop drag); the lane header is where a fader goes
   (054); and 052 extends the three gesture functions rather than adding a
   second seek path. The base peak count (8192) and 049's `needsHighResTile`
   are what 051 will use to decide when a zoom needs recomputing from samples.

10. **Known limitation, carried forward from 023.** The playhead does not
    persist across a phase change: leaving `inspect` and coming back rebuilds
    the engine at 0:00. Unchanged by this feature.

11. **The lane header is deliberately compact, and that is load-bearing.** The
    header row's height is pinned to `LANE_HEIGHT_PX` so the two columns stay
    aligned — which means content that did not *fit* would be painted over the
    next header and would swallow its clicks. The E2E tier caught exactly that
    on the first run: `Mute vocals` was un-clickable because the `drums`
    header's name span sat on top of it. The fix was to give the header two
    compact rows (a truncating name plus its duration, then the toggles),
    raise the lane height to 64 px, and add `overflow: hidden` as the guard.
    A future feature adding anything to a lane header — 054's faders — has to
    keep it inside that box or raise `LANE_HEIGHT_PX` with it.

12. **A quiet stem draws as a thin band, and that is right.** The E2E
    fixtures' stems peak at about −19 dBFS, so the lane shows a narrow band
    around the midline rather than a full-height block. The scale is linear
    amplitude, deliberately: it is the same picture Audacity draws, and 049's
    one-pixel minimum keeps silence a hairline rather than a gap.

13. **The two promise-backed waits in `StemPlayer.test.tsx` carry an explicit
    ceiling.** RTL's default is one second, and the result fetch reaching the
    DOM was seen to exceed it twice on a machine running the E2E tier at the
    same time. They are still conditions and a passing run spends none of the
    budget; this is the same reasoning `playwright.config.ts` gives for its own
    20 s `expect` timeout.

14. **Noticed, not touched.** `StemPlayer` still fetches the result once with
    no retry — that is feature 048, which was not on `dev` when this branch was
    cut, so the error branch was left exactly as it was to keep the merge
    clean.
