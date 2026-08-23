# [013] WebSocket event hub

Branch: `013-websocket-hub`
Status: PR OPEN
Dependencies: 012
PR: #13

## Objective

`WS /api/v1/ws` exists: connected browsers receive the job manager's typed
events as JSON, pushed in order, with connection lifecycle handling that
tolerates slow, silent, and dying clients. This is the transport that makes
real-time progress (017) and telemetry (019) possible.

## Scope

- `backend/src/straticate/jobs/hub.py` — `EventHub`:
  - `register(websocket)` / `unregister(websocket)` manage the connected set;
    `connection_count` / `is_closed` expose hub state.
  - `publish(event)` is the single listener registered with
    `JobManager.add_listener`. It is **synchronous**, serializes the event
    exactly once (`json.dumps(event.model_dump(mode="json"))`) and hands the
    resulting frame to every client's bounded outbound `asyncio.Queue`. It
    never touches a socket.
  - One dedicated sender task per client performs the socket writes, so a slow
    client can never delay the manager's ordered dispatcher, the other
    clients, or the job pipeline.
  - `EventSocket` — the structural protocol (`send_text` / `close`) the hub
    consumes, so the hub is unit-testable with doubles and never depends on
    Starlette internals.
  - `aclose()` drains what is still buffered (bounded, best effort) and then
    closes every connection with code `1001` (*going away*).
  - `get_event_hub(connection)` — `Depends`-style accessor reading
    `app.state.event_hub`, typed on `HTTPConnection` so both WebSocket and
    (future) HTTP endpoints can use it.
- `backend/src/straticate/api/ws.py` — the endpoint: `@router.websocket("/ws")`
  accepts, registers, then drains inbound messages until the client
  disconnects or the server closes the socket; `finally` always unregisters.
- `backend/src/straticate/main.py` — `lifespan` creates a fresh `EventHub`
  alongside the fresh `JobManager`, registers `hub.publish` as a listener,
  stores the hub on `app.state.event_hub`, and on shutdown closes the manager
  first (so its event queue drains into the hub) and the hub second — the hub
  in a `finally`, so it is closed even if the manager's teardown raises. The
  `ws` router is registered under `API_PREFIX`.
- `backend/src/straticate/jobs/__init__.py` — `EventHub`, `EventSocket`,
  `get_event_hub`, `DEFAULT_CLIENT_QUEUE_SIZE`, `EVICTABLE_EVENT_TYPES` added
  to the public surface.

## Out of scope

- `runtime_metrics` production (019 — the hub already forwards
  `RuntimeMetricsEvent` with no special-casing), job REST endpoints (015),
  `Separator` / `FakeSeparator` (014), any frontend code (016/017), per-job
  subscription filtering.
- No schema changes: `backend/src/straticate/schemas/events.py` is untouched;
  events are dumped from the Pydantic models, never hand-built.

## Expected modules/files

- `backend/src/straticate/jobs/hub.py`
- `backend/src/straticate/api/ws.py`
- `backend/src/straticate/main.py` (lifespan + router wiring)
- `backend/src/straticate/jobs/__init__.py` (public surface)
- `backend/tests/test_jobs_hub.py` · `backend/tests/test_api_ws.py`
- `docs/contracts/websocket-events.md` (connection-lifecycle section added)

## Acceptance criteria

- [x] `WS /api/v1/ws` accepts connections and pushes every job event as JSON.
- [x] Each event is serialized once and fanned out to all connected clients.
- [x] The hub's listener never awaits a socket, so the manager's ordered
      dispatcher is never stalled by a slow client.
- [x] Per-client bounded outbound queue with a documented overflow policy;
      terminal events are never silently dropped.
- [x] A send error affects only the failing client; it is unregistered and its
      socket closed while the others keep receiving.
- [x] Inbound client payloads (text, JSON, bytes, unknown) are discarded and
      never break the connection.
