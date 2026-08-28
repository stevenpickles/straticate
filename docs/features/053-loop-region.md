# [053] Loop / A-B region playback

Branch: `053-loop-region`
Status: PR OPEN
Dependencies: 050, 051
PR: —

## Objective

A user marks a passage — by dragging across the timeline ruler, by
shift-dragging the lanes, or from the transport's buttons — and playback loops
it, sample-accurate across every stem, at whatever zoom the region was drawn
at.

## Scope

- **`audio/engine.ts`** — `setLoopRegion(start, end)` / `clearLoopRegion()`,
  `loopRegion` in the snapshot, `loop` / `loopStart` / `loopEnd` on the
  structural `AudioEngineSourceNode`, the flags applied in `startSources`
  before the one shared `start(when, offset)`, and the wrap in `currentTime()`.
- **`test/fakeAudioContext.ts`** — `FakeSourceNode` extended with the three
  loop fields and a `stops` array recording every `stop(when)`.
- **`components/StemTimeline.tsx`** — the region gesture (ruler drag, shifted
  lane drag, two edge handles), the band overlay, and the three new props
  (`loopRegion`, `onSetLoopRegion`, `onClearLoopRegion`).
- **`components/StemPlayer.tsx`** — "Loop start", "Loop end", "Clear loop" and
  the `Loop m:ss – m:ss` badge in the transport row; the engine's `loopRegion`
  passed down as the single source of truth.
- **E2E** — `Workflow.ruler` / `.loopBadge` / `.clearLoop` / `.dragRuler()`,
  and a loop stage in `separation.spec.ts`.

## Out of scope

- The audible scrub preview (052 — `AudioEngineParam` and the preview path are
  untouched), zoom and pan beyond what drawing a region needs, faders, the
  backend, and `index.css`.
- Loop count limits, a loop-crossfade, snapping the region to bars or to
  transients, and persisting a region across a phase change.

## Expected modules/files

- `frontend/src/audio/engine.ts`, `engine.test.ts`
- `frontend/src/test/fakeAudioContext.ts`
- `frontend/src/components/StemTimeline.tsx`, `.css`, `.test.tsx`
- `frontend/src/components/StemPlayer.tsx`, `.css`, `.test.tsx`
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts`
- `docs/features/053-loop-region.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **Every stem loops on the same boundary.** A region set before or during
      playback puts `loop`, `loopStart` and `loopEnd` on every stem's source
      before one shared `start(when, offset)`, so the browser wraps the whole
      mix on one sample. Asserted through the UI over the real engine as well
      as in the engine suite.
- [x] **The playhead agrees with what is audible.** `currentTime()` wraps into
      the region however many passes have gone by (raw 25 s and raw 45 s of a
      10–20 s loop both read 0:15). **Proved to fail first — output below.**
- [x] **A region is a trap, not a fence.** The engine never seeks to enter one:
      playback started before it runs into it and wraps; a seek past `loopEnd`
      plays through to the end of the mix and settles there via the existing
      `onended`, with the region still set.
- [x] **Set and cleared while playing, without losing the position.** Both
      rebuild through `startSources(currentTime())` — the position under the
      *outgoing* region — inside the same try/catch → `transportError` pattern
      `seek` uses. While paused they store and notify only.
- [x] **A ruler drag draws a region and commits once**, in absolute seconds,
      normalising a right-to-left drag; a plain click on the ruler (under 4 px
      of travel) clears the region and seeks, as Audacity does.
- [x] **Shift+drag over the lanes is the same gesture**, through the same three
      functions the seek gesture uses, and seeks nothing.
- [x] **The band moves with the window, and its edges are draggable.** Position
      and width come from `timeToX` at the current viewport; an 8 px
      `ew-resize` handle on each visible edge commits one `setLoopRegion` on
      release, and collapsing an edge onto the other clears the loop.
- [x] **A region drawn while zoomed and panned lands on the right seconds** —
      100 px into a 40 s window scrolled to 0:10 is 0:20, not 0:10 and not
      0:15.
- [x] **A keyboard and screen-reader path exists.** Three transport buttons
      plus a `Loop 0:12 – 0:34` badge in an `aria-live="polite"` region that is
      mounted whether or not there is a loop. "Clear loop" is disabled when
      there is none.
- [x] **Five frontend gates green**, and the E2E tier run locally: 26 passed,
      including the new loop stage.

## Required tests

