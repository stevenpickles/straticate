# [067] Lane height + fader accessibility

Branch: `067-lane-height-a11y`
Status: PR OPEN
Dependencies: 050, 054
PR: #94

## Post-review corrections

A cross-model review of this feature's first merge confirmed five findings,
all applied on this branch:

1. **(Headline) The `resize`-refresh claim was false.** `useRootFontSize`
   refreshed the canvas backing store's pixel height on window `resize`, on
   the theory that a root-font change reflows the page and fires it. A
   controlled Chromium probe found `resize` never fires for a font-size-only
   change, so a mid-session font change left the backing store stale while
   the lane's CSS box grew — a real vertical stretch of the waveform for the
   large-font users this feature serves. **Fix:** `useRootFontSize` is
   deleted; `useMeasuredHeight` (new) measures the lane's own rendered box
   directly with `ResizeObserver`, this codebase's own idiom (already used
   for the tracks strip's width). See Notes §3 and "Fix 1's own fail-first
   run" below for the measured before/after.
2. **The fader hit-box spec sampled only its centre**, the same point the
   pre-fix ~11 px box's centre also passed at — vacuous. **Fix:**
   `frontend/e2e/layout.spec.ts` now also samples one pixel inside the top
   and bottom edges of the box.
3. **The fader's hit box could fall under 24 px below the 16–20 px range
   this feature targets** — measured `22.39px` at a 14 px root. **Fix:**
   `padding-block: max(0.45rem, calc((24px - 0.7rem) / 2))` in
   `StemTimeline.css` floors it at 24 px without changing the already-measured
   16–20 px figures. See Notes §7.
4. **B8's per-stem loop in `separation.spec.ts` passed vacuously on an empty
   stem list.** **Fix:** the stem count is asserted against the mode's own
   stems before the loop. See Notes §8.
5. **Test-title honesty for the deleted `useRootFontSize.test.ts`.**
   `useMeasuredHeight.test.ts` makes no "…and only on X" claim and pins its
   cleanup test to the specific observer instance it created, not a generic
   spy.

## Objective

Close the measured v0.2.0 layout debt 054 recorded and left for whoever
picked it up next (orphaned in turn by 051 and 055): the timeline's lane
header clips its own content at browser root font sizes above the 16 px
default, and the per-stem level fader's pointer target is far under the
24 px WCAG 2.2 SC 2.5.8 minimum. After this feature, `.stem-timeline-lane-header`
does not clip at 16/17/18/20 px root fonts and every level fader's hit box
reaches at least 24 px at all four, verified by a Playwright spec that
measures a real Chromium rather than asserting against arithmetic.

## Scope

- `frontend/src/components/TimelineLane.tsx` — `LANE_HEIGHT_PX` (fixed
  pixels) replaced with `LANE_HEIGHT_REM` (`rem`); a new `laneHeightPx` prop
  the canvas draw effect and backing-store sizing use instead.
- `frontend/src/components/useMeasuredHeight.ts` — measures one attached
  element's rendered height directly with `ResizeObserver`. **Replaces
  `useRootFontSize.ts`** (deleted), which multiplied `LANE_HEIGHT_REM` by the
  root font size read on window `resize`; a post-merge review found `resize`
  never fires for a font-size-only change, so that mechanism went stale
  mid-session. See "Post-review corrections" below.
- `frontend/src/components/StemTimeline.tsx` — computes `laneHeightPx` by
  attaching `useMeasuredHeight()`'s ref to the first lane's own rendered box
  (every lane shares the same height) and reading its measured value, passes
  it to `TimelineLane`; the lane header row and lane row's inline heights
  become `rem`.
- `frontend/src/components/StemTimeline.css` — the fader's hit box grows
  independently of its drawn track (C-item, WCAG 2.2 SC 2.5.8); the lane
  header's padding/gap retightened a second time (054 did it once) to fund
  the fader without raising `LANE_HEIGHT_REM` any further than measurement
  showed necessary; C10 — `.stem-timeline-scroll-thumb` lifted above
  `.stem-timeline-playhead` with one `z-index` line.