- [x] Clean teardown on `WebSocketDisconnect` and on server shutdown.
- [x] Serialization matches `docs/contracts/websocket-events.md` exactly.
- [x] Hub created/wired/closed by the lifespan; fresh instance per cycle;
      exposed on `app.state.event_hub` with a `Depends` accessor.

## Required tests

`backend/tests/test_jobs_hub.py` (unit, fake sockets; every slow-client
scenario gated by `asyncio.Event` — no sleeps as synchronization): fan-out to
all clients with identical bytes; exact JSON shape for `job_progress`,
`job_completed`, `job_stage_changed`, and timestamp serialization;
`runtime_metrics` forwarded unchanged; per-client delivery order; a raising
socket is unregistered (closed `1011`) while peers keep receiving;
`unregister` idempotent and delivery-stopping; overflow drops the oldest
`job_progress`; overflow keeps a terminal event by evicting progress instead;
overflow behind an undroppable *head* still evicts a progress sample from
further back and keeps the client; overflow with a wholly undroppable buffer
disconnects (`1013`); a wedged client delays no one; `aclose` closes all
connections (`1001`), is idempotent, disables publishing and registration,
gives up on a wedged client after `drain_timeout`, and survives a socket that
raises on `close`; plus three integration tests driving a real `JobManager`
(full event sequence, two clients, and a wedged client not stalling the
pipeline) and two lifespan tests (a socket whose write costs more than one
event-loop hop still receives the shutdown `job_cancelled` before its close
frame; a manager that raises from `aclose` still leaves the hub closed).

`backend/tests/test_api_ws.py` (endpoint, `TestClient`; manager calls
marshalled onto the app loop via `portal.call`): a connected client receives a
job's full event sequence in order with the documented field values; two
clients receive byte-identical streams; a client disconnecting mid-job breaks
neither the job nor its peer; inbound text/JSON/bytes are ignored; hub
shutdown closes the connection with `1001`; connecting *after* hub shutdown is
closed cleanly with `1001` rather than raising; application shutdown leaves no
registered clients; each lifespan cycle gets a fresh hub wired to its manager.

## Notes / decisions

### Backpressure / overflow policy

Each client gets an outbound `asyncio.Queue` of `DEFAULT_CLIENT_QUEUE_SIZE`
(256) frames and a dedicated sender task. `publish` only ever calls
`put_nowait`. When the queue is full the hub looks for the **oldest droppable**
frame anywhere in the buffer:

- `job_progress` and `runtime_metrics` (`EVICTABLE_EVENT_TYPES`) are droppable.
  The oldest one still buffered — whatever its position in the queue — is
  dropped and the new frame takes its place; the surviving frames keep their
  relative order. Both are *periodic samples*: a newer one carries strictly
  fresher state, so dropping the stale one loses nothing a user can perceive
  (progress is redrawn at ≤ 4 Hz anyway).
- Only when the buffer holds **no** droppable frame at all is it saturated with
  state that cannot be reconstructed from a later event — `job_created` /
  `job_started` / `job_stage_changed` and the terminal `job_completed` /
  `job_cancelled` / `job_failed`. The client is then **disconnected** with
  close code `1013` (*try again later*).

Inspecting only the head frame would not do: a buffer headed by `job_created`
and otherwise full of progress samples would be misread as undroppable, killing
a client the policy is meant to keep alive. `asyncio.Queue` offers no removal,
so the eviction empties and refills the queue through the non-blocking API —
`O(client_queue_size)` (≤ 256 list operations, no awaits) and only ever paid
for an already-saturated client. `publish` stays synchronous throughout, so no
producer or consumer can observe the queue mid-rebuild.

