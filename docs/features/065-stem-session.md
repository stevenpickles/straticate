# [065] Job-scoped stem session (the engine hoist)

Branch: `065-stem-session`
Status: PR OPEN
Dependencies: 064
PR: #96

## Objective

The audio engine's lifetime becomes **the tracked job's, not the Inspect
screen's**. Unmounting the stem player no longer disposes the Web Audio graph,
re-downloads every stem or resets the playhead, loop region and zoom window;
only clearing or changing the tracked job takes the session down.

## Scope

- New `frontend/src/state/stemSession.tsx`: `StemSessionProvider` /
  `useStemSession`, mounted in `App.tsx` inside `JobStateProvider` beside
  `JobEventBridge`. It owns, keyed to the tracked `jobId`:
  1. the `GET /jobs/{id}/result` fetch — the `ResultState` machine and feature
     048's `attempt` counter, moved out of `StemPlayer`;
  2. the `StemPlayerEngine` instance, built on the first load the session needs
     and then kept;
  3. the timeline window, as a `TimelineWindowStore` ref pair.
- `useTimelineGeometry(durationSeconds, windowStore?)`: seeds its
  `{ zoom, scrollSeconds }` state from the store and writes back through it in
  `applyToViewport` and `zoomToFit` — the only two places a window changes.
- `StemTimeline` gains one optional `windowStore` prop, plumbed from
  `StemPlayer`. It is the whole of this feature's footprint in that file.
- `StemPlayer` becomes a consumer: no `createEngine` prop (the injection moved
  to the provider), no result fetch, no engine effect, and `currentTime` is
  seeded from `engine?.currentTime() ?? 0` rather than from `0`.

## Out of scope

- Reload persistence (066) — `persistence.ts` and `SessionGate` are untouched.
- Lane height / fader accessibility (067) — `TimelineLane.tsx` and
  `StemTimeline.css` are untouched.
- Auto-follow inside a loop region (068).
- Anything in the backend.

## Expected modules/files

- `frontend/src/state/stemSession.tsx` (new)
- `frontend/src/state/stemSession.test.tsx` (new)
- `frontend/src/components/StemPlayer.tsx`
- `frontend/src/components/StemTimeline.tsx`
- `frontend/src/components/useTimelineGeometry.ts`
- `frontend/src/App.tsx`
- `frontend/src/components/StemPlayer.test.tsx`,
  `frontend/src/components/Workspace.test.tsx` (harness)

## The ownership invariant

**One session per tracked job; views read it, nothing else owns it.**

| Thing | Lived in (before) | Lives in (after) |
| --- | --- | --- |
| `GET /jobs/{id}/result` + `attempt` | `StemPlayer` | `StemSessionProvider` |
| `StemPlayerEngine` | `StemPlayer` | `StemSessionProvider` |
| `{ zoom, scrollSeconds }` | `useTimelineGeometry` state | `StemSessionProvider` ref, seeded/written through by the hook |
| playhead, loop region, mute/solo/level, decoded buffers | the engine | the engine (unchanged — they were already engine-resident, and survive for free once the engine does) |

A session stays **shut** until a view calls `openSession()`; `StemPlayer` calls
it from a mount effect. A job watched to completion and never opened therefore
fetches nothing and builds no engine.

## Dispose triggers

Exhaustive. All three are the cleanup of one provider effect keyed on `jobId`.

| Trigger | Reached by | What happens |
| --- | --- | --- |
| `jobId → null` | `job/clear` — both "Start another separation" sites (`StemPlayer`, `SeparationProgress`) | dispose immediately: sources stopped, nodes disconnected, in-flight downloads aborted, context closed, buffers dropped |
| `jobId` changes to a different job | a new `POST /jobs`, a restored session, a re-track | dispose the old engine, reset the result/attempt state **and the window store** — a new job is a new timeline |
| the provider unmounts | the app going away; React's development double-invoke | dispose (idempotent, and nothing is built yet on the double-invoke's first pass) |

**Unmounting `StemPlayer` is not on that list.** That is the feature.

A *result* that changes for the same job — feature 048's "Try again" finally
succeeding — calls `engine.load()` again on the **same** instance rather than
disposing and recreating: `load()` is generation-guarded, aborts the previous
attempt's downloads and tears down the old graph itself. That reload does
re-fetch every stem's audio (it is a full load, not a diff), which is accepted:
the alternative is a second engine, and every consumer's reference to the first.

## Behavioural consequence: playback survives the view

