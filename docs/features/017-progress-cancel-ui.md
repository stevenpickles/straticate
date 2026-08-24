# [017] Progress UI + cancel + error handling

Branch: `017-progress-cancel-ui`
Status: PR OPEN
Dependencies: 011, 015, 016
PR: #23

## Objective

The `separate` phase becomes real: the browser opens the job event socket for
the whole session, shows live chunk-grained progress and the current stage,
lets the user cancel a running job, and renders every terminal outcome
(completed / cancelled / failed) clearly. This is the feature that makes the
WebSocket path visible to a human.

## Scope

- `frontend/src/ws/JobEventBridge.tsx` — renderless component that mounts
  `useJobEvents()` once per session and, on every socket `open` (first connect
  and every reconnect), refetches the tracked job with `getJob` and dispatches
  `job/track`. REST is the source of truth on (re)connect.
- `frontend/src/App.tsx` — mounts `<JobEventBridge />` directly under
  `JobStateProvider`, above the workspace.
- `frontend/src/components/SeparationProgress.tsx` (+ `.css`) — the real
  progress panel: determinate `progressbar`, humanized stage, chunk counts,
  elapsed time, audio processed/total, the queued state, all three terminal
  states, the cancel affordance, and the WebSocket connection status whenever
  it is not `open`.
- `frontend/src/state/jobState.tsx` — a cancel-request slice
  (`idle` / `requesting` / `error` with the envelope `code` + `message`), reset
  when a different job is tracked and cleared on the terminal transition; a
  demotion guard in `trackJob` so a stale REST snapshot cannot walk a job back
  out of a terminal state; and no more `job_created` adoption, now that this
  feature keeps the (broadcast) socket open from app start.

## Out of scope

- Anything under `backend/` and `frontend/src/api/generated/api.d.ts`
  (021 was regenerating it in parallel).
- `frontend/src/components/TelemetryPanel.*`, `frontend/src/format.ts`,
  `frontend/src/test/fixtures.ts` — owned by 020 this wave; read, never
  written.
- `frontend/src/ws/client.ts`, `frontend/src/api/jobs.ts` and their tests
  (016), `frontend/src/components/{SeparationOptions,Workspace,DropZone,
  AudioSummary,Header}.tsx` and `frontend/src/state/appState.tsx` (011),
  `frontend/src/index.css`.
- The stem player / results display (023), export UI (024), the telemetry
  panel's content (020), a job list or history UI (unclaimed).

## Expected modules/files

- `frontend/src/components/SeparationProgress.tsx` · `.css` · `.test.tsx`
- `frontend/src/ws/JobEventBridge.tsx` · `JobEventBridge.test.tsx`
- `frontend/src/App.tsx` · `App.test.tsx`
- `frontend/src/state/jobState.tsx` · `jobState.test.tsx`
- `docs/features/017-progress-cancel-ui.md` · `ROADMAP.md`

## Acceptance criteria

- [x] The job event socket is opened once per session and its events drive the
      UI; on (re)connect the tracked job is refetched over REST.
- [x] Progress is chunk-grained and real — driven by `job_progress`
      (`chunks_completed / chunks_total`), never a timer or an animation
      standing in for work.
- [x] Every job state in the contract renders sensibly, including `queued`
      (no progress yet) and each of the three terminal states.
- [x] A failed job shows `job.error.message` (with `code` rendered alongside
      for support); a cancelled job shows the stage it was cancelled at.
- [x] Cancel issues exactly one `POST /jobs/{id}/cancel`, shows a "Cancelling…"
      affordance while the job is still processing, and settles on the
      `job_cancelled` event; a failed cancel is shown and retryable.
- [x] A non-`open` socket status is visible to the user.
- [x] No hardcoded stem names, mode IDs or model IDs anywhere in the diff.
- [x] `index.css` is untouched; all new styles live in `SeparationProgress.css`.
- [x] `npm run format:check` · `lint` · `typecheck` · `test` · `build` green.