- `frontend/e2e/layout.spec.ts` (new; extended post-review) — the
  multi-root-font measurement spec that is this feature's actual acceptance
  mechanism, plus (post-review) edge-sampled fader clicks and a mid-session
  root-font-change stage that pins the canvas backing store against the
  `resize` finding below.
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts` — B10) a
  `chooseStereoHandling` helper and a `startSeparationWithRequest` variant
  that also returns the request body; B8) one new stage exercising "Fold to
  mono" through real job creation, closing the E2E-coverage gap 041 recorded
  (post-review: the stage now pins the stem count before iterating it, so an
  empty result cannot pass the loop vacuously).
- Unit tests: `useMeasuredHeight.test.ts` (new, post-review; replaces
  `useRootFontSize.test.ts`), extended `StemTimeline.test.tsx`.

## Out of scope

- `engine.ts`, `StemPlayer.tsx/.css/.test.tsx` — feature 064's, run in
  parallel this wave.
- Auto-follow logic (068) — the follow effect in `StemTimeline.tsx` was not
  touched.
- Session hoist (065), backend, any schema.

## Expected modules/files

- `frontend/src/components/TimelineLane.tsx`
- `frontend/src/components/useMeasuredHeight.ts`, `useMeasuredHeight.test.ts`
  (post-review; replaces `useRootFontSize.ts`/`useRootFontSize.test.ts`)
- `frontend/src/components/StemTimeline.tsx`, `StemTimeline.css`,
  `StemTimeline.test.tsx`
- `frontend/e2e/layout.spec.ts`, `frontend/e2e/app.ts`,
  `frontend/e2e/separation.spec.ts`
- `docs/features/067-lane-height-a11y.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **No lane header clips at 16/17/18/20 px root fonts.**
      `.stem-timeline-lane-header`'s `scrollHeight <= clientHeight` at every
      size, for every stem, measured in a real Chromium.
- [x] **Every level fader's pointer target reaches WCAG 2.2 SC 2.5.8's
      24 px minimum** at all four root sizes.
- [x] **A click at a fader's top edge, centre, and bottom edge all land on
      the fader** (`document.elementFromPoint`), not on an overflowing
      neighbour — the 050-era guard, now checked as a real measurement
      rather than assumed from "it still fits". (Post-review: the original
      spec sampled only the centre, the same point the pre-fix ~11 px box's
      centre also passed at — vacuous. Edge samples are what a 24 px *box*,
      as opposed to a 24 px *point*, actually promises.)
- [x] **The header row and lane row stay aligned to the pixel** for every
      stem, at every root size, with stem counts read from the mode rather
      than hardcoded.
- [x] **The canvas backing store still repaints only on state, never on a
      playhead frame** — `laneHeightPx` joined the draw effect's dependency
      list without adding a frame-driven dependency; playhead motion still
      reaches only a transformed `div`.
- [x] **The canvas backing store follows a mid-session root-font change**,
      verified directly (post-review, Fix 1) rather than assumed from CSS:
      `frontend/e2e/layout.spec.ts`'s last stage changes the root font after
      the page has already rendered and asserts the backing store's height
      changed to match the lane's new box, with no `resize` event dispatched
      by the test itself.
- [x] **C10**: the scroll thumb no longer sits under the playhead hairline.
- [x] **B8**: the "Fold to mono" stereo-handling radio is exercised through
      real job creation in the E2E tier, with the stem count pinned
      (post-review) before the per-stem loop that reads it.
- [x] Five frontend gates green (`npm ci` first); full E2E tier green,
      including the new spec.

## Required tests

**Unit — `useMeasuredHeight.test.ts`** (new, post-review; replaces
`useRootFontSize.test.ts`): reports `0` with nothing attached; measures the
attached element via `getBoundingClientRect` even with no `ResizeObserver`
(the real jsdom default this whole suite otherwise runs under); follows a
later resize reported by a stubbed observer's callback (a `contentRect`
delivered directly — no `resize` event anywhere in the test, which is the
point); disconnects **the same observer instance it created** — pinned by
reference, not a generic "some observer was told to disconnect" spy — both
on detach and on reattachment to a different element. (Fix 5: the deleted
`useRootFontSize.test.ts` had a case titled "…and only on a resize", a claim
that dissolved along with the hook; the replacement makes no such claim, and
its cleanup case is pinned to the specific instance rather than to
`expect.any(Function)`.)

