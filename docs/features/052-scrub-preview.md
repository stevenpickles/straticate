# [052] Audible scrub preview

Branch: `052-scrub-preview`
Status: PR OPEN
Dependencies: 050, 053
PR: —

## Objective

Dragging the playhead is **audible**, Audacity-style: short preview grains of
every stem sound at the cursor while the drag is in flight, without the main
transport graph ever being rebuilt. The one-real-transport-move-per-gesture
contract feature 050 established survives unchanged — now with sound during
the drag.

## Scope

- **`audio/engine.ts`** — `beginScrubPreview()` / `scrubPreview(seconds)` /
  `endScrubPreview(commitSeconds?)`, `scrubbing` in the snapshot, the three
  `scrub*Seconds` options, `AudioEngineParam` widened with
  `setValueAtTime` / `linearRampToValueAtTime` / `cancelScheduledValues`, and
  the defensive session close in `play`, `pause`, `seek`, `load` and
  `dispose`.
- **`test/fakeAudioContext.ts`** — `FakeAudioParam` extended with a recorded
  `events` array (`{type, value?, time}` in call order).
- **`components/StemTimeline.tsx`** — one new prop, `onScrubStart`, fired by
  `beginGesture` before the first position. Loop-region gestures do not fire
  it.
- **`components/StemPlayer.tsx`** — `beginScrubPreview` on scrub start,
  `scrubPreview` on every move (alongside the displayed position), and
  `endScrubPreview(seconds)` **instead of** `seek(seconds)` as a pointer
  gesture's commit.
- **E2E** — `Workflow.dragSeek()` and a scrub stage in `separation.spec.ts`.

## Out of scope

- Rate-varying (tape-style) scrubbing, pitch-preserving playback, a scrub
  speed control, or scrubbing a single stem in isolation.
- Zoom, loop regions and faders beyond the gesture wiring above; the backend;
  `index.css`.

## Expected modules/files