## Required tests

- `SeparationProgress.test.tsx` — queued rendering (no progress bar, still
  cancellable); progress bar values (`aria-valuenow`/`min`/`max`), chunk
  counts, elapsed and audio processed after a `job_progress` event delivered
  over the mock socket; the stage rendering of all six processing states;
  `completed`, `cancelled` (with and without a known stage) and `failed`
  (message + code) renderings; no cancel button on any terminal state; cancel
  posts exactly once and shows "Cancelling…" while the response is still a
  processing state, then settles on `job_cancelled`; a double click issues one
  POST; a failed cancel shows the envelope message and the retry succeeds in
  posting again; `connecting`, `reconnecting` (driven by a real socket drop)
  and `closed` are surfaced.
- `JobEventBridge.test.tsx` — connects on mount and closes on unmount; renders
  nothing; feeds events into the store; refetches the tracked job over REST on
  open and again after a drop + reconnect; does nothing while no job is
  tracked; picks up a job adopted after the socket was already open; a failed
  resync leaves the store intact.
- `jobState.test.tsx` — the cancel slice: request recorded; requests ignored
  with no job and on a terminal job; failure carries `code` + `message`;
  `requesting` survives a cancel response that is still a processing state;
  settles on `job_cancelled` and on any other terminal transition; a cancel
  failure is cleared by the terminal transition; reset when a different job is
  tracked and by `job/clear`.
- `App.test.tsx` — the session opens exactly one job event socket at
  `/api/v1/ws` and closes it on unmount.

Regression tests for the three defects PR review found (each fails against the
code as first written):

- `jobState.test.tsx` — a `job/track` carrying a non-terminal record cannot
  replace a `completed` or `cancelled` job of the same id; a snapshot that
  carries the job *forward* is still accepted; the guard is per job id, not
  global; a `job_created` broadcast is never adopted while nothing is tracked,
  but still refreshes the record of the job this client does track.
- `JobEventBridge.test.tsx` — a `getJob` response that predates a
  `job_completed` event does not revert the store (the completion and its
  result survive); a `job_created` broadcast is neither adopted nor refetched
  on the next reconnect; a job tracked *after* the socket opened is resynced on
  the next reconnect; only one socket is opened as the tracked job changes.
- `SeparationProgress.test.tsx` — a `cancelJob` response that predates a
  `job_cancelled` event does not revert the store (no Cancel button reappears
  for a dead job); a cancel response, and a cancel failure, that resolve after
  a different job was tracked are both ignored.

## Notes / decisions

### The socket is a session resource, not a phase resource

`JobEventBridge` mounts under `JobStateProvider`, above the workspace, rather
than inside `SeparationProgress`. Mounting it with the phase would tie the
socket's lifetime (and its reconnect backoff) to whatever the workspace happens
to be rendering, so an event arriving during a phase switch would be lost. One
socket per session, opened on mount and closed on unmount, is both simpler and
what 016's `useJobEvents` was built for.

The bridge reads the tracked job id from a ref rather than closing over it, so
starting a new job never tears down and rebuilds the subscription. A resync
whose response arrives after the tracked job changed is discarded, and a failed
resync is swallowed: the connection status is already on screen, events keep
arriving, and the next reconnect tries again.

### Stage labels are derived, not tabulated

`JobState` is a backend-owned contract with ten members and room for more.
A hand-written label map would silently render nothing for a state added later,
so the component humanizes the identifier instead (`loading_model` →
`Loading model`), in the same spirit as the backend humanizing mode and tier
IDs for `SeparationMode` (feature 010). The tests pin all six processing
labels.

### Cancelling is a request; `requesting` outlives the HTTP call

`POST /jobs/{id}/cancel` answers with the job *as it is now*, which for a
running job is still a processing state — so the cancel slice cannot settle on
the HTTP response. `cancel/requested` therefore stays `requesting` until the
job reaches **any** terminal state, which is what `settleCancel` in the reducer
enforces (it also covers the race where the job completes or fails before the
cooperative checkpoint is reached). The click handler guards double submission
with a ref that flips synchronously, the same pattern feature 011 used for
"Start separation".

