# [068] Auto-follow suppressed inside a loop region

Branch: `068-auto-follow-loop`
Status: PR OPEN
Dependencies: 051, 053, 067
PR: #97

## Objective

While a loop region is set and the visible window is zoomed narrower than
that region, playback no longer page-flips the timeline forward approaching
`loopEnd` and back at the wrap. The auto-follow effect stays in force
everywhere else.

## Scope

- **`components/StemTimeline.tsx`** — the follow effect's guard condition
  only. While `loopRegion !== null` and the playhead is inside it
  (`positionSeconds >= loopRegion.start && positionSeconds < loopRegion.end`),
  the effect returns without calling `scrollTo`, leaving the window exactly
  where the user left it.

## Out of scope

- Zoom-to-region on loop start (rejected — see Notes).
- Anything else in the follow effect, the loop-region gesture, the band
  overlay, or the engine's loop semantics (053's, unchanged).
- The parallel, unmerged `windowStore` write-through around this same hook
  (065) — not preempted; this change is the guard condition only, the
  smallest diff that closes 053 note 13.

## Expected modules/files

- `frontend/src/components/StemTimeline.tsx`
- `frontend/src/components/StemTimeline.test.tsx`
- `docs/features/068-auto-follow-loop.md`, `ROADMAP.md`

## Acceptance criteria

- [x] **No page-flip while looping inside a narrower window.** A region set,
      a window zoomed narrower than it, and a playhead that moves from run-up
      to near `loopEnd` and back to `loopStart` leaves `data-scroll-seconds`
      unchanged throughout. **Proved to fail first — output below.**
- [x] **Existing follow behaviour is unchanged with no region set.** The same
      playhead move, same zoom, no loop region: the window still flips
      forward, pinning that 051 behaviour wasn't lost.
- [x] **Suppression is narrowed to inside the region, not disabled
      wholesale.** A playhead trapped past `loopEnd` (053 note 2 — a region
      is a trap, not a fence) still leaves the region's bounds and the window
      still flips to follow it.
- [x] **Five frontend gates green.**

## Required tests

**Unit — `components/StemTimeline.test.tsx` (3 new).** Region set, window
narrower than the region, playhead moving run-up → near `loopEnd` → wrapped
back to `loopStart`: `data-scroll-seconds` never changes. The identical move
with no region set: the window still flips (pins 051's existing behaviour).
Region set, playhead moved *past* `loopEnd` and out of the region: the window
still flips, clamped to the end of the file — proving the guard is a
narrowing, not a blanket suppression.

### Proved to fail first

The guard was reverted (`git stash push -- StemTimeline.tsx`, the fix alone,
keeping the new tests) and the suite run against the unmodified follow
effect:

```text
 FAIL  src/components/StemTimeline.test.tsx > StemTimeline auto-follow inside a loop region > does not page-flip while the playhead loops inside a region narrower than the window
AssertionError: expected 32.333 to be +0 // Object.is equality

- Expected
+ Received

- 0
+ 32.333

 ❯ src/components/StemTimeline.test.tsx:881:29
    879|     // rule): 35 s is inside the region but outside this window.
    880|     view.show({ positionSeconds: 35 })
    881|     expect(scrollSeconds()).toBe(0)
       |                             ^
```

The window flips forward to 32.333 s the moment the playhead reaches 35 s
inside the region — exactly the busy behaviour 053 note 13 described. The
other two new tests passed unmodified, as intended: they pin behaviour this
feature does not change.

## Notes / decisions

1. **Conditional suppression, narrowed — not zoom-to-region.** 053 note 13
   left two options on the table: suppress auto-follow while a region is set,
   or zoom-to-region on loop start. The M5 plan settled on the first, made
   narrower still: suppression applies only while the playhead is *inside*
   the region (`loopRegion !== null && position >= loopRegion.start &&
   position < loopRegion.end`), not for the whole time a region merely
   exists. Zoom-to-region was rejected because it would discard the zoom the
   user just chose — 051's whole philosophy is that a manual pan or zoom is
   never fought by auto-follow, and rewriting the viewport the moment a loop
   starts is exactly that.

2. **The accepted trade-off.** A region wider than the window can still carry
   the playhead off-screen while looping — the guard suppresses the *flip*,
   not the fact that a region can exceed what is visible. The loop band
   overlay (053) still shows where the loop lives even when the playhead
   itself is off the edge, so the user is never left with no indication of
   where the audio is. This was judged preferable to either fighting the
   user's zoom (rejected option) or leaving the double-flip in (053 note 13's
   original complaint).

3. **Why the guard is `< loopEnd`, not `<= loopEnd`.** `LoopRegion` is a
   half-open interval, `[start, end)` (`audio/engine.ts`) — the wrap lands
   exactly at `end` never being reached as a resting position, and a seek
   landing at or past `loopEnd` is the trap-past-end case (053 note 2) that
   must still be followed, not suppressed. Mirroring the interval's own
   half-openness in the guard is what keeps that case working without a
   separate branch.

4. **Closes 053 note 13 and the CHANGELOG 0.2.0 "What it cannot do" third
   bullet** ("Auto-follow can page-flip twice a loop…"). The changelog entry
   itself is 069's to update, alongside the rest of the v0.3.0 release notes.

5. **The smallest possible diff.** The change touches only the follow
   effect's early-return condition and its dependency array (`loopRegion`
   added). It does not touch `useTimelineGeometry`, the loop-region gesture,
   the band overlay, or 065's unmerged `windowStore` write-through around
   this same hook — that branch's diff will still apply cleanly against this
   one's shape of the effect.
