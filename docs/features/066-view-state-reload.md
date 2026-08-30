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
  `scrollSeconds` — numbers only, no records). A record under this key with
  no `view` field still restores its identifiers; a malformed or
  wrongly-shaped `view` is tolerated the same way. A *real* v1 record — one
  actually written under the superseded `straticate.session.v1` key — is
  simply not found under this key, so it is silently ignored exactly like
  any other unparsable payload; it is never read, let alone upgraded.
  `isEmptySessionSnapshot` now also considers `view`, so `writeViewSnapshot`
  — a new read-modify-write helper that updates just the view, leaving the
  identifiers alone — is never silently dropped by the "empty snapshot"
  check.
- `frontend/src/state/SessionGate.tsx`: its identifier write now reads the
  on-disk `view` forward rather than overwriting it with nothing, since the
  two writers (this one and `stemSession.tsx`) touch the same key at very
  different frequencies — but drops that view when its `jobId` does not
  match the job actually being tracked (post-review nit fix), so a restore
  that finds the job gone does not pin a dead job's view in storage forever.
- `frontend/src/state/stemSession.tsx`: reads the persisted view once at
  provider mount (`initialView`); seeds the timeline window for whichever job
  is tracked (restored, or `WHOLE_FILE`) from an effect keyed on
  `[jobId, initialView]`; restores the playhead and loop region exactly once
  per page load, when the engine reaches `ready` for the **matching**
  `jobId`; exposes `persistView()`, the write path every discrete commit site
  calls; registers one `pagehide` flush while a session is open; and clears
  the persisted view when the tracked job goes away (`job/clear` or a
  different job). `windowStore.set` — reached by every zoom/pan/wheel tick,
  thumb-drag and auto-follow — writes only the in-memory window, never
  storage (post-review should-fix): the `pagehide` flush and the other
  discrete commits already write the current window as part of their own
  full-view write, so nothing is lost, and per-tick storage I/O from inside
  a state updater is gone.
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
- [x] A record with no `view` field still restores its identifiers.
- [x] A malformed or mismatched-shape `view` is tolerated, not thrown on.
- [x] Writes happen at every discrete commit — seek, loop set/clear, pause —
      plus one `pagehide` flush for the reload-while-playing case. A named
      viewport movement (zoom, pan, wheel, thumb-drag, auto-follow) is
      **not** its own write: `windowStore.set` only updates the in-memory
      window, and the current window rides along on the next discrete commit
      or the `pagehide` flush, whichever comes first (post-review
      should-fix — see the module docstring).
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

- `persistence.test.ts`: v2 round-trip with and without a view; a record
  under the v2 key with no `view` field restores; a malformed view is
  tolerated; a view for a different job round-trips intact (matching is the
  caller's job, not this module's); `writeViewSnapshot` updates the view
  without touching the identifiers and vice versa; a view-only snapshot is
  not "empty"; a raw `1e999` in the stored `view` (parses to `Infinity`) is
  rejected by `optionalFiniteNumber`.
- `stemSession.test.tsx`: restore fires exactly one `seek` (+ `setLoopRegion`
  when the view carried a region) after `ready`, and not again on a later
  snapshot notification; no restore with no persisted view; a mismatched
  `jobId` is dropped; the window store is seeded before the first
  `StemTimeline` mount; each discrete commit site (seek, loop set, loop
  clear, pause) persists; a viewport move alone (`windowStore.set`) writes
  nothing (the regression pin for the should-fix above); the `pagehide`
  flush writes the live `currentTime` and the current window — ahead of the
  last discrete commit, and regardless of whether any viewport move since
  that commit was separately persisted; `job/clear` wipes the view.
- `separation.spec.ts`: the feature's own stage, described above — unchanged
  by the should-fix, since `page.reload()` fires `pagehide`, which is still
  a full-view write.

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
- **Post-review: `windowStore.set` no longer persists (should-fix).** The
  first version called `persistView()` from inside `set` itself, so a
  session's `initialView`/`persistView` closure could stay current for the
  window store without a second seam. That made every wheel tick — pan,
  zoom, thumb-drag, auto-follow, on the order of 100/s on a trackpad —
  a synchronous read-parse-validate-stringify-write of the whole
  `sessionStorage` record, from inside a React state updater (render-phase
  I/O), doubled by StrictMode. It was also unnecessary: `pagehide` already
  writes the whole view fresh, including whatever window `set` last left in
  the ref, so no window move was ever actually lost by removing the
  write-through. `set` now only writes {@link timelineWindowRef}; the
  window's durable persistence points are `pagehide` plus the existing
  discrete commits (seek, loop set/clear, pause). See `stemSession.tsx`'s
  module docstring for the full write-path table.
- **Post-review: the module docstring's window-seeding paragraph was
  describing an earlier design (should-fix).** It said `windowStore` was "a
  `useMemo` keyed on `jobId`" holding the restored window in a closure —
  true of a version before the one that shipped, per the Notes bullet above
  about the seed living in an effect. The code has always been a ref
  (`timelineWindowRef`), a `windowStore` memoized once on `[]`, and a
  separate seed effect keyed on `[jobId, initialView]`; the docstring now
  says that.
- **Post-review: a stale `view` used to pin the session key alive (nit).**
  When restore found the tracked job gone (a backend restart), `SessionGate`
  wrote `{ jobId: null, …, view: <the dead job's stale view> }` — and because
  `isEmptySessionSnapshot` counts `view`, that record was never "empty," so
  the key was never removed. Every later reload restored nothing (the job
  was still gone) but still showed "Restoring your session…" while the
  no-op restore ran. `SessionGate`'s identifier-write effect now drops
  `view` when its `jobId` does not match the job actually being tracked,
  the same rule `stemSession.tsx` already applies when *reading* a view
  back.

### Known limitations

- **A viewport move made before the engine reaches `ready`, followed by a
  second reload before anything else commits, can come back at the whole
  file rather than where it was left.** Moving the window no longer writes
  storage on its own (see the should-fix above); it rides along on the next
  seek, loop edit, pause or `pagehide`. If a second reload happens first —
  with no discrete commit and no `pagehide`-worthy tab close in between —
  there was never a write to have captured the move, so restore falls back
  to `WHOLE_FILE`. Narrow (it needs two reloads back to back with literally
  nothing else happening between them) and deliberately not engineered
  around: the only fix would be persisting on every viewport move again,
  which is the exact cost this feature's should-fix removed.
