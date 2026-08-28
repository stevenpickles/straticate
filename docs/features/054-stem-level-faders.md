# [054] Per-stem level faders

Branch: `054-stem-level-faders`
Status: PR OPEN
Dependencies: 050
PR: #76

## Objective

Each stem's lane header in the timeline (feature 050) gains a volume fader
that drives the engine's existing `setLevel(name, 0..1)`, so the level a user
hears per stem is adjustable from the UI the same way mute and solo already
are.

## Scope

- `StemTimeline.tsx` — the `.stem-timeline-lane-header` region: one
  `<input type="range">` fader per stem, wired through a new `onSetLevel`
  prop, alongside the existing Mute/Solo buttons.
- `StemTimeline.css` — fader styling, added at the end under a
  `/* 054: level faders */` comment; the header's own padding/gap were
  tightened slightly to make room for the third row without touching
  `LANE_HEIGHT_PX` (050/051's, not this feature's to change).
- `StemPlayer.tsx` — a `setLevel` callback that forwards `(name, value)` to
  `engine.setLevel`, mirroring `toggleMute`/`toggleSolo`; `level` added to the
  `timelineStems` mapping, defaulting to `1` before a snapshot exists.
- Tests — `StemTimeline.test.tsx`, `StemPlayer.test.tsx` (`FakeEngine.setLevel`
  now records calls instead of being a no-op), `e2e/separation.spec.ts`.

## Out of scope

- Zoom/pan (051, parallel this wave), scrub preview (052), loop regions (053).
- Any engine change — including a zipper-noise-avoiding level ramp; see Notes.
- Backend, `index.css`, `TimelineLane.tsx`, `useTimelineGeometry.ts`,
  `useWaveformPeaks.ts`, `TimelineRuler.tsx`.

## Expected modules/files

- `frontend/src/components/StemTimeline.tsx` — lane-header region only
- `frontend/src/components/StemTimeline.css` — header rules + new fader rules
- `frontend/src/components/StemPlayer.tsx` — `setLevel` wiring
- `frontend/src/components/StemTimeline.test.tsx`,
  `frontend/src/components/StemPlayer.test.tsx` — extended
- `frontend/e2e/separation.spec.ts` — extended
- `docs/features/054-stem-level-faders.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **One fader per stem, in every lane header, for any stem count.** Rows
      come from `stems`, exactly like the rest of the header — a two-stem and
      a four-stem result render through the same code.
- [x] **`onChange` calls `engine.setLevel(name, value)` continuously.** Unlike
      the seek gesture (one commit per pointer gesture, because a seek tears
      down and rebuilds every `AudioBufferSourceNode`), a level change is a
      plain `AudioParam.value` write — there is nothing to batch against, so
      every `change` event reaches the engine. Proved with two fired events
      producing two calls, not one.
- [x] **The fader is enabled and shows the true level while muted or
      soloed-out.** `level` is independent of `audible`; only
      `status !== 'loaded'` disables it, matching Mute/Solo.
- [x] **Fits the existing 64 px lane header without raising
      `LANE_HEIGHT_PX`.** A thin, deliberately compact range input; see Notes
      for the pixel accounting and the file-ownership reason it had to fit
      rather than grow.
- [x] **Accessible name per stem** (`"<stem name> level"`), with
      `aria-valuetext` as a percentage.
- [x] **Five frontend gates green**, e2e tier run and green (24/24, including
      the extended `separation.spec.ts`).

## Required tests

**Unit — `StemTimeline.test.tsx`** (component alone, `onSetLevel` prop):

- one fader per stem for two- and four-stem results, found by accessible name;
- two `change` events on one fader forward two calls with the stem's name and
  the numeric value — continuous, not batched;
- the fader's value reflects the snapshot's `level`;
- disabled while `status !== 'loaded'`;
- stays enabled and keeps showing the true level while `muted` (`audible:
  false`) and while soloed out.

**Unit — `StemPlayer.test.tsx`** (through the whole player):

- `FakeEngine.setLevel` extended from a no-op into
  `readonly levelSets: { name: string; value: number }[]`, recording calls;
- two fader `change` events reach `engine.setLevel` as two calls, not one;
- one fader per stem for two- and four-stem results;
- the fader reflects `stemState(...).level` from the engine snapshot;
- disabled until the stem has loaded;
- stays enabled and correct while muted.

**Semantics — over the real engine** (`createStemAudioEngine` +
`FakeAudioContext`, `StemPlayer.test.tsx`'s "over the real engine" describe):
firing a fader `change` event sets the corresponding gain node's `.value`
directly (`gains()` helper), and a subsequent Mute still silences it — `level`
and `audible` compose exactly as `engine.ts`'s `applyGains` documents.

**E2E — `separation.spec.ts`.** In "lists the stems with working playback
controls": asserts a fader (role `slider`, name `"<stem> level"`) is visible
for every stem the mode reports, then `fill('0.5')` on the first stem's fader
and asserts the resulting value — driving it through Playwright's real input
path rather than a synthetic DOM event.

### Proved to fail first

`StemTimeline.tsx` was stashed back to its pre-fader state (via
`git stash push -- frontend/src/components/StemTimeline.tsx`) and the new
`StemPlayer.test.tsx` test run in isolation:

```text
 FAIL  src/components/StemPlayer.test.tsx > StemPlayer level faders (feature 054)
   > calls engine.setLevel with the stem name and value, continuously — not
     once per gesture