**Unit — `StemTimeline.test.tsx`** (extended): a new case in the
`StemTimeline lanes` describe block extends the existing "repaints only the
lane whose audibility changed" invariant — a rerender that changes only
`positionSeconds` (what the player sends 60 times a second while playing)
produces zero `fillRect` calls, and (post-review) a stubbed
`ResizeObserver`'s callback reporting a taller `contentRect` on the lane box
— the real signal `useMeasuredHeight` uses, with no `resize` event dispatched
— produces a fresh repaint of every lane. The existing cases stay green
unmodified — none of them depended on the literal `64`.

**E2E — `frontend/e2e/layout.spec.ts`** (new; extended post-review): for a
completed four-stem job on the Inspect screen, at root font sizes
16/17/18/20 px (set via `document.documentElement.style.fontSize`, the same
mechanism a real browser setting drives), asserts all four original
acceptance criteria above. Waits are two `requestAnimationFrame`s after the
font-size mutation (the same idiom `app.ts`'s `renderedFrames` uses
elsewhere in this tier) — a real condition, never a sleep. Post-review: the
fader-click check now samples the top edge, centre, and bottom edge of the
box rather than only the centre; a final, separate stage sets the root font
once more mid-session (after the page has already rendered at 16 px) and
asserts the first canvas's backing-store `height` attribute changed to
match the lane's new rendered box — with no `resize` dispatched by the test,
since dispatching one by hand would exercise the deleted, broken mechanism
rather than the one that replaced it. See "Proved to fail first" below for
that stage's own fail-first run.

**E2E — `frontend/e2e/separation.spec.ts`** (extended, rider B8): a new,
independent-page stage selects "Fold to mono", starts a job through
`Workflow.startSeparationWithRequest`, asserts the request itself carried
`stereo_handling: "mono"`, waits for completion, and (post-review) asserts
the result's stem count matches the mode's before asserting every resulting
stem's `channels` is `1` — the fake separator folds for real (041), so this
is a genuine end-to-end check, not a UI-only one, and the stem-count
assertion is what keeps the per-stem loop from passing vacuously on an
empty result.

### Proved to fail first

`git stash push` on `TimelineLane.tsx`, `StemTimeline.tsx` and
`StemTimeline.css` (keeping the new `layout.spec.ts`), then
`npx playwright test e2e/layout.spec.ts` against the unmodified v0.2.0
layout:

```text
Running 4 tests using 1 worker

  1) … at a 16px root: … faders reach 24px …
     Error: vocals's fader reaches the 24px pointer-target minimum at 16px
     Expected: >= 24
     Received:    11.1875

  2) … at a 17px root: … headers don't clip …
     Error: vocals's header does not clip at 17px
     Expected: <= 62
     Received:    63

  3) … at a 18px root: … headers don't clip …
     Error: vocals's header does not clip at 18px
     Expected: <= 62
     Received:    65

  4) … at a 20px root: … headers don't clip …
     Error: vocals's header does not clip at 20px
     Expected: <= 62
     Received:    69

  4 failed
```

This is an exact match to the assignment's and 054's own figures — ~11 px
fader height at 16 px, and 1/3/7 px of header clipping at 17/18/20 px — so
the new spec is pinning the actual, previously-recorded regression rather
than a synthetic one. `git stash pop` restored the fix; the same command
then passed 4/4 (below).

### Fix 1's own fail-first run (post-review)

The claim under test: `useRootFontSize`'s original mechanism (window
`resize`) never fires for a font-size-only change, so a mid-session root-font
change left the canvas backing store stale while the lane's CSS box grew.
`git stash push` on `TimelineLane.tsx`, `StemTimeline.tsx`, `StemTimeline.css`
and `useRootFontSize.ts`/`.test.ts` (restoring the original `resize`-driven
mechanism; keeping the new mid-session stage in `layout.spec.ts`), then
`npx playwright test e2e/layout.spec.ts -g "mid-session"`:

```text
Running 1 test using 1 worker

  1) … a mid-session root font-size change keeps the canvas backing store in step (review Fix 1)
     Error: the canvas backing store actually changed, rather than staying at the stale baseline
     Expected: > 76
     Received:   76

  1 failed
```

