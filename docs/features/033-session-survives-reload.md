# [033] Session survives a page reload

Branch: `033-session-survives-reload`
Status: PR OPEN
Dependencies: 016, 017
PR: #39

## Objective

Reloading the browser during or after a separation returns the user to where
they were, with every record re-read from the backend. Before this feature a
reload was a new session: workflow state lived only in React context, so a
reload mid-job dropped the user back at file selection while the backend job
ran on to completion, unreachable from the UI.

## Scope

- **`frontend/src/state/persistence.ts`** — a guarded `sessionStorage` wrapper
  storing **identifiers only**: the tracked job id, the uploaded audio id, and
  the workflow phase. No `Job`, no `AudioFile`, no result, no metrics.
- **`frontend/src/state/SessionGate.tsx`** — rehydration on startup
  (`GET /jobs/{id}` → `job/track`, `GET /audio/{id}` → `upload/succeeded`,
  then `phase/set`), and the gate that holds the workspace back until it
  settles.
- **`frontend/src/api/audio.ts`** — `getAudio(id)` for `GET /api/v1/audio/{id}`,
  which had no client function.
- **`frontend/src/ws/JobEventBridge.tsx`** — resync now also fires when a job
  becomes the tracked one while the socket is already open, which is what a
  restored job does.
- **`frontend/e2e/resync.spec.ts`** — the reload test that asserted the old
  behaviour now asserts the new one, plus reload-after-completion and a stale
  stored id.

## Out of scope

- Anything under `backend/`; `frontend/src/api/generated/api.d.ts`.
- Feature 035's files (`SeparationOptions.tsx`, `state/appState.tsx`,
  `api/models.ts`, `e2e/install.spec.ts`).
- Persisting playback position, telemetry history or export state.
- Multi-job history or a job list UI.
- `localStorage`-based cross-tab sync — see *Session scope, deliberately*.

## Expected modules/files

- `frontend/src/state/persistence.ts` (+ `persistence.test.ts`)
- `frontend/src/state/SessionGate.tsx` (+ `SessionGate.test.tsx`)
- `frontend/src/App.tsx` (+ `App.test.tsx`)
- `frontend/src/ws/JobEventBridge.tsx` (+ `JobEventBridge.test.tsx`)
- `frontend/src/api/audio.ts`
- `frontend/e2e/resync.spec.ts`

## Acceptance criteria

- [x] Reload mid-job returns to the running job, which continues to completion
      in the UI.
- [x] Reload after completion returns to the results.
- [x] A stale or unknown stored job id starts cleanly, with no error the user
      cannot act on.
- [x] Only identifiers are persisted — no `Job`, result or metrics.
- [x] The app behaves normally when `sessionStorage` throws or is unavailable.
- [x] A job that finished while the page was closed rehydrates as completed,
      and rehydration cannot rewind a terminal job.
- [x] E2E covers all of the above with no fixed sleeps; `npm test` unaffected
      (517 → 553 tests, all passing).
- [x] All frontend gates green: `format:check`, `lint`, `typecheck`, `test`,
      `build`, and `npm run e2e` (13 → 15 tests).

## Required tests

| Test | Covers |
| --- | --- |
| `persistence.test.ts` (19) | round trip; identifiers-only; validation of a corrupt or foreign payload; storage absent, throwing on every method, and throwing on property access |
| `SessionGate.test.tsx` (17) | restoring running / completed / upload-only sessions; fetching rather than trusting; every failure path (404 job, 404 audio, unreachable backend, a backend that never answers); the persist loop; `restoredPhase` |
| `JobEventBridge.test.tsx` (+3) | resync when a job is tracked while the socket is open; no refetch when an event updates the same job; an answer about a different job is ignored |
| `App.test.tsx` (+1) | the wiring: a stored snapshot restores the `separate` phase and the drop zone is never live in between |
| `e2e/resync.spec.ts` (+2, 1 rewritten) | reload mid-run resumes and reaches `completed` in the UI without creating a second job; reload after completion returns to `inspect`; an unknown stored id is fetched, refused, and started over cleanly |

## Notes / decisions

### Identifiers, never records

The store holds three strings. That is the whole design, and it is a direct
consequence of what features 017 and 031 cost: a `Job` record that is not the
newest one loses to the event stream, and when it wins anyway the UI is
stranded in a state no further event will correct. A record cached in
`sessionStorage` is the worst version of that — arbitrarily old, and applied
at the exact moment the app has no events yet to argue with it.

An *id* cannot be stale in that way. It is either still known to the backend
or it is not, and both answers are actionable. So rehydration fetches:
`GET /jobs/{id}` for the job, `GET /audio/{id}` for the upload. REST is the
source of truth (ARCHITECTURE.md §4/§11) and rehydration is simply the first
read of it.

The e2e suite pins this: it reads the stored payload out of the page and
asserts its keys are exactly `audioId`, `jobId`, `phase`, so a future change
that starts caching a record fails a test rather than a user.

### Why the phase is stored too

Two of the three fields are recoverable from the records; the phase is not.
A completed job is equally consistent with the user reading the result summary
in `separate` and with the user listening to stems in `inspect`, and nothing in
the record distinguishes them. So the phase is stored — as a *preference*.

`restoredPhase()` gives the records the final say:

- no job, no upload → `select`; no job but the upload survived → `configure`;
- a job still running → `separate` whatever was stored (there is nothing to
  inspect until it finishes);
- a completed job → `inspect` only if that is where the user was, otherwise
  `separate`;
- a cancelled or failed job → `separate`, the phase that renders a terminal
  job and offers the route out of it.

That ordering is what makes a stale snapshot harmless: the stored phase can
never land the user on a phase whose data is gone, which would be exactly the
dead end feature 030's suite was built to catch.

