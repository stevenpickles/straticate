# [012] Job manager (queue, states, cancellation)

Branch: `012-job-manager`
Status: PR OPEN
Dependencies: 005
PR: #8

## Objective

The asynchronous job engine exists: an in-process `JobManager` with a FIFO
queue, a single active job, validated state-machine transitions, cooperative
cancellation, progress plumbing, and a typed event-listener hook. Pure backend
infrastructure — no API endpoints (those are feature 015; the WebSocket hub is
feature 013; the separation engine is feature 014).

## Scope

- `backend/src/straticate/jobs/state.py` — transition rules for `JobState`:
  forward-only along `queued → preparing → decoding → loading_model →
  separating → post_processing → encoding → completed` (skipping stages
  forward is allowed), any non-terminal → `cancelled`/`failed`, terminal
  states allow nothing. `assert_transition(old, new)` raises
  `InvalidJobTransition` (a programming error, deliberately not an
  `ApplicationError`).
- `backend/src/straticate/jobs/cancellation.py` — `CancellationToken`
  (`cancel()`, `is_cancelled`, `raise_if_cancelled()` raising `JobCancelled`).
- `backend/src/straticate/jobs/manager.py` — `JobManager`, `JobExecutor`
  protocol, `JobContext`, typed listener hook, progress throttling (≤ 4 Hz per
  job), `get_job_manager` FastAPI dependency.
- Wiring: the FastAPI lifespan creates a **fresh** `JobManager` per lifespan
  cycle (a closed manager cannot be restarted), stores it on
  `app.state.job_manager`, starts it, and closes it on shutdown (`start()` /
  `aclose()`).
- Job IDs are ULID strings via `python-ulid` (already a backend dependency
  since feature 006, which uses it for audio IDs — not new to this feature).

## Out of scope

- REST job endpoints (015), WebSocket transport (013), `Separator` interface /
  `FakeSeparator` (014), runtime telemetry / `RuntimeMetricsEvent` (019),
  device selection, model catalog/resolution (010).

## Expected modules/files

- `backend/src/straticate/jobs/__init__.py` (public surface)
- `backend/src/straticate/jobs/state.py`
- `backend/src/straticate/jobs/cancellation.py`
- `backend/src/straticate/jobs/manager.py`
- `backend/src/straticate/main.py` (lifespan wiring)
- `backend/tests/test_jobs_state.py` · `test_jobs_cancellation.py` ·
  `test_jobs_manager.py`

## Acceptance criteria

- [x] `submit()` creates a `queued` ULID-identified job, enqueues it, and
      returns immediately; `job_created` is emitted.
- [x] Single worker task; strict FIFO; exactly one active job at a time.
- [x] All state changes validated by `assert_transition`; backward/terminal
      violations raise `InvalidJobTransition`.
- [x] Cancellation: queued job → immediately `cancelled` (never runs); running
      job → cooperative via token; unknown id →
      `ApplicationError("job_not_found", 404)`.
- [x] Executor outcomes: `JobCancelled` → `cancelled`; `ApplicationError` →
      `failed` preserving its code; other exceptions → `failed` with code
      `separation_failed`; success → `completed`, result attached,
      `progress = 1.0`.
- [x] Timestamps: `created_at` on submit, `started_at` on leaving the queue,
      `finished_at` on terminal state — all timezone-aware UTC.
- [x] Listeners receive the typed event models from
      `straticate.schemas.events`; sync and coroutine listeners supported;
      listener errors are logged and never break processing.
- [x] `job_progress` events throttled to ≤ 4 Hz per job; a report with
      `progress ≥ 1.0` is always delivered.
- [x] Manager started/closed with the app lifecycle; `aclose()` with running
      and queued jobs shuts down cleanly.

## Required tests

Covered in `backend/tests/test_jobs_*.py` (all scripted executors are gated by
`asyncio.Event` — no sleeps as synchronization): submit snapshot/timestamps;
FIFO with one active job; happy-path event order and payloads; cancel of a
queued job (never runs); cancel of a running job via token; executor exception
→ `failed` with `ErrorInfo`; `ApplicationError` code preserved; backward
transition raises `InvalidJobTransition`; raising listener doesn't break
processing; coroutine listeners; progress throttling with guaranteed final
delivery; `aclose()` with pending jobs; lifespan wiring + `get_job_manager`.

Added after code review: worker resilience (non-`SeparationResult` return,
executor-internal `CancelledError`, an error inside the manager's own terminal
marking — each fails the job and the queue keeps running); a listener removing
itself mid-dispatch does not skip later listeners; a gated coroutine listener
still observes events in strict emission order; `NaN` progress is ignored and
negative audio seconds clamp to `0.0`; the manager moves a starting job to
`preparing` and `set_stage` to the current stage is a no-op; `cancel()` after
`aclose()` raises `RuntimeError` while `get()` still works; two sequential
`TestClient` lifespans on one app; `ApplicationError.to_error_info()`.

## Interfaces for downstream features

These contracts are what 013/014/015 build against; import everything from
`straticate.jobs`.

### `JobExecutor` (the seam for feature 014)

```python
class JobExecutor(Protocol):
    async def __call__(self, job: Job, context: JobContext) -> SeparationResult: ...
```

- Awaited on the event loop by the manager's worker — long-running compute
  must be offloaded by the executor itself (worker thread etc.).
- `job` is a deep-copy **snapshot**; the `context` is the only mutation
  channel.