- `frontend/src/audio/engine.ts`, `engine.test.ts`
- `frontend/src/test/fakeAudioContext.ts`
- `frontend/src/components/StemTimeline.tsx`, `.test.tsx`
- `frontend/src/components/StemPlayer.tsx`, `.test.tsx`
- `frontend/e2e/app.ts`, `frontend/e2e/separation.spec.ts`
- `docs/features/052-scrub-preview.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **A drag is audible.** Every accepted `scrubPreview(seconds)` schedules
      one grain per loaded stem — source → envelope → the stem's own gain node
      — at one shared `when` read from the clock once, with the scrubbed
      offset and `stop(when + grain)`.
- [x] **The transport graph is never rebuilt by a preview.** Grains are
      throwaway nodes. The session stops the transport once at `begin` and
      starts exactly one new generation at `end`, so a whole drag still costs
      one real transport move.
- [x] **`endScrubPreview(commit)` *is* the gesture's seek.** A pointer drag
      calls it instead of `seek`, never both; the engine and component suites
      both assert that no `seek` accompanies a gesture.
- [x] **The resume decision is captured at `begin`.** `scrubWasPlaying` is
      read while the transport is still running; by `end` it is always paused,
      which is why the API has three calls rather than two.
- [x] **The throttle is the audio clock, never a timer.** A call arriving
      before `previewBusyUntil` is dropped, not queued. **Proved to fail
      first — output below.**
- [x] **Mute, solo and level apply to grains with nothing recomputed**, by
      routing them into the existing per-stem gain node.
- [x] **A grain never sets `loop`**, even with a region set (feature 053's
      note 10), and grains ignore the region entirely.
- [x] **Every edge closes the session.** `play`, `pause` and `seek` close an
      open one; `load` and `dispose` stop and disconnect the grains; all three
      public calls are no-ops when disposed, and `beginScrubPreview` is a
      no-op unless the engine is `ready`.
- [x] **A keyboard commit mid-drag ends the session and moves once.**
- [x] **Five frontend gates green**, and the full E2E tier run locally: 27
      passed, including the new scrub stage.

## Required tests

**Unit — `audio/engine.test.ts` (21 new).** A grain per stem at one shared
`when`, the scrubbed offset and `stop(when + grain)`; the four-point envelope
recorded on `FakeAudioParam.events`; source → envelope → stem-gain routing,
and a muted stem's grain landing in the node whose gain is already zero;
three previews inside one retrigger window producing one grain set and the
clock advancing past it admitting the next; `begin` silencing a playing
transport and holding its position; the release resuming a fresh generation at
the commit, with loop flags reapplied when a region is set; grains never
looping; the release fade (`cancel` + ramp to 0 + `stop(now + fade)`) and
disconnection; a paused session staying paused; an uncommitted end leaving the
playhead alone; clamping to the mix; no-ops with no session and after
disposal; `begin` idempotent while a session is open; `play` / `pause` / `seek`
closing an open session, with the mid-drag `seek` folding into **one**
generation; `load` and `dispose` stopping every grain; `scrubbing` published on
change and *not* published per preview; and the three options honoured.

**Unit — `components/StemTimeline.test.tsx` (4 new).** One `onScrubStart` per
drag, fired before the first `onScrub`; further moves not reopening it; and a
ruler drag, a shifted lane drag and an edge handle each opening none.

**Unit — `components/StemPlayer.test.tsx` (5 new, 7 updated).** A drag opening
one session, previewing the press and every move in order, and committing
exactly one `endScrubPreview` with **no** `seek`; a cancelled gesture ending
with no commit; a keyboard seek with no drag under way staying a plain `seek`;
a loop-region gesture previewing nothing; and — over the **real** engine with
a fake `AudioContext` — a press silencing all four running sources and
scheduling four grains at the pointer's position with `loop === false`, then
the release resuming four sources at the released offset. The seven updated
cases are the pre-existing gesture tests, which now assert on `FakeEngine`'s
`moves` (a real transport move, whichever call made it) rather than on `seeks`.

**E2E — `separation.spec.ts`.** A stage between the loop stage and export:
with the whole 60 s file fitted, press Play, drag the seek surface from two
tenths to six tenths of the strip through four intermediate moves, and assert
the transport is still playing afterwards, that the playhead landed between
0:35 and 0:45 once paused, and that nothing reached the console. Run locally
against the real backend and the fake separator: **27 passed**.

### Proved to fail first

The clock comparison was removed from `scrubPreview` (`if (false && …)`) and
the regression run against the result:

```text
 FAIL  src/audio/engine.test.ts > StemAudioEngine scrub preview > drops
 previews inside the retrigger window, and takes the next one
AssertionError: expected [ FakeSourceNode{ …(10) }, …(5) ] to have a length of
2 but got 6

 ❯ src/audio/engine.test.ts:1241:29
    1241|     expect(context.sources).toHaveLength(2)
       |                             ^