`76` is exactly `LANE_HEIGHT_REM × 16` (the 16 px baseline the stage starts
at) — the canvas backing store never moved off it even though the stage then
set the root font to 20 px and gave the page two animation frames to settle,
which is exactly the staleness the review predicted: `resize` did not fire,
so `useRootFontSize` never re-read the font size, so `laneHeightPx` never
changed. `git stash pop` restored this feature's fix (`useMeasuredHeight`);
the same command then passed (`e2e/layout.spec.ts`'s full run, 5/5, is in the
PR's test output).

## Measurements

All measured in a real Chromium (`npx playwright test`), on the Inspect
screen of a completed four-stem job, one stem's lane header
(`.stem-timeline-lane-header`) and its fader
(`.stem-timeline-lane-fader`).

**Re-verified post-review, unchanged.** Neither review fix touches these
numbers: Fix 1 (`useMeasuredHeight`) only changes how the canvas's *pixel*
backing store is computed, never measured by anything in this table (all of
which reads `clientHeight`/`scrollHeight`/`getBoundingClientRect`, plain CSS
layout); Fix 3's `max()` floor only changes the fader's padding below a
~14.98 px root, below every size this table covers, and evaluates to the
same `0.45rem` at 16–20 px (confirmed by re-running
`frontend/e2e/layout.spec.ts`'s full 16/17/18/20 px matrix after both fixes:
5/5 passing, including the header/fader/alignment assertions this table
mirrors).

### Before (v0.2.0, unmodified)

| Root font | Header `clientHeight` | Header `scrollHeight` | Clipped by | Fader height |
| --- | --- | --- | --- | --- |
| 16 px | 62 px | 62 px | 0 px (0.9 px slack per 054) | **11.19 px** (measured) |
| 17 px | 62 px | 63 px | **1 px** | 11.9 px (`0.7rem`; border-box, no padding — exact) |
| 18 px | 62 px | 65 px | **3 px** | 12.6 px |
| 20 px | 62 px | 69 px | **7 px** | 14.0 px |

The header row's fail-first run (below) stops at the first failing
assertion in each test, so only the 16 px case actually reached the fader
check before the header check failed at 17/18/20 px; those three fader
figures are `0.7rem` evaluated at each root font rather than a second
Chromium measurement — exact under the pre-067 CSS, since `height: 0.7rem;
margin: 0;` with the app's global `box-sizing: border-box` and no padding
or border makes the specified height the whole story.

### After (this feature)

| Root font | Header `clientHeight` | Header `scrollHeight` | Clipped by | Fader height |
| --- | --- | --- | --- | --- |
| 16 px | 74 px | 74 px | 0 px | **25.56 px** |
| 17 px | 79 px | 79 px | 0 px | **27.17 px** |
| 18 px | 84 px | 84 px | 0 px | **28.78 px** |
| 20 px | 93 px | 93 px | 0 px | **32.00 px** |

`clientHeight === scrollHeight` at every size means no overflow at all —
`scrollHeight` cannot exceed `clientHeight` once content fits, so this
column alone does not show *margin*. A separate, unconstrained (`height:
auto`) measurement of the same three rows put the real margin at roughly
2.5–3.6 px across the four sizes: comfortable without being wasteful, and
deliberately not zero (see Notes §2).

## Notes / decisions

1. **`rem`, not a bigger fixed pixel count.** A fixed `LANE_HEIGHT_PX`, however
   generous, only postpones the clipping to a larger root font — 054 already
   demonstrated that with the *existing* rows alone. `LANE_HEIGHT_REM` grows
   the header box at exactly the rate its `rem`-sized rows grow, so the
   slack measured at one root font holds at every root font instead of
   shrinking through zero as it did before.

2. **Two numbers deviate from the assignment's literal design, both
   measured, not guessed.**

   - **`min-height: 24px` on the fader is not included**, though the design
     listed it. With `box-sizing: content-box` (also specified), `min-height`
     constrains the *content* box — the drawn track — not the padded hit box.
     Since the track's specified `height` (`0.7rem`, 11.2–14 px across
     16–20 px roots) is below 24 px at every root font this feature targets,
     `min-height: 24px` does not act as a quiet floor: it wins outright,
     forcing the drawn track itself to 24 px and pushing the *total* layout
     box (with padding still added on top) to **38–42 px** — measured in a
     real Chromium with the literal four-property block, not derived. That
     both fails the "keep the drawn track slim" half of the design's own
     stated intent and blows well past what `LANE_HEIGHT_REM` budgets for.
     Dropping `min-height` and keeping `height: 0.7rem; padding-block:
     0.45rem` alone already clears 24 px at every tested root (25.56–32 px,
     table above) — measured, not assumed — so nothing else was needed.
   - **`LANE_HEIGHT_REM` is `4.75`, not the assignment's `4.5`.** With
     `min-height` removed, `4.5rem` (72 px at a 16 px root) still clipped by
     2–3 px at every tested root — measured directly against the real
     `.stem-timeline-lane-header` markup (label line, Mute/Solo row, the
     corrected fader), not against a hand-derived arithmetic estimate. Two
     levers close the gap: `LANE_HEIGHT_REM` itself, and the header's own
     padding/gap, which 054 already tightened once for exactly this reason.
     This feature uses a bit of both — `LANE_HEIGHT_REM` at `4.75rem` and
     the header's `padding`/`gap` retightened from `0.2rem`/`0.2rem` to
     `0.15rem`/`0.15rem` — landing a few pixels of real margin (measured via
     an unconstrained `height: auto` render, since a constrained
     `scrollHeight`/`clientHeight` comparison reads `0` once nothing clips;
     see the Measurements section) rather than the knife's-edge exact fit
     either lever alone would have produced. The margin matters because this
     repository's CI (`ubuntu-latest`) renders with different system fonts
     than any one contributor's machine; an exact-fit number measured on one
     platform is not guaranteed to hold on another, and the whole point of
     this feature is to stop shipping numbers that were only checked at one
     configuration.

   Both are one-line, clearly-scoped deviations from the assignment's
   literal CSS, made because the acceptance mechanism the assignment itself
   specifies — "acceptance is measurement" — disagreed with the assignment's
   own worked arithmetic once checked against a real browser. The measured
   numbers govern.