There is deliberately **no** 409 / "not cancellable" path: cancelling a
terminal job is a `200` no-op (`docs/contracts/rest-api.md`), and the cancel
button is not rendered at all once the job is terminal.

### REST snapshots must never demote a job (PR review, defects 1 & 2)

`getJob` and `cancelJob` both answer with the job **as it was when the handler
ran**, and both race the event stream:

- `JobEventBridge.resync` runs *after* the socket is open, so events stream in
  during the fetch. Reconnect while `separating`, apply `job_completed` at
  t+5 ms, and a snapshot taken pre-completion resolving at t+30 ms would
  overwrite it — reverting the store to `separating` with `result: null`.
- The cancel handler is the same shape: a worker that reaches its cooperative
  checkpoint quickly emits `job_cancelled` before the HTTP reply lands, and the
  pre-cancel snapshot would be written back over it.

Both are **permanent**: the job has genuinely stopped, so no further event will
ever arrive to correct the mistake, and the panel is stranded mid-run with a
live Cancel button.

The fix is a single rule in `trackJob`: when the incoming record has the same
id as the tracked one, a **non-terminal** record may not replace a **terminal**
one. It lives in the reducer rather than in each caller so every present and
future `job/track` producer inherits it. Events are authoritative for forward
progress; REST is authoritative only for resyncing a job the events have not
already carried further. A snapshot that moves the job *forward* (including
terminal-to-terminal) is still accepted, and the guard is scoped to a matching
id, so tracking a genuinely different job is unaffected.

`SeparationProgress`'s cancel handler additionally got the "a different job may
have been started while the request was in flight" guard that `JobEventBridge`
already had, on both the success and the failure path — otherwise a late reply
re-tracks the old job, or reports the old job's failure against the new one.

### No `job_created` adoption (PR review, defect 3)

The store used to adopt a `job_created` event when nothing was tracked. The hub
broadcasts every event to **every** connected client, and until this feature
nothing opened the socket, so the branch never fired in practice. This feature
holds the socket open from app start through the whole `select`/`configure`
phase, which would let a second tab — or another machine against the same
backend — silently adopt the first one's job, and `JobEventBridge` would then
refetch that foreign job on every reconnect.

The branch is therefore **removed** rather than restricted: a job this client
starts is already tracked from the `POST /jobs` response (feature 011), which
is authoritative about ownership, so adoption bought nothing that a
`job/track` did not already provide. `job_created` for the job that *is*
tracked still refreshes the record. This supersedes the "the only exception: a
`job_created` event is adopted when nothing is tracked yet" note in
`docs/features/016-job-ws-clients.md`; 016's doc was left as the record of what
016 shipped.

### Progress fraction and its fallback

The bar reads `useJobState().progress?.progress` — the newest `job_progress`
event — and falls back to `job.progress` from the REST record, which is what a
freshly tracked or freshly resynced job has before the first event arrives. The
value is clamped to `0..100` for `aria-valuenow` and the fill width. The
`.progress-track` / `.progress-fill` rules already in `index.css` (from the
upload progress bar) are reused, so nothing global changed.

### Known limitations

- The completed state announces that the stems are ready and how many there
  are, but the stem player and export live in features 023/024; this panel just
  says so.
- There is still no way back to `configure` from `separate`, and no "start
  another separation" affordance — unchanged from 011 and still unclaimed.
- Two frontend nits inherited from 016 were left alone as out of scope
  (`jobs.ts` and `jobs.test.ts` belong to 016 and the assignment forbids
  editing them): `listJobs()`'s TSDoc claims newest-first ordering while the
  backend is oldest-first, and `jobs.test.ts` mocks a `job_not_cancellable`
  (409) response the backend never produces. Both are already recorded in 015's
  known limitations and want a small follow-up feature.
