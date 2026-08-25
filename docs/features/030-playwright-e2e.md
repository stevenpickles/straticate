# [030] Playwright E2E tier (fake separator)

Branch: `030-playwright-e2e`
Status: PR OPEN
Dependencies: 024
PR: #35

## Objective

M1's workflow is covered by automated browser tests, running against the fake
separator, in CI, on every PR — the tier that has been scheduled since M1 and
would have caught at least two defects this project shipped and then fixed by
hand.

## Scope

- **Playwright in `frontend/`**: `@playwright/test`, `playwright.config.ts`, an
  `e2e/` directory, and `npm run e2e` / `e2e:ui` / `e2e:browsers`. Chromium
  only.
- **Both servers are started by the suite**: the backend on a dedicated port
  with `STRATICATE_DATA_DIR` in a temporary directory, and the Vite dev server
  pointed at it through the new `STRATICATE_BACKEND_URL` override in
  `vite.config.ts`. A developer's own `:8000` backend and `:5173` dev server
  are untouched.
- **Generated audio fixtures** (FFmpeg, at setup time, into that temporary
  directory), removed with everything else at teardown. No audio is committed.
- **Specs** covering upload (both routes) → configure → separate → chunk
  progress → telemetry → completion → cancel → inspect → playback controls →
  export → reconnect resync → reload.
- **CI**: a third job, `e2e`, running in parallel with `backend` and
  `frontend`.
- **DEVELOPMENT.md**: the test-strategy row, the CI plan, and how to run the
  tier locally.

## Out of scope

- Any change to application behaviour. Two findings are recorded below rather
  than fixed.
- `backend/src/straticate/inference/**`, `models/catalog.json`,
  `backend/pyproject.toml` (feature 027 owns them).
- Visual regression, screenshot diffing, cross-browser matrices, performance
  budgets.
- Anything needing a real model or a GPU.

## Expected modules/files

- `frontend/playwright.config.ts`, `frontend/tsconfig.e2e.json`
- `frontend/e2e/environment.ts` — ports, paths, fixture definitions
- `frontend/e2e/global-setup.ts` / `global-teardown.ts` — fixtures in, run
  directory out
- `frontend/e2e/app.ts` — page object, the two upload routes, socket control
- `frontend/e2e/upload.spec.ts`, `separation.spec.ts`, `cancel.spec.ts`,
  `resync.spec.ts`
- `frontend/vite.config.ts` — `STRATICATE_BACKEND_URL`; Vitest scoped to `src/`
- `frontend/package.json`, `eslint.config.js`, `tsconfig.json`, ignore files
- `.github/workflows/ci.yml` — the `e2e` job
- `DEVELOPMENT.md`

## Acceptance criteria

- [x] `npm run e2e` passes locally against a freshly started backend +
      frontend — 13 tests, ~35 s.
- [x] The suite covers upload → configure → separate → progress → telemetry →
      cancel → inspect → playback controls → export → reload/resync.
- [x] A test fails if a terminal job can be reverted by a stale REST snapshot,
      and one fails if a phase has no route out (both verified by
      reintroducing the defects — see *Mutation-tested*).
- [x] No fixed sleeps anywhere in the suite.
- [x] Runs in CI on every PR; no GPU, no model download, nothing left behind.
- [x] No committed audio.
- [x] `npm test` (517 Vitest tests) unaffected; all frontend and backend gates
      pass.
- [x] DEVELOPMENT.md's test strategy and CI plan match reality.

## Required tests

The tier *is* the tests. What each one is for:

| Spec | Covers |
| --- | --- |
| `upload.spec.ts` | the file picker and a real drag-and-drop; ffprobe metadata; the mode/quality/stem lists rendered from the catalog |
| `separation.spec.ts` | the four-stem job end to end: chunk progress, telemetry, completion, the stem list and its controls, a real download, and the route back out of `inspect` |
| `cancel.spec.ts` | a job cancelled mid-run reaches `cancelled`, offers a way onward, and stays cancelled across a reconnect |
| `resync.spec.ts` | a stale REST snapshot cannot revert a completed job; a reload mid-run leaves the backend job alone and the app usable |

## Notes / decisions

### The two defects, and how the tier catches them

**Defect 1 — a stale REST snapshot stranding the progress UI.** On reconnect
the app refetches the tracked job (`JobEventBridge`), and that answer can
predate a `job_completed` already applied. The old code let it win, reverting a
finished job to `separating` with a live Cancel button and no further event
coming to fix it.