3. **The canvas backing store is measured directly, not derived from
   `resize` — `useRootFontSize`'s original mechanism, which a post-review
   probe found does not fire for the case it existed to catch.** Every
   layout box in the timeline that participates in this feature's
   acceptance criteria — the header row, the lane row — is sized with plain
   CSS `rem`, which the browser recomputes for free on a root-font change;
   nothing here has to listen for that. The one exception is
   `<canvas>.width`/`.height`, which are plain integers with no unit and no
   participation in CSS layout at all — something still has to tell
   `TimelineLane` the lane's actual pixel height.

   **This feature originally shipped `useRootFontSize`**, which multiplied
   `LANE_HEIGHT_REM` by the root font size, refreshed on window `resize` — on
   the theory that a root-font change reflows the page and a reflow of the
   page above the timeline changes the window's content box, which fires
   `resize`. **A cross-model review tested that theory in a real Chromium
   with a controlled probe and found it false**: changing
   `document.documentElement.style.fontSize` does not fire `resize` at all —
   `innerWidth`/`innerHeight` are unaffected by a font-size-only reflow, and
   nothing else on this page's layout happens to trigger it either. The
   consequence was silent and specific to the exact users this feature
   serves: a mid-session font-size change grew the lane's `rem`-sized CSS box
   (for free, since CSS recomputes `rem` layout on its own) while the canvas
   backing store — still multiplying `LANE_HEIGHT_REM` by a `resize`-driven
   root font size that had gone stale — did not, stretching the waveform
   vertically by roughly the ratio of the font change (measured: the backing
   store stuck at `76px`, the 16 px baseline, after the root font moved to
   20 px — see "Fix 1's own fail-first run" above). This is why the original
   E2E spec's own note ("holds regardless of whether `resize` fires") was
   true for every assertion *in that spec* (all CSS layout boxes, which do
   not need `resize`) but did not generalise to the canvas — the one thing
   in this feature that did.

   **The fix (`useMeasuredHeight`, replacing `useRootFontSize`) measures the
   lane's own rendered box with `ResizeObserver`** instead of re-deriving its
   height from a proxy signal — this codebase's own idiom, already used for
   the tracks strip's width (`trackRef` in `useTimelineGeometry.ts`). A
   `ResizeObserver` on the box itself cannot miss a font-size-driven change
   (probed: `76px → 95px` across a change that fired zero `resize` events)
   because it is not inferring the change from something else; it is
   watching the box. `frontend/e2e/layout.spec.ts`'s last stage now checks
   the canvas backing store directly, rather than relying on the CSS-only
   assertions above to imply it is correct.