**Unit — `audio/engine.test.ts` (16 new).** Native flags on every source at one
shared `when`; the `currentTime()` wrap at one pass and at three; no wrap for a
generation started past the region, which still ends through `onended`; clear
while playing rebuilding at the *wrapped* position with `loop === false`; set
while playing rebuilding at the current position; paused set storing without
building sources, and looping from the next `play()`; survival of a
pause/resume; no rebuild for the region it already has; degenerate, inverted,
too-short and entirely-past-the-end regions all clearing; clamping to the mix;
a short stem clamped to its own duration and a stem that ends before the region
left unlooped; `load()` forgetting the region; notification exactly on change;
and disposal ignoring loop commands.

**Unit — `components/StemTimeline.test.tsx` (11 new).** One commit per ruler
drag with nothing committed mid-drag; an inverted drag normalised; a plain
click clearing and seeking; a shifted lane drag producing a region and neither
a seek nor a scrub; a handle drag committing once; a collapsed edge clearing; a
cancelled gesture committing nothing; a second release ignored; a region
dragged at zoom 1.5 scrolled to 0:10 committing 0:20–0:40; the band's
`left`/`width` read off the viewport before and after a zoom; and a region
panned off the window drawing nothing.

**Unit — `components/StemPlayer.test.tsx` (9 new).** The badge formatted from
the snapshot and its live region; "Loop start" and "Loop end" and their
fallbacks; the crossing case widening rather than swapping; "Clear loop"
disabled without a region and clearing with one; every loop control disabled
while the stems decode; a ruler drag reaching the engine through the whole
player; and — over the **real** engine with a fake `AudioContext` — a ruler
drag putting identical loop boundaries and one shared `when` on all four
sources, with the readout then wrapping to 0:15.

**E2E — `separation.spec.ts`.** A stage between zoom and export: with the whole
60 s file fitted, a drag from a tenth to four tenths of the ruler shows
`Loop 0:06 – 0:24` (asserted with a second of slack for the pixel the drag
lands on) and enables "Clear loop"; clicking it removes the badge and disables
the button again. Run locally against the real backend and the fake separator:
**26 passed**.

### Proved to fail first

The wrap was removed from `currentTime()` and the regression run against the
result:

```text
 FAIL  src/audio/engine.test.ts > StemAudioEngine loop regions > wraps the
 playhead back into the region, however many passes on
AssertionError: expected 25 to be close to 15, received difference is 10,
but expected 5e-7
 ❯ src/audio/engine.test.ts:916:34
    914|     // Raw 25 s of material is five seconds into the second pass.
    915|     context.currentTime = 25 + LOOKAHEAD
    916|     expect(engine.currentTime()).toBeCloseTo(15, 6)
       |                                  ^
```

A playhead reading 0:25 while the speakers are at 0:15 is the whole feature
failing quietly: the readout, the `aria-valuetext`, the drawn playhead and
auto-follow all derive from that one number.

## Notes / decisions

1. **The loop is the platform's, not a timer's.** `loop`, `loopStart` and
   `loopEnd` are set on every source *before* the one shared
   `start(when, offset)` the engine already schedules, so the wrap happens
   inside the audio thread on the same sample for every stem. The alternative —
   watching the clock and rescheduling the mix at each pass — would reopen a
   scheduling lookahead every loop (an audible gap), and would drift.
   `startSources` was already the one place a generation is built, so applying
   the region there is the whole change.

2. **A region is a trap, not a fence, and the engine never auto-seeks.**
   Playback that starts before the region runs forward into it and then loops;
   playback that starts at or after `loopEnd` never re-enters it — that is the
   Web Audio behaviour, not a rule invented here — and plays to the end. The
   alternative (seeking to `start` the moment a region is set) was rejected
   because setting a loop while listening would jump the audio backwards under
   the user, and because "play the region from the top" is one `seek(start)`
   the UI can make when it *means* it. `currentTime()` encodes the same rule:
   it wraps only when `startOffset < region.end`.

3. **A looping mix never ends, and that is correct.** `onended` is still
   attached to the longest stem only; a looping source simply never fires it.
   So a mix with a live region does not settle at its duration — it loops until
   the region is cleared or the transport is moved past it. Clearing while
   playing rebuilds at the position `currentTime()` reports *under the outgoing
   region*, so the audio carries on from where it actually was rather than from
   the raw elapsed time.

4. **The equal-stem-length assumption, and the belt-and-braces `min`.** Exact
   sync across the mix assumes every stem is the same length, which is what a
   separation produces: one input file in, N stems of that duration out. The
   code does not *rely* on it — `loopEnd` is `min(region.end, buffer.duration)`
   and a stem that ends before `region.start` is left unlooped entirely — but a
   genuinely short stem then wraps on its own boundary and drifts against the
   rest. That is a deliberate degradation (the alternative is silencing it), and
   it is unreachable through the app as it stands.

