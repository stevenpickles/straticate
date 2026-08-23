# [016] Frontend job + WebSocket clients

Branch: `016-job-ws-clients`
Status: PR OPEN
Dependencies: 003, 005
PR: #14

## Objective

The typed client layer for jobs: REST calls to create, list, fetch, and
cancel jobs, plus a resilient WebSocket client that decodes the documented
event union and feeds a job store — the foundation the progress UI (017) and
the telemetry panel (020) build on.

## Scope

- `src/api/jobs.ts` — `createJob`, `listJobs`, `getJob`, `cancelJob` over the
  existing `get`/`post` helpers (which already prefix `/api/v1`), typed with
  the generated contract types and rejecting with the shared `ApiError`.
- `src/ws/client.ts` — `JobEventClient`: same-origin socket, typed event
  decoding, subscriptions, connection status, automatic reconnect with
  exponential backoff and jitter, injectable socket factory and timer.
- `src/ws/useJobEvents.ts` — React hook that connects on mount, disconnects
  on unmount, and dispatches events and status changes into the job store.
- `src/state/jobState.tsx` — new context + reducer holding the tracked job,
  chunk-grained progress, the newest runtime metrics payload, and the
  connection status.
- `src/test/mockWebSocket.ts` — scriptable socket and timer doubles.
- `App.tsx` — wraps the tree in `JobStateProvider` (state only; no socket).

## Out of scope

- Progress UI, cancel button, error surfaces (017); telemetry panel (020);
  stem player; `Workspace.tsx`, `DropZone.tsx`, metadata components (009).
- Backend job endpoints (015) and the WebSocket hub (013). This feature is
  built strictly against `docs/contracts/rest-api.md`,
  `docs/contracts/websocket-events.md`, and the generated types, with the
  network mocked in tests.

## Expected modules/files

- `frontend/src/api/jobs.ts` (+ `jobs.test.ts`)
- `frontend/src/ws/client.ts` (+ `client.test.ts`)
- `frontend/src/ws/useJobEvents.ts` (+ `useJobEvents.test.tsx`)
- `frontend/src/state/jobState.tsx` (+ `jobState.test.tsx`)
- `frontend/src/test/mockWebSocket.ts`, `frontend/src/test/fixtures.ts`
- `frontend/src/App.tsx`, `docs/features/016-job-ws-clients.md`, `ROADMAP.md`

## Acceptance criteria

- [x] `createJob`/`listJobs`/`getJob`/`cancelJob` hit the documented paths and
      methods and parse the documented payloads.
- [x] Backend error envelopes become typed `ApiError`s with `status`, `code`,
      `message`, and `detail`.
- [x] The WebSocket client connects to `/api/v1/ws` on the page's own origin,
      deriving `ws:`/`wss:` from `location.protocol` so the Vite dev proxy
      (`ws: true`) forwards it.
- [x] Every documented event type decodes into the generated
      `WebSocketEvent` union.
- [x] Unknown `type` values are ignored, not thrown; malformed JSON is logged
      and ignored and the socket stays usable.
- [x] Unexpected closes reconnect with exponential backoff plus jitter;
      `close()` is intentional and never reconnects.
- [x] The connection status is observable and renderable.
- [x] The reducer handles every event type and ignores events for any job
      other than the tracked one.
- [x] The hook subscribes on mount and unsubscribes on unmount.
- [x] `npm run format:check`, `lint`, `typecheck`, `test`, `build` all pass.

## Required tests

- REST: path, method, and body per function; envelope → `ApiError`;
  percent-encoded job IDs.
- WS client: decodes the exact JSON from the event contract for all eight
  types; ignores unknown types, malformed JSON, and non-object payloads;
  backoff sequence, cap, jitter, reset after a successful reconnect; no
  reconnect after `close()`; status transition sequence.
- Reducer: every event type; stale/foreign `job_id` ignored; terminal events;
  status changes.
- Hook: connects on mount, closes on unmount, feeds the store, stops after
  unmount.