4. **The draw effect still never repaints on a playhead frame.**
   `TimelineLane` never received `positionSeconds` as a prop before this
   feature and still does not; `laneHeightPx` is exactly the kind of input
   the effect already depended on (peaks, viewport, dpr, audibility,
   duration) — a value that changes on a real state transition, not 60
   times a second. The new `StemTimeline.test.tsx` case makes this an
   explicit regression test rather than an implicit one.

5. **C10, the scroll-thumb/playhead z-order, in one line.** Neither
   `.stem-timeline-scrollbar` nor `.stem-timeline-playhead` set `z-index`
   before this feature, so the two stacked in DOM order — the playhead,
   which is later in the markup (see `StemTimeline.tsx`'s region map),
   painted over the scroll thumb's track on every frame the hairline
   crossed it. `z-index: 1` on `.stem-timeline-scroll-thumb` is sufficient:
   both elements are positioned, and neither establishes its own stacking
   context other than the playhead's own `will-change: transform` (which
   only affects how *it* paints as a unit, not the comparison against the
   thumb).

6. **B8, "Fold to mono" through job creation, deliberately not folded into
   the shared serial workflow in `separation.spec.ts`.** That block reuses
   one job across every stage; changing its stereo handling would change
   what later stages in the block are asserting about (e.g. the fold halves
   a stem's channel count, which the export/download stage does not
   currently care about, but the point is not to make later stages start
   caring). The new stage opens its own page and uses the tiny (2 s, one
   chunk) fixture, so it costs little and stays independent.

7. **(Post-review) The fader's `padding-block` floors at a 24 px box below
   the 16–20 px root-font range this feature targets.** The flat `0.45rem`
   this feature measured sufficient at 16–20 px stops being enough at a
   14 px root — measured at `22.39px`, under WCAG's 24 px. `padding-block:
   max(0.45rem, calc((24px - 0.7rem) / 2))` keeps the larger of the flat
   value (which still wins at 16–20 px, so those measurements are unchanged)
   and the exact per-side padding that puts the *total* content-box height
   at 24 px (which wins below that range). See
   `.stem-timeline-lane-fader` in `StemTimeline.css` for the full arithmetic.

8. **(Post-review) B8's stem-count pin.** The "Fold to mono" stage's
   `for (const stem of completed.result?.stems ?? [])` loop would pass
   vacuously if the result ever came back with an empty (or missing) stem
   list — `?? []` exists so a malformed response does not throw mid-test, but
   it also means the loop's assertions silently check nothing in that case.
   `expect(completed.result?.stems ?? []).toHaveLength(mode.stems.length)`
   before the loop closes that gap, reading the expected count from the
   mode's own stems the way the rest of this repository's E2E tier does.

## Known limitations

- **This spec's four root sizes (16/17/18/20 px) are the ones 054 measured
  clipping at, not an exhaustive sweep.** A root font well past 20 px (a
  200%+ browser zoom-equivalent setting) is not verified here; `rem`-relative
  sizing should hold by construction, but nothing in this feature's test
  suite proves it above 20 px.
- **Font-rendering differences across platforms are mitigated with a
  measured margin, not eliminated.** The 2.5–3.6 px of slack (Notes §2)
  comfortably absorbs the kind of sub-pixel variation seen between this
  review's own measurement runs, but was not independently re-measured on
  `ubuntu-latest` (this repository's actual CI runner) before merge — only
  reasoned about from font-stack differences. If CI's `e2e` job (not a
  required check, but not nothing) reports a clip at any of the four sizes,
  the fix is another small, measured bump to `LANE_HEIGHT_REM` or the
  header's padding/gap, not a rethink of the approach.
- **(Noted in post-review, not fixed) The canvas backing store's height is a
  fractional `rem × devicePixelRatio` product rounded to an integer pixel
  count** (`Math.round(laneHeightPx * devicePixelRatio)` in
  `TimelineLane.tsx`), so at some root font / DPR combinations the backing
  store is up to half a device pixel taller or shorter than the CSS box it
  fills — an imperceptible sub-pixel stretch or letterbox, not a layout
  defect. This is the same pre-existing pattern the width side of the same
  line already uses (`Math.round(cssWidth * devicePixelRatio)`, unchanged by
  this feature or its review), so it is consistent rather than new; nothing
  in this feature's scope calls for resolving it.