TestingLibraryElementError: Unable to find an accessible element with the
role "slider" and name "vocals level"
```

The stash was then restored (`git stash pop`) and the full suite re-run green
(912/912).

## Notes / decisions

1. **Continuous `onChange`, not commit-on-release, and that is correct here.**
   The seek control commits once per pointer gesture because seeking is
   expensive: an `AudioBufferSourceNode` is single-use, so a seek stops and
   recreates every stem's source node with a fresh scheduling lookahead — 32
   source nodes for one four-stem drag, per 050's own measurement. A level
   change is nothing like that: it is one write to an existing `GainNode`'s
   `AudioParam`, the same node that already exists and stays connected. There
   is no teardown, no rebuild, and therefore nothing worth batching. Firing
   `engine.setLevel` on every `change` event is the correct design, not a
   shortcut — this is stated directly in the code comments on both
   `StemTimeline`'s prop doc and `StemPlayer`'s `setLevel` callback.

2. **Level is independent of mute/solo, by construction.** `engine.ts`'s
   `applyGains` already computes `entry.audible ? entry.level : 0` — the
   fader's value always reflects `level`, and audibility is a separate signal
   the row's opacity and the Mute/Solo buttons carry. Muting a stem does not
   move its fader, and un-muting it restores exactly the level it was left
   at. This was true of the engine before this feature; the fader just gives
   it a control surface.

3. **The fader had to fit the existing 64 px header, not grow it.** 050's
   `LANE_HEIGHT_PX` lives in `TimelineLane.tsx`, which the parallel 051 branch
   owns this wave — off limits per the assignment's file-ownership split.
   050's own notes (§11) already flagged this constraint for whichever
   feature added a fader. The fix was two changes, both inside
   `StemTimeline.css` (owned by this feature):
   - `.stem-timeline-lane-header`'s padding went from `0.25rem 0.4rem` to
     `0.2rem 0.4rem` and its `gap` from `0.3rem` to `0.2rem`, freeing a few
     pixels without touching the label or the Mute/Solo rows 050 already
     tuned (and which the 050 E2E run caught overflowing once before).
   - The fader itself is a thin `<input type="range">` (`height: 0.7rem`,
     `margin: 0`), styled with `accent-color` rather than a full custom
     track/thumb rebuild — every engine tested (Chromium, which the E2E tier
     runs against) scales the thumb down with an explicit `height`, so a
     short box reads as a slim fader instead of a clipped full-size one.
   Accounting at a 16 px root, measured in a real Chromium (review pass, not
   estimated): the header box is `border-box`, so the budget is 64 px −
   2 px border − ~6.4 px padding − ~6.4 px of two gaps ≈ 49.2 px; the three
   rows measure 15.44 + 20.86 + 11.19 = 47.49 px, leaving **~0.9 px of
   slack** — it fits at default settings, but only just. At browser root
   fonts of 17/18/20 px the rem-sized rows outgrow the fixed 64 px box by
   1/3/7 px; `overflow: hidden` clips the excess symmetrically (fader bottom
   and label top) and, measured via `elementFromPoint`, the fader stays
   clickable — the 050-style overlay-swallows-clicks defect cannot recur.
   The real fix (a taller or rem-relative `LANE_HEIGHT_PX`) lives in
   `TimelineLane.tsx`, which feature 051 owns this wave; recorded under
   Known Limitations for 051/055 to pick up. Verified by running the full
   E2E tier in a real Chromium (`npm run e2e`, 24/24 green).

4. **The zipper-noise ramp is deliberately not implemented.** A real fader
   moving quickly can produce an audible "zipper" artifact from stepped
   `AudioParam.value` writes; the idiomatic fix is
   `gain.linearRampToValueAtTime(...)` with a short time constant. That is an
   engine change (`engine.ts`, specifically `applyGains`/`setLevel`), and this
   feature's assignment scopes engine changes out entirely — `setLevel`
   already exists, tested, and is not this feature's to touch. The benefit is
   also small at the step granularity a `step={0.01}` range input produces
   from a mouse or keyboard (as opposed to a continuous physical fader), so
   this is recorded as a known limitation rather than opened as a new
   numbered feature.

5. **`FakeEngine.setLevel` in `StemPlayer.test.tsx` changed from a no-op to a
   recorder.** It previously carried the comment "Not driven from this UI" —
   true before this feature and false after it. The new
   `readonly levelSets: { name: string; value: number }[]` follows the same
   pattern as `muteToggles`/`soloToggles` already on the same class.

6. **`level` defaults to `1` in `StemPlayer`'s `timelineStems` mapping**,
   matching the engine's own default (`StemEntry.level = 1` in `engine.ts`)
   for the window before a snapshot exists — the same reasoning `audible`'s
   `?? true` default already uses one line above it.

## Known limitations

- **The lane header runs out of vertical headroom above the default root
  font.** Measured in the review pass: ~0.9 px of slack at a 16 px root; at
  17/18/20 px browser font settings the rem-sized rows outgrow the fixed
  64 px `LANE_HEIGHT_PX` by 1/3/7 px and `overflow: hidden` clips the fader's
  bottom edge and the label's top. The fader remains clickable
  (`elementFromPoint`-verified). The fix — a taller or rem-relative lane
  height — belongs to `TimelineLane.tsx`, owned by feature 051 this wave;
  hand to 051 or 055 if 051 does not absorb it.
- **The fader's pointer target is ~11 px tall**, under WCAG 2.2 SC 2.5.8's
  24 px minimum (the user-agent-control exception is arguable since the
  height is author-set). Keyboard operation is unaffected. A taller lane
  height would allow a taller target; same owner as above.
- **The zipper-noise gain ramp is deliberately not implemented** (engine
  change, small benefit at `step={0.01}` granularity) — see Notes §4.