- On success return a `SeparationResult`; raise `JobCancelled` when observing
  cancellation; raise `ApplicationError` for expected failures (its `code`,
  `message`, `detail` are preserved in the job's `ErrorInfo` and the
  `job_failed` event); any other exception maps to code `separation_failed`.
- Returning anything that is not a `SeparationResult` is a protocol violation:
  the job fails with code `separation_failed` (the worker survives).
- An executor-internal `asyncio.CancelledError` (i.e. not caused by manager
  shutdown) also fails the job with `separation_failed`; only during
  `aclose()` does `CancelledError` mean shutdown (job ends `cancelled`).
- If the executor returns normally although cancellation had been requested,
  the cancellation wins: the job ends `cancelled` and the result is discarded.

### `JobContext` (constructed by the manager, handed to the executor)

- `context.cancellation: CancellationToken` — check `is_cancelled` or call
  `raise_if_cancelled()` between chunks (safe to read from worker threads).
- `context.set_stage(stage: JobState)` — forward-only processing stages
  (skipping is fine, terminal states are rejected); updates the job record and
  emits `job_stage_changed`. Setting the stage the job is already in is a
  **no-op** (no duplicate event) — the manager itself moves the job
  `queued → preparing` before the executor runs, so an initial
  `set_stage(PREPARING)` does nothing. Must be called on the manager's event
  loop.
- `context.report_progress(progress, chunks_completed, chunks_total,
  audio_processed_seconds=None, audio_total_seconds=None)` — updates
  `job.progress` immediately; the `job_progress` event is throttled to one per
  0.25 s per job, except `progress ≥ 1.0` which always emits. Inputs are
  sanitized before the record is touched: a `NaN` `progress` makes the whole
  report a logged no-op; `progress` is clamped to `[0, 1]`; unknown, negative,
  or `NaN` audio seconds are reported as `0.0`. Must be called on the
  manager's event loop.

### Listener contract (the hook for feature 013)

- `manager.add_listener(listener)` / `manager.remove_listener(listener)`.
- `JobEventListener = Callable[[JobEvent], Awaitable[None] | None]` — plain
  callables or coroutine functions.
- `JobEvent` is the union of `JobCreatedEvent | JobStartedEvent |
  JobStageChangedEvent | JobProgressEvent | JobCompletedEvent |
  JobCancelledEvent | JobFailedEvent` from `straticate.schemas.events`,
  constructed per `docs/contracts/websocket-events.md`. (`runtime_metrics` is
  produced by the telemetry sampler, feature 019 — not by the job manager.)
- Delivery is **strictly ordered**: a single dispatcher task consumes an
  internal FIFO event queue; for each event it calls every listener in
  registration order and awaits coroutine listeners before touching the next
  event (a client can therefore never observe `job_completed` before the
  final `job_progress`). A raising listener is logged and never affects job
  processing or the other listeners; a listener may remove itself during
  dispatch without skipping the remaining listeners. `aclose()` drains the
  queue before returning.

### Manager API (for feature 015)

- `submit(configuration, executor, *, model_id="") -> Job` — `model_id` is
  the resolved model for the job record; resolution belongs to the caller
  (catalog, feature 010/015).
- `get(job_id) -> Job` · `list_jobs() -> list[Job]` (submission order) ·
  `cancel(job_id) -> Job` (idempotent on terminal jobs; `job_not_found` /
  404 for unknown ids).
- After `aclose()`: `get()`/`list_jobs()` remain available (read-only);
  `submit()` and `cancel()` raise `RuntimeError` — there is no worker or
  dispatcher left to act on them.
- All returned `Job` objects are deep-copy snapshots — re-read with `get()`
  or subscribe with a listener for updates.
- Dependency accessor: `get_job_manager(request)` for use with `Depends`;
  the instance is created, started, and closed by the application lifespan
  (a fresh instance per lifespan cycle) and lives on `app.state.job_manager`.
- **Feature 015 note:** endpoint handlers using the manager must be
  `async def` so they run on the manager's event loop — sync handlers execute
  in the threadpool, off-loop. (Dispatch has a `call_soon_threadsafe` safety
  net for off-loop emission, but the manager's mutating API is
  single-loop by contract.)

## Notes / decisions

- **Concurrency contract:** the manager is single-event-loop, in-process
  state. All manager/`JobContext` calls happen on the app's event loop; only
  `CancellationToken` is safe to touch from worker threads.
- **State authority:** the manager owns the `queued → preparing` transition —
  a job leaves the queue as `preparing` (with `started_at` set and
  `job_started` + `job_stage_changed` emitted) *before* its executor runs.
  `Job.state` is therefore the single predicate for "is this job running":
  `cancel()` cancels a `queued` job immediately and uses the cooperative
  token for anything past `queued`; there is no shadow "running" flag.
- **Worker resilience:** no single job can kill the worker or stall the
  queue. A protocol-violating executor return value, an executor-internal
  `CancelledError`, or even an error in the manager's own terminal-marking
  marks the job `failed` (`separation_failed`) and the loop continues.
- **Shutdown semantics:** `aclose()` cancels the worker; a job running at
  shutdown is marked `cancelled` (it receives `asyncio.CancelledError`);
  still-queued jobs simply remain `queued` — nothing survives the process.
  The event queue is drained before `aclose()` returns.
- `report_progress` accepts an extra optional `audio_total_seconds` keyword
  (beyond the originally sketched signature) so executors can fill the
  contract-required `audio_total_seconds` field of `job_progress` events.
- ARCHITECTURE.md §6 was checked against the implementation and matches; no
  correction needed (inference off-loading to a worker thread is the
  executor's/separator's responsibility, per §6 "Scheduling").
