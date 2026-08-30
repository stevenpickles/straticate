# [066] View state survives a reload

Branch: `066-view-state-reload`
Status: PR OPEN
Dependencies: 033, 065
PR: —

## Objective

The audio engine cannot survive a page reload — the stems have to be
re-downloaded and re-decoded from scratch, which is C12/v0.4.0's problem, not
this one's — but the *view* of them can. After `page.reload()`, the playhead
position, the loop region, and the timeline's zoom/scroll window are restored
once the engine for the tracked job reaches `ready`.

## Scope

- `frontend/src/state/persistence.ts`: the storage key bumps to
  `straticate.session.v2`. A new optional `view` field, keyed by `jobId`
  (`ViewSnapshot`: `positionSeconds`, `loopStart`/`loopEnd`, `zoom`,
  `scrollSeconds` — numbers only, no records). A v1 record (no `view` key)
  still restores its identifiers; a malformed or wrongly-shaped `view` is
  tolerated the same way. `isEmptySessionSnapshot` now also considers `view`,
  so `writeViewSnapshot` — a new read-modify-write helper that updates just
  the view, leaving the identifiers alone — is never silently dropped by the
  "empty snapshot" check.
- `frontend/src/state/SessionGate.tsx`: its identifier write now reads the
  on-disk `view` forward rather than overwriting it with nothing, since the
  two writers (this one and `stemSession.tsx`) touch the same key at very
  different frequencies.
- `frontend/src/state/stemSession.tsx`: reads the persisted view once at
  provider mount (`initialView`); seeds the timeline window for whichever job
  is tracked (restored, or `WHOLE_FILE`) from an effect keyed on
  `[jobId, initialView]`; restores the playhead and loop region exactly once
  per page load, when the engine reaches `ready` for the **matching**
  `jobId`; exposes `persistView()`, the single write path every commit site
  calls; registers one `pagehide` flush while a session is open; and clears
  the persisted view when the tracked job goes away (`job/clear` or a
  different job).
- `frontend/src/components/StemPlayer.tsx`: calls `persistView()` after the
  seek commit, the loop set/clear commits, and a pause. Its readout effect
  also gains `snapshot.status` as a dependency — the session can move the
  engine's clock itself (the restore), with no gesture on this component to
  have called `setCurrentTime`, so the readout has to notice the transition
  that could have moved it.
- `frontend/e2e/resync.spec.ts`: the hardcoded storage key and the expected
  key set both follow the v2 bump.
- `frontend/e2e/separation.spec.ts`: a new stage, right after the loop stage,
  is the feature's own end-to-end proof.

## Out of scope

- Streaming decode / engine state surviving a reload (C12, v0.4.0).
- The backend.
- `frontend/src/components/TimelineLane.tsx`, `StemTimeline.css` (067).
- `062`/`063`'s files.

## Expected modules/files

- `frontend/src/state/persistence.ts`, `persistence.test.ts`
- `frontend/src/state/SessionGate.tsx`, `SessionGate.test.tsx`
- `frontend/src/state/stemSession.tsx`, `stemSession.test.tsx`
- `frontend/src/components/StemPlayer.tsx`
- `frontend/e2e/resync.spec.ts`, `separation.spec.ts`
- `ROADMAP.md`, this document

## Acceptance criteria

- [x] `persistence.ts` stores an optional `view`, keyed by `jobId`, under a
      bumped `straticate.session.v2` key.
- [x] A v1-shaped record (no `view`) still restores its identifiers.
- [x] A malformed or mismatched-shape `view` is tolerated, not thrown on.
- [x] Writes happen at every discrete commit: seek, loop set/clear, a named
      viewport movement, pause — plus one `pagehide` flush for the
      reload-while-playing case.
- [x] Restore is exactly one `seek` and (when the view carried one) exactly
      one `setLoopRegion`, once the engine reaches `ready` for the matching
      `jobId`, never repeated for the same page load.
- [x] The timeline window is seeded before `StemTimeline`'s first mount.
- [x] A view for a different job is dropped, exactly like a stale job id.
- [x] `job/clear` clears the persisted view.
- [x] The e2e stage: seek, set a region, zoom in, reload, and the phase, the
      readout, the loop badge and the strip's `data-zoom`/`data-scroll-seconds`
      all come back.
- [x] Five frontend gates green; the e2e tier green.

## Required tests

- `persistence.test.ts`: v2 round-trip with and without a view; a v1-shaped
  record restores; a malformed view is tolerated; a view for a different job
  round-trips intact (matching is the caller's job, not this module's);
  `writeViewSnapshot` updates the view without touching the identifiers and
  vice versa; a view-only snapshot is not "empty".
- `stemSession.test.tsx`: restore fires exactly one `seek` (+ `setLoopRegion`
  when the view carried a region) after `ready`, and not again on a later
  snapshot notification; no restore with no persisted view; a mismatched
  `jobId` is dropped; the window store is seeded before the first
  `StemTimeline` mount; each commit site (seek, loop set, loop clear, a named
  viewport movement, pause) persists; the `pagehide` flush writes the live
  `currentTime`, ahead of the last discrete commit; `job/clear` wipes the
  view.
- `separation.spec.ts`: the feature's own stage, described above.

### Fail-first

A temporary probe (`frontend/src/state/_066_faillfirst.test.tsx`, not part of
the merged suite) mounted a session, seeked, drew a loop region, then mounted
a second, independent session over the same (real, jsdom) `sessionStorage` —
the unit-test stand-in for a reload, since an actual page reload runs none of
this process's JS, let alone a React cleanup function. Run against
`persistence.ts` / `stemSession.tsx` / `StemPlayer.tsx` / `SessionGate.tsx`
stashed back to their pre-066 state:

```text
AssertionError: the playhead should have been restored: expected [] to deeply equal [ 12 ]
```

The same probe passes once the implementation is restored.

## Notes / decisions

- **`isEmptySessionSnapshot` now considers `view`.** `writeViewSnapshot` can
  be the first writer to touch a fresh store (its read-modify-write starts
  from whatever is already on disk, identifiers or not); a snapshot with only
  a view is something worth restoring, not nothing.
- **The window seed lives in an effect, not in render.** An earlier version
  wrote the seed synchronously during render (and, before that, mutated a
  closure-local variable from inside `windowStore.set`) — both are exactly
  what `eslint-plugin-react-hooks`'s stricter, React-Compiler-oriented rules
  in this repo forbid (`react-hooks/refs`, `react-hooks/immutability`). The
  effect is safe because `StemTimeline` never mounts in the *same* render
  pass this effect runs in: it needs `result.status === 'loaded'`, which
  needs a `GET /jobs/{id}/result` round trip that has not even started the
  first time `jobId` takes a new value.
- **The readout effect gained `snapshot.status`.** Before this feature,
  nothing ever moved the engine's clock except a gesture on `StemPlayer`
  itself, which always also called `setCurrentTime` directly — so the
  readout's effect never needed to notice an externally-moved clock. The
  restore breaks that assumption once: `engine.seek()` is called from the
  session, not from a click, so the effect has to re-read the clock on the
  transition that could have moved it. Free the rest of the time:
  `engine.currentTime()` has not changed since the last read, and
  `setCurrentTime` bails out on the same value.
- **Noticed, not touched:** the ROADMAP ledger table around row 066
  documents the pairing with 065 in prose (row "Playhead, loop, zoom and
  decoded stems survive…") — left as is; only this feature's own row moved to
  `PR OPEN`.