5. **Degenerate means "clear", at both layers, with two different
   thresholds.** The engine clears for anything under `MIN_LOOP_SECONDS`
   (10 ms) after clamping — which covers an inverted pair, an empty one, and
   one that clamped down to nothing at the end of the mix — because a
   ten-millisecond loop is a buzz, not a passage. The timeline clears for
   anything under 50 ms *before* calling, because that is the width a user can
   land on by accident when dragging two handles together. Setting the region
   the engine already has is a no-op rather than a rebuild, so a handle dragged
   back where it started costs nothing audible.

6. **Button defaults: "loop from here", "loop to here".** "Loop start" sets the
   start at the playhead and keeps the region's end when that end is still
   ahead of it, falling back to the end of the mix — which is also what it does
   with no region at all. "Loop end" is the mirror image, falling back to the
   start of the mix. When the two would cross, the region *widens* to the
   fallback rather than swapping the edges: swapping would move the edge the
   user did not touch, which is the one thing a user pressing "Loop start" is
   certain not to have asked for. The engine's degenerate rule then covers the
   remaining corner (pressing "Loop end" at 0:00 clears).

7. **The overlay is rendered after the seek surface, and is transparent.** The
   band spans the ruler and every lane, so it lies over the interaction layer;
   `pointer-events: none` on the band with `auto` on the two handles is what
   keeps a click *inside* a loop a seek while still letting the edges be
   grabbed. A handle scrolled out of the window is not rendered at all — its
   box edge is the window's edge, and dragging it would move a second the user
   cannot see.

8. **The gesture is the seek gesture's mechanism, extended, not a second
   one.** `beginGesture` branches on `shiftKey`; `updateGesture`,
   `commitGesture` and `abandonGesture` branch on whether a region drag is in
   flight. One synchronously-cleared ref per gesture kind, one commit on
   release, and a keyboard seek clears both — the same defences 050 built for
   the seek path, for the same reason: `setLoopRegion` while playing rebuilds
   every source node.

9. **Pixels are measured against the strip, not against the element.** An edge
   handle is 8 px wide, so `event.currentTarget.getBoundingClientRect()` — what
   the seek path uses, correctly, on a full-width surface — would answer
   nonsense for a handle. Region gestures go through `stripX`, which measures
   against the track strip the viewport is defined over. (jsdom's stubbed rect
   is the same for every element, so no unit test can catch this one; it is
   arithmetic, not behaviour.)

10. **A note left for 052.** The scrub-preview grains must **not** set `loop`.
    A grain auditions one position and stops; a looping grain would repeat a
    fragment under the pointer for as long as it was held. The region belongs
    to the transport, not to a preview — recorded here and in the engine's
    module docstring, where 052's author will be working.

11. **The badge's live region is always mounted.** A live region has to be in
    the DOM *before* its content changes for a screen reader to announce the
    change, so `.stem-player-loop-status` renders unconditionally and only the
    `.stem-player-loop-badge` inside it comes and goes. The E2E tier therefore
    asserts on the badge's *count*, not on empty text.

12. **Noticed, not touched.** The transport row now wraps (`flex-wrap`) because
    five controls do not fit a narrow panel on one line; nothing else in the
    player's layout was changed. Still true and still unaddressed: 050's
    known limitation that the playhead (and now the loop region) does not
    survive a phase change — leaving `inspect` and coming back rebuilds the
    engine at 0:00 with no region — and the two jsdom `getContext` warnings
    `StemPlayer.test.tsx` prints, which predate this branch.

13. **Auto-follow and a wrapping playhead (review finding).** When the window
    is zoomed narrower than the region's span, playback approaching
    `loopEnd` page-flips the window forward (051's rule: follow only when the
    playhead leaves the view), and the wrap back to `loopStart` reads as
    out-of-view again — so the window flips back, once per loop pass. It
    follows 051's documented page-flip design and is not incorrect, but it
    is visually busy in that one zoom regime. Options if it grates in use:
    suppress auto-follow while a region is set, or zoom-to-region on loop
    start. Left for a follow-up decision; no test pins the interaction.

14. **Pause while wrapped is covered by composition and now directly.**
    `pause()` stores `currentTime()`, whose wrap is independently tested; a
    dedicated test ("pauses at the wrapped position and resumes from it",
    added at review) also pins the full sequence: pause past `loopEnd` holds
    the wrapped readout, and resume restarts every source from that wrapped
    offset with the loop flags intact.