`export` maps onto `inspect`, because `Workspace` renders the export panel
*inside* `inspect` and has no `export` branch at all. Restoring `export`
literally would render an empty workspace.

### `appState.tsx` was not touched

Feature 035 owns it. It turned out not to need changing: `upload/succeeded`
already carries an `AudioFile` and `phase/set` already sets any phase, so
rehydration is expressible as three existing actions dispatched in order —
`upload/succeeded` (which moves `select` to `configure` on its own),
`job/track`, then `phase/set`, which has the final say precisely because it
goes last.

### The gate

`SessionGate` holds the workspace back while rehydration is in flight. Not
cosmetic: without it a reload paints `select` first, and the drop zone is live
for that moment — a file dropped into it would be silently overwritten by the
restore landing a beat later.

With nothing stored the gate settles **synchronously on its first render** and
issues no requests, so an ordinary first visit is byte-for-byte the app that
shipped before this feature. `App.test.tsx` asserts the "Select" phase never
appears on a restored session, and the gate's own test asserts the no-storage
path is synchronous.

### Every failure is honest, and none of them wedges

Job and audio records live in memory in the backend, so a stored id is
routinely stale — after a restart it always is.

- **404** (`job_not_found`, `audio_not_found`) — the id is dropped and the
  workflow starts where the remaining evidence supports. No error is shown:
  an id the user never saw, naming a job the backend has never heard of, is
  not something they can act on.
- **Network failure** — same treatment. `fetch` rejects promptly, and the
  restore settles.
- **A backend that accepts and never answers** — `RESTORE_TIMEOUT_MS`
  (10 s) abandons the restore, clears the snapshot and settles the gate; a
  late answer is then ignored rather than dropped onto a workflow the user
  has since started using. This is the one timer in the feature, and it
  exists because "do not wedge" cannot be built out of promises that may
  never resolve. It is in application code, not in a test — the e2e suite
  still has no fixed sleeps.
- **A partial restore** — a live job whose upload is gone still restores to
  `separate`; the run is what matters and it is still watchable.
- **Storage unavailable or throwing** — every read and write is wrapped,
  including the `globalThis.sessionStorage` property access itself, which
  throws (not returns `undefined`) in browsers configured to block site data.
  The app then behaves exactly as it did before this feature: it simply does
  not survive a reload.
- **A corrupt or foreign payload** — validated field by field on read. A
  string that is not JSON, a JSON value that is not an object, fields of the
  wrong type, and a `phase` that is not a member of `WORKFLOW_PHASES` are all
  dropped rather than trusted.

### The resync window rehydration opened, and how `JobEventBridge` closes it

Feature 017's bridge refetched the tracked job on every socket `open`. A
restored job breaks the assumption underneath that: it becomes tracked some
time *after* the socket opened, so the open-time resync ran with nothing to
fetch.

That leaves a window. Events are filtered by the tracked job id, so anything
arriving before `job/track` is discarded — and if the job reaches a terminal
state between the backend serving the restore's `GET /jobs/{id}` and the store
applying that answer, the terminal event is dropped and the snapshot that lands
says the job is still running. No further event will ever come. It is the
stranded UI of 017 and 031, reached by a different road.

So the bridge's rule is now stated in terms of an invariant rather than a
callback: *while a job is tracked and the socket is open, that job's record has
been fetched at least once since the socket opened.* It is driven by a
`useEffect` on `[connection, jobId]`, which covers both halves — the socket
opening and the tracked job changing — with one mechanism. A `job` object that
changed without changing id (every progress event, several times a second) is
not a change here, so events never cause fetches and the architecture's "no
polling" rule (§4) is untouched. The extra fetch for a job this client just
created is redundant but harmless: `POST /jobs` answers with a record that is
by definition current, and `trackJob`'s terminal guard handles it either way.

While there, the "is this answer still relevant" guard was tightened from
"is this the id I asked for" to "is this the id I am tracking", read off the
returned record. It is the same question asked of the authoritative field.

### Rehydration and the terminal-state rule

Rehydration goes through `job/track` like every other REST snapshot, so it
inherits feature 031's guard rather than bypassing it — the reason that rule
lives in the reducer. In practice nothing is tracked when a restore lands, so
there is nothing to rewind; what the guard protects against is the *next*
snapshot, including the confirming resync described above.

A job that finished while the page was closed needs nothing special: the
backend answers `completed`, with its result, and that is what is tracked.
No cached record could have known it.

### Session scope, deliberately

`sessionStorage`, not `localStorage`, and no cross-tab coordination. The hub
broadcasts every event to every connected client, and feature 017 deliberately
removed `job_created` adoption so a second tab could never take over the first
one's job. Sharing the tracked id across tabs through `localStorage` would
reintroduce exactly that coupling by another route. Per-tab scope keeps each
tab's workflow its own, and is also what makes the e2e tests deterministic:
Playwright gives every test a fresh context.

### Mutation-tested

The claim "the session survives a reload" was checked by removing the
capability — `readSessionSnapshot` returning the empty snapshot — and
re-running the tier. *Reloading mid-run returns to the running job* and
*reloading after completion returns to the results* both failed; nothing else
did. The stale-id test passed under the mutation, which is why it now also
asserts that the app **fetched** the planted id and was answered `404`: without
that, it would pass just as well against an app that never reads storage at
all. The mutation was reverted.

### Cost

`npm test`: 517 → 553 tests, still ~11 s. `npm run e2e`: 13 → 15 tests, ~35 s →
~50 s locally (the mid-run reload test uses the 60 s fixture, for the same
reason the cancel spec does — the reload has to land while the run is still
going).