**Audio that is playing keeps playing when the Inspect UI is unmounted.** The
transport belongs to the session, and nothing about a view going away stops it.
This is precedent, not novelty: `App.tsx` has documented since feature 037 that
opening the model library "hides the workspace without unmounting it" precisely
so "the stem player's Web Audio graph … survive[s] a trip to the library and
back". 065 moves that guarantee from a `hidden` attribute one screen happens to
use to the place the session lives. It is also why `job/clear` disposes
*immediately* rather than lazily — "Start another separation" has to be silence.

## Memory

A retained session retains decoded audio: roughly **340 MB** for a four-stem,
four-minute stereo job at 44.1 kHz (`4 × 2 × 44100 × 240 × 4 B ≈ 339 MB`), held
for as long as that job is the tracked one. That is the price of not
re-downloading ~10 s of stems on every re-entry, and it is bounded — one
session at a time, freed the moment the job stops being tracked.

**Lazy opening is the mitigation.** A job that is never inspected costs nothing
at all: no result fetch, no engine, no buffers. The cost is only ever paid by a
job the user actually listened to.

## Acceptance criteria

- [x] Unmounting `StemPlayer` with a loaded session disposes nothing and
      re-fetches nothing.
- [x] Remounting it finds the **same** engine instance and lands on the
      preserved playhead (seek to 0:12, leave, return, readout still 0:12).
- [x] The loop region and the zoom/scroll window survive the same round trip.
- [x] `job/clear` disposes the engine and resets the session.
- [x] A different tracked job disposes the old engine, builds a new one, and
      resets the window store to the whole file.
- [x] A second result for the same job reloads the same instance; `dispose` is
      not called.
- [x] Nothing is fetched for a tracked job until `openSession()` is called.
- [x] Every pre-existing `StemPlayer` / `StemTimeline` assertion still holds,
      with the engine injection moved to the provider.
- [x] Five frontend gates green; the e2e tier green.

## Required tests

- `frontend/src/state/stemSession.test.tsx` (new, 11 cases): lazy open,
  survival of engine/result/playhead/loop/window across an unmount and remount
  of the player, the three dispose triggers, and the same-instance reload.
- `frontend/src/components/StemPlayer.test.tsx` (87 cases): unchanged
  assertions, harness moved up one level.

### Fail-first

The headline assertion was run against unmodified `dev` first, through a
temporary probe that mounted `StemPlayer` under a toggle:

```text
disposeCount after unmount: 1
AssertionError: expected 1 to be +0        // engine.disposeCount
AssertionError: expected 2 to be 1         // result fetches after remount
```

Both failures are the pre-065 behaviour exactly: one disposal per unmount, one
extra result fetch (and, behind it, one extra full stem download) per re-entry.

### End to end

The existing tier stays green as it is (27 Playwright cases across the six spec
files, including `models.spec.ts`'s "the library never disturbs the workflow it
sits beside", which exercises survival across the library round trip today).
**No new e2e stage is added, deliberately**: there is no in-app path that leaves
the Inspect phase while keeping the job tracked — "Start another separation"
clears the job, and the library uses `hidden` rather than unmounting. Feature
066 adds the reload stage that will exercise this end to end.

## Notes / decisions

- **`openedFor`, not an `opened` flag.** The session records *which job* it was
  opened for, so "open" is answered against the job tracked right now. A job
  change shuts the session with no state write, which is what keeps a stale
  effect closure from fetching for the new job before a view has asked for it.
  `openSession`'s identity changes with the tracked job, so a view that calls
  it from a mount effect re-opens automatically when the job changes underneath
  it.
- **Only settled outcomes are stored.** `idle` and `loading` are derived from
  whether the session is open and whether the settled answer's `jobId#attempt`
  key matches the current request. Storing them would mean a synchronous
  `setState` in an effect body — a cascading render, and one that
  `react-hooks/set-state-in-effect` rejects.
- **The window store is a ref pair, not context state.** Zoom and scroll change
  on every wheel tick; putting them in provider state would re-render the whole
  workspace per tick. `useTimelineGeometry` reads `get()` exactly once, to seed
  its own state.
- **`applyToViewport` and `zoomToFit` are the only two places a window
  changes**, which is what makes the write-through complete. A third movement
  added later must write through as well, or a re-entered timeline will open on
  a stale window.
- **Noticed, not touched:** `StemTimeline.tsx` is in flight for feature 067;
  this branch adds one optional prop (`windowStore`) and one hook argument to
  it and nothing else. `TimelineLane.tsx` and `StemTimeline.css` are untouched.