```

Three grains per stem for three pointer moves the user made inside ninety
milliseconds is what an unthrottled preview sounds like: a real drag delivers
pointer events faster than the grains can be heard, so every stem is layered
over itself several times and the mix under the cursor is mud rather than
audio.

## Notes / decisions

1. **Three calls, not two, because the resume decision has to be captured
   early.** By the time a drag is released the transport is always paused —
   the session paused it — so nothing at `end` can tell whether the user was
   listening when they grabbed the playhead. `beginScrubPreview` records
   `scrubWasPlaying` while it is still true. The commit parameter on
   `endScrubPreview` then folds the UI's two actions (move the playhead,
   restart the audio) into one engine transition with no ordering hazard
   between them.

2. **`endScrubPreview(commit)` is the gesture's seek, and the player chooses
   which commit that is.** The timeline still has exactly one commit path
   (`onSeek`, fired once per gesture and once per keypress) — 050's design,
   untouched. What is new is a ref in `StemPlayer` that remembers whether a
   preview session is open: a pointer gesture commits through
   `endScrubPreview(seconds)`, a keypress with no drag under way through
   `seek(seconds)`. Adding a second commit prop to the timeline would have
   duplicated the one mechanism 050 built deliberately; making
   `endScrubPreview` fall back to a seek when no session is open would have
   made a dead call silently move the transport.

3. **The throttle is a clock comparison and never a timer.** `scrubPreview`
   drops a call when `context.currentTime < previewBusyUntil` and does nothing
   else — no `setTimeout`, no queue, no trailing call. A dropped position is
   *superseded* by the next pointer move, which is the position the user is
   actually pointing at; a queue would replay stale ones behind the pointer.
   It also means the throttle is testable by assignment (`context.currentTime
   = …`) rather than by waiting, which is the rule the whole suite follows.

4. **Grains inherit mute, solo and level by construction.** Each grain is a
   source into its own envelope gain into **the stem's existing gain node** —
   the node `applyGains()` already writes mute/solo/level into. Nothing in the
   preview path resolves audibility a second time, so the two cannot drift
   apart; a muted stem is silent under the pointer because its grain runs into
   a gain of zero, which the engine suite asserts through the routing rather
   than with a duplicated rule.

5. **`AudioEngineParam` was widened, but `applyGains` was not changed.** The
   structural type gained the three scheduling methods a real `AudioParam`
   already has (their `AudioParam` return type is assignable to `void`). They
   are used **only** by grain envelopes; mute/solo/level still write
   `gain.value` directly, so every pre-existing gain assertion in the suite
   stands unchanged.

6. **The envelope is four points, not two.** `setValueAtTime(0, when)` →
   ramp to 1 by `when + fade` → hold to `when + grain - fade` → ramp to 0 by
   `when + grain`. A grain gated on and off with a step is a click at both
   ends, eight milliseconds of ramp is inaudible as a fade, and the fade is
   clamped to half the grain so a misconfigured pair cannot invert it.

7. **A grain never loops — see 053's note 10.** The region belongs to the
   transport; a looping grain would repeat a fragment under the pointer for as
   long as the user held it. Grains therefore ignore any region entirely: they
   audition an absolute position, which is exactly what a user dragging the
   playhead is asking to hear, even when that position is outside the loop.

8. **Cross-feature edges.**
   - *Scrub during a loop region*: grains ignore the region; the position read
     at `begin` is `currentTime()`, which under a region is the **wrapped**
     value — where the audio actually is.
   - *Committing outside the region while looping*: the region's own trap
     semantics apply to the generation the commit starts (053, note 2). A
     commit before `loopEnd` runs into the region and wraps; a commit at or
     past `loopEnd` plays straight through to the end of the mix with the
     region still set. The engine still never seeks to enter a region.
   - *A mid-drag keyboard seek*: `seek` closes the session **without**
     resuming and then builds its own generation, so the move costs one
     rebuild rather than two.
   - *`load` and `dispose`*: both go through `teardownGraph`, which stops and
     disconnects every grain and clears the session flags — a new job never
     inherits a drag from the last one.

9. **The press is audible, and a plain click is not.** Folding the preview
   into the existing `onScrub` flow means the press previews too, which is
   what a scrub should do. A motionless *click* still makes no sound, and not
   by special case: the grain is scheduled at `currentTime + lookahead` and the
   release's `stop(now + fade)` lands before that, so it never starts.

10. **The release fade is scheduled, but `disconnect()` is immediate.** As
    designed, `endScrubPreview` cancels the envelope, ramps it to zero at
    `now + fade`, stops the source at the same instant and disconnects both.
    Disconnection takes effect immediately in the graph, so the ramp is
    belt-and-braces rather than an audible tail — which is acceptable because a
    grain is at most ninety milliseconds long and is being replaced by the
    transport resuming at the commit. Deferring the disconnect to `onended`
    would keep the ramp audible at the cost of nodes outliving the session;
    that trade was not taken.

11. **`scrubbing` is snapshot state; a preview is not.** Opening and closing a
    session notifies subscribers (the UI may want to reflect it); individual
    grains do not, because a snapshot per pointer move would re-render the
    whole player dozens of times a second for a value nothing renders.

12. **Noticed, not touched.** 050's known limitation still stands: the
    playhead, the loop region and now a drag do not survive a phase change —
    leaving `inspect` and coming back rebuilds the engine at 0:00. Feature
    053's note 13 (auto-follow flipping the window once per loop pass at high
    zoom) is also unaddressed and unrelated to this branch. The two jsdom
    `getContext` warnings `StemPlayer.test.tsx` prints predate this work.