Reproducing that against a real backend is not possible by waiting — the
backend's answer is always current. So `resync.spec.ts` *builds* the stale
record from the real completed one (`state: 'separating'`, no result), serves
it with `page.route`, and severs the socket to force the refetch. It then
asserts the app stayed `Completed`, and does the whole thing **twice**, because
"reached the right state" and "stayed there" are different claims. The route
handler counts its answers and the test asserts the count, so the test cannot
pass by the resync never happening.

**Defect 2 — `inspect` was a dead end.** The last test of
`separation.spec.ts` leaves the phase through the control the stem player
carries and asserts it lands somewhere the user can act (`configure`, file
still uploaded, Start enabled). The cancel spec makes the same "no dead ends"
assertion for a terminal job in `separate`.

### Mutation-tested

Both were verified by reintroducing the defects in a scratch working tree and
re-running the tier:

- removing the terminal-state guard in `state/jobState.tsx` → *"a stale REST
  snapshot cannot revert a completed job"* fails, with exactly the historical
  symptom (`stage` reverted to "Separating");
- removing the restart button from `components/StemPlayer.tsx` → *"offers a
  route back out of the inspect phase"* fails.

No other test failed in either case. The mutations were reverted; the
application is unchanged by this feature.

### No fixed sleeps — what is waited on instead

Every wait is a real condition: Playwright's auto-waiting locators, `expect`
retries, `expect.poll` for backend state, `waitForResponse` for a REST answer
that has actually arrived, `waitForEvent('download')` for the browser's
download, and `requestAnimationFrame` twice ("let the browser paint") where a
test needs to assert that something did **not** change. `grep -rn
"waitForTimeout\|setTimeout\|sleep" frontend/e2e` finds nothing.

Two things make that possible without racing: the fixtures are sized in chunks
(a 60 s fixture is a twelve-chunk job, so progress, telemetry and a live Cancel
are all observable for seconds), and the `job_progress` frames are recorded off
the WebSocket, so "progress advanced through every chunk" is asserted on the
whole event sequence rather than on whatever the DOM happened to be showing.

### Driving a socket drop

Nothing in the UI can drop a connection, and feature 016's client owns its
socket privately. `e2e/app.ts` therefore wraps `window.WebSocket` in an init
script and keeps the instances, so a spec can close the live one; the client
sees an unexpected close and reconnects with its own backoff, and the reconnect
triggers the REST resync the tests are aiming at.

### Findings — not fixed here

1. **The session does not survive a page reload.** Workflow state lives in
   React context only, so reloading mid-job returns the app to file selection
   while the backend job runs on to completion, unreachable from the UI. The
   test asserts today's behaviour (comes back clean and usable, backend
   untouched) and is where the assertion would change if this is fixed. It is
   not a regression and not a defect of any one feature — it is a missing
   capability (persist the tracked job ID, e.g. in `sessionStorage`, and
   rehydrate from `GET /jobs/{id}` on load). Worth a numbered feature.
2. **The e2e CI job installs `torch` (~183 MiB) that it never uses.**
   `straticate.main` → `inference/registry.py` imports the RoFormer builder at
   module import time, so the application cannot start without PyTorch even
   for a run that only ever touches the fake separator. An optional dependency
   group plus a lazily imported builder would fix it; `backend/pyproject.toml`
   belongs to 027 right now, so this is recorded rather than done.

Neither is a correctness bug in shipped behaviour, and nothing in the
application was changed to make a test pass.

### Smaller decisions

- **One worker, no parallelism.** The backend runs one separation at a time
  (ARCHITECTURE.md §6), so parallel specs would queue behind each other
  anyway.
- **`describe.serial` in `separation.spec.ts`.** Its tests are stages of one
  run, sharing a page and a job; giving each its own job would multiply the
  runtime for no coverage.
- **Nothing about the catalog is hardcoded.** The specs read
  `GET /separation-modes` and drive the UI from it — the four-stem mode is
  *found*, not named — so a catalog that gains a mode, a tier or a stem does
  not need an edit here (AGENTS.md principle 6).
- **`--host 127.0.0.1` on the dev server.** Vite's default `localhost` bind
  resolves to `::1` first on Windows, and then nothing answers on 127.0.0.1,
  including Playwright's readiness probe.
- **FLAC for the export test.** It exercises the format picker and keeps the
  built zip small; the expected filename is read back from the control rather
  than written out.
- **`--autoplay-policy=no-user-gesture-required`.** The stem player builds a
  Web Audio graph; without the flag Chromium suspends the context for reasons
  unrelated to what is being tested.