Rationale for disconnecting rather than blocking or truncating: blocking would
stall the manager's single ordered dispatcher and therefore the whole job
pipeline (012's listener contract), and silently dropping a terminal event
would leave a job stuck "running" in the UI forever. A disconnect is
recoverable — the documented client behaviour is to refetch jobs over REST on
(re)connect — while a lost terminal event is not. A terminal event is
therefore never dropped in silence: it is either delivered or the client is
closed loudly.

At 4 Hz progress, a 256-frame buffer covers roughly a minute of a wedged
client before any eviction happens at all; a client that far behind is not
usefully connected.

Close codes: `1001` server shutdown, `1011` send failure, `1013` client cannot
keep up.

### Shutdown drain

The lifespan closes the manager first so that it drains its event queue —
including the cancellation of a job that was still running — into the hub, and
only then closes the hub. For that ordering to actually deliver anything,
`aclose()` gives the sender tasks a bounded, best-effort window
(`SHUTDOWN_DRAIN_TIMEOUT_SECONDS`, 2.0 s, overridable per call) to write what
is still buffered before it cancels them and closes the sockets with `1001`.
Publishing is disabled first, so nothing new is buffered during the drain, and
the window is shared by all clients so one wedged client cannot hold up
shutdown; frames that miss it are dropped and the timeout is logged.

Without the drain the terminal event was lost on any socket whose write takes
more than one event-loop hop — that is, every real WebSocket. The senders now
mark each dequeued frame done, so the drain is an ordinary
`asyncio.Queue.join()` and waits for frames to reach the *socket*, not merely
to leave the queue.

The job manager listener is deliberately **not** removed before
`manager.aclose()`: the hub must still be subscribed while the manager drains,
or the shutdown cancellation would never reach the clients. It is removed
afterwards (and the hub is closed) in a `finally`, so a manager teardown
failure can neither leak a sender task per client nor leave sockets open.

### A connection racing shutdown

`register()` raises `RuntimeError` on a closed hub. The endpoint catches that
around the `register` call and closes the socket with `1001`, so a client that
connects while the server is shutting down sees the same "going away" it would
have seen a moment earlier. Letting the error escape would hand it to the
application-wide `Exception` handler, which can only produce a `JSONResponse`
— meaningless in a WebSocket scope.

### Broadcast to all

Straticate is a single-user local application (ARCHITECTURE.md §11), so there
is no per-job subscription filtering and no client → server protocol in v1:
every connected client receives every event. Inbound frames are read only to
detect disconnects and are discarded without parsing, which keeps the door
open for a future subscribe/filter protocol without changing the hub.

### Module placement

The hub lives in `straticate.jobs` (next to the `JobManager` whose listener
hook it implements) rather than in `straticate.api`, so that non-API producers
— feature 019's telemetry sampler — can publish without importing the API
layer. `straticate.api.ws` holds only the endpoint.

### What feature 019 must do to publish telemetry

The hub is transport-only and does **not** special-case event types. To emit
`runtime_metrics`:

1. Get the hub from `app.state.event_hub` (or `Depends(get_event_hub)`).
2. Build a `RuntimeMetricsEvent` from
   `straticate.schemas.events` and call `hub.publish(event)` — synchronous,
   non-blocking, must be called on the application's event loop (the sampler
   should be an asyncio task on that loop; sample off-loop if needed, publish
   on-loop).
3. Nothing else is required: `publish` accepts the whole `WebSocketEvent`
   union, `runtime_metrics` is already in `EVICTABLE_EVENT_TYPES` so a slow
   client sheds telemetry samples before anything that matters, and the
   contract's JSON shape comes straight from the model dump.
4. `hub.connection_count` is available if the sampler wants to skip work while
   nobody is listening.

### Contract documentation

`docs/contracts/websocket-events.md` gained a "Connection lifecycle" section
recording the close codes and the drop policy — behaviour feature 016's WS
client needs. No event payload changed; the Pydantic models remain the single
source of truth.

### Known limitations

- Frames buffered for a client are discarded when it unregisters, and at
  shutdown whatever the drain window does not cover is dropped too; there is no
  replay/resume. REST remains the source of truth for reconnect, exactly as the
  contract specifies.
- The `1013` disconnect path drops the frames still buffered for that client.
  This is intentional (the client resyncs over REST) but means an operator
  watching logs sees the warning, not the lost events.