## Notes / decisions

### Reconnect policy

`JobEventClient` reconnects only after an **unexpected** close (or a socket
that could not be created). Attempt *n* (zero-based) waits:

```text
delay = min(500 ms * 2^n, 8000 ms) - jitter,   jitter ∈ [0, 25 % of delay)
```

so the un-jittered sequence is 500, 1000, 2000, 4000, 8000, 8000, … ms. The
attempt counter resets to zero as soon as a socket opens. `close()` cancels
any pending reconnect, stops the sequence, and settles the status at
`closed`; a later `connect()` restarts the client with a fresh sequence.
`initialReconnectDelayMs`, `maxReconnectDelayMs`, `jitterRatio`, `random`,
and `schedule` are all injectable, so tests are deterministic without real
timers.

### Unknown-event rule

`decodeEvent` returns `null` — it never throws — for payloads that are not
strings, not JSON, not objects, carry no `type`, or carry a `type` outside
`KNOWN_EVENT_TYPES`. The client logs a warning and drops the message. A newer
backend emitting additional event types therefore cannot break an older
frontend, as `docs/contracts/websocket-events.md` requires.

### What 017 and 020 consume

```ts
import { createJob, cancelJob, getJob, listJobs } from '../api/jobs'
import { useJobEvents } from '../ws/useJobEvents'
import {
  useJobState,
  useJobDispatch,
  isTerminalJobState,
} from '../state/jobState'
```

`useJobState()` returns:

| Field             | Type                        | Meaning |
| ----------------- | --------------------------- | ------- |
| `job`             | `Job \| null`               | Best-known record of the tracked job: the REST response, kept current by events. `job.state` is the live lifecycle state, `job.result` the terminal result, `job.error` the terminal failure. |
| `progress`        | `JobProgressDetail \| null` | Newest `job_progress` detail: `progress`, `chunksCompleted`, `chunksTotal`, `elapsedSeconds`, `audioProcessedSeconds`, `audioTotalSeconds` (camelCase). |
| `metrics`         | `RuntimeMetricsEvent \| null` | Newest `runtime_metrics` event, stored **verbatim** — feature 020 renders `metrics.model`, `metrics.gpu` (null on CPU), `metrics.processing`. |
| `cancelledAtStage`| `JobState \| null`          | Stage the job was in when `job_cancelled` arrived. |
| `connection`      | `ConnectionStatus`          | `closed \| connecting \| open \| reconnecting`. |

Dispatch actions: `{ type: 'job/track', job }` after any authoritative REST
response, `{ type: 'job/clear' }` to stop tracking, plus `ws/event` and
`ws/status`, which the hook dispatches for you.

Notes for consumers:

- **Track before you listen.** The store follows one job. Dispatch
  `job/track` with the `Job` returned by `createJob`/`getJob`; events for any
  other `job_id` are ignored (the hub broadcasts to every client). The only
  exception: a `job_created` event is adopted when nothing is tracked yet.
- **The provider does not open the socket.** `App.tsx` mounts
  `JobStateProvider`; the component that needs live events calls
  `useJobEvents()`. It connects on mount and closes on unmount, so mount it
  once, at the level that owns the separation phase.
- **Refresh over REST on reconnect.** Events are notifications, not the
  database. Pass `useJobEvents({ onOpen })` and refetch `getJob(id)` there —
  it fires on first connect and after every reconnect.
- `cancelJob` returns the job as the backend sees it *now*; the authoritative
  transition to `cancelled` arrives as a `job_cancelled` event.

### Known limitations

- `listJobs()` is typed as `Job[]`, following the bare-array convention of
  the other collection endpoints in `docs/contracts/rest-api.md`. The job
  routes are not in the generated OpenAPI document yet (they land with 015);
  if 015 wraps the list in an envelope, this one type alias changes.
- The store tracks a single job, matching the current product workflow. A
  queue view would need a keyed collection.
- Reconnect refresh is a hook option rather than automatic, because the
  client layer does not know which job the UI cares about.
