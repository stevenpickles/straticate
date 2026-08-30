# [067] Lane height + fader accessibility

Branch: `067-lane-height-a11y`
Status: PR OPEN
Dependencies: 050, 054
PR: —

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
- `frontend/src/components/useRootFontSize.ts` (new) — reads the root
  element's computed font size, refreshed on window `resize`.
- `frontend/src/components/StemTimeline.tsx` — computes `laneHeightPx` from
  `LANE_HEIGHT_REM` and `useRootFontSize()`, passes it to `TimelineLane`;
  the lane header row and lane row's inline heights become `rem`.
- `frontend/src/components/StemTimeline.css` — the fader's hit box grows
  independently of its drawn track (C-item, WCAG 2.2 SC 2.5.8); the lane
  header's padding/gap retightened a second time (054 did it once) to fund
  the fader without raising `LANE_HEIGHT_REM` any further than measurement
  showed necessary; C10 — `.stem-timeline-scroll-thumb` lifted above
  `.stem-timeline-playhead` with one `z-index` line.
- `frontend/e2e/layout.spec.ts` (new) — the multi-root-font measurement
  spec that is this feature's actual acceptance mechanism.
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts` — B10) a
  `chooseStereoHandling` helper and a `startSeparationWithRequest` variant
  that also returns the request body; B8) one new stage exercising "Fold to
  mono" through real job creation, closing the E2E-coverage gap 041 recorded.
- Unit tests: `useRootFontSize.test.ts` (new), extended
  `StemTimeline.test.tsx`.

## Out of scope

- `engine.ts`, `StemPlayer.tsx/.css/.test.tsx` — feature 064's, run in
  parallel this wave.
- Auto-follow logic (068) — the follow effect in `StemTimeline.tsx` was not
  touched.
- Session hoist (065), backend, any schema.

## Expected modules/files

- `frontend/src/components/TimelineLane.tsx`
- `frontend/src/components/useRootFontSize.ts`, `useRootFontSize.test.ts`
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
- [x] **A click at a fader's centre lands on the fader**
      (`document.elementFromPoint`), not on an overflowing neighbour — the
      050-era guard, now checked as a real measurement rather than assumed
      from "it still fits".
- [x] **The header row and lane row stay aligned to the pixel** for every
      stem, at every root size, with stem counts read from the mode rather
      than hardcoded.
- [x] **The canvas backing store still repaints only on state, never on a
      playhead frame** — `laneHeightPx` joined the draw effect's dependency
      list without adding a frame-driven dependency; playhead motion still
      reaches only a transformed `div`.
- [x] **C10**: the scroll thumb no longer sits under the playhead hairline.
- [x] **B8**: the "Fold to mono" stereo-handling radio is exercised through
      real job creation in the E2E tier.
- [x] Five frontend gates green (`npm ci` first); full E2E tier green,
      including the new spec.

## Required tests

**Unit — `useRootFontSize.test.ts`** (new): falls back to 16 with no
measurable root font (the jsdom case, and every other test in this suite);
reads a real stubbed value when one is measurable; refreshes on `resize`
and only on `resize`; removes its listener on unmount.

**Unit — `StemTimeline.test.tsx`** (extended): a new case in the
`StemTimeline lanes` describe block extends the existing "repaints only the
lane whose audibility changed" invariant — a rerender that changes only
`positionSeconds` (what the player sends 60 times a second while playing)
produces zero `fillRect` calls, and a simulated root-font change (a stubbed
`getComputedStyle` plus a dispatched `resize`) produces a fresh repaint of
every lane. The existing 43 cases stay green unmodified — none of them
depended on the literal `64`.

**E2E — `frontend/e2e/layout.spec.ts`** (new): for a completed four-stem
job on the Inspect screen, at root font sizes 16/17/18/20 px (set via
`document.documentElement.style.fontSize`, the same mechanism a real
browser setting drives), asserts all four acceptance criteria above. Waits
are two `requestAnimationFrame`s after the font-size mutation (the same
idiom `app.ts`'s `renderedFrames` uses elsewhere in this tier) — a real
condition, never a sleep.

**E2E — `frontend/e2e/separation.spec.ts`** (extended, rider B8): a new,
independent-page stage selects "Fold to mono", starts a job through
`Workflow.startSeparationWithRequest`, asserts the request itself carried
`stereo_handling: "mono"`, waits for completion, and asserts every
resulting stem's `channels` is `1` — the fake separator folds for real
(041), so this is a genuine end-to-end check, not a UI-only one.

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

## Measurements

All measured in a real Chromium (`npx playwright test`), on the Inspect
screen of a completed four-stem job, one stem's lane header
(`.stem-timeline-lane-header`) and its fader
(`.stem-timeline-lane-fader`).

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

3. **`useRootFontSize` exists only for the canvas backing store.** Every
   layout box in the timeline that participates in this feature's
   acceptance criteria — the header row, the lane row — is sized with plain
   CSS `rem`, which the browser recomputes for free on a root-font change;
   nothing here had to listen for that. The one exception is
   `<canvas>.width`/`.height`, which are plain integers with no unit and no
   participation in CSS layout at all. `useRootFontSize` (window `resize`,
   16 px jsdom/no-layout-engine fallback) exists to give `TimelineLane`
   something to multiply `LANE_HEIGHT_REM` by. Because of this, the E2E
   spec's assertions (all about `clientHeight`/`scrollHeight`/
   `getBoundingClientRect`, i.e. layout boxes) hold regardless of whether a
   `resize` event happens to fire after `document.documentElement.style.
   fontSize` is set by hand — CSS updates those on its own. A real browser's
   own font-size setting does fire `resize` (a root-font change reflows the
   whole page), which is what keeps the canvas's raster resolution in step
   with everything else outside of this spec's own assertions.

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
