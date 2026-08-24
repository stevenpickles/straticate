# [019] Runtime telemetry sampler + metrics events

Branch: `019-telemetry-sampler`
Status: PR OPEN
Dependencies: 013, 014, 015, 018
PR: #…

## Objective

While a separation job runs, the backend samples the active separator's runtime
statistics on an interval and publishes `runtime_metrics` WebSocket events, so
a browser can show live model / device / processing telemetry (feature 020
renders it). Nothing else in the system changes: the hub is transport, the
separator is the source of truth, and the sampler is the only thing that
connects them.

## Scope

- `backend/src/straticate/telemetry/` — a new package holding
  `TelemetrySampler`:
  - constructed with the `EventHub` and a sampling interval
    (`DEFAULT_SAMPLE_INTERVAL_SECONDS`, 1.0 s per ARCHITECTURE.md §12;
    injectable so tests use a tiny interval or an interval long enough that
    only the opening tick fires);
  - `register(job_id, separator)` — the create-job endpoint tells the sampler
    which separator will run a job. This is the only coupling; the sampler
    never imports the API layer;
  - `on_job_event(event)` — the `JobManager` listener: starts sampling on
    `job_started`, stops and drops the registration on any terminal event.
    Synchronous, non-blocking and never raising, per 012's listener contract;
  - `sample_once(job_id) -> RuntimeMetricsEvent | None` — builds the event from
    `separator.runtime_stats()` using the three documented projections
    (`stats.model.to_model_info()`, `stats.device.to_gpu_metrics()`,
    `stats.processing.to_processing_metrics()`) and nothing else; returns
    `None` for an unregistered job, absent stats, or a snapshot belonging to a
    different job;
  - `aclose()` — cancels the sampling task, drops every registration and makes
    further starts no-ops; idempotent;
  - `get_telemetry_sampler` — the `Depends` accessor reading
    `app.state.telemetry_sampler`, in the same style as `get_job_manager` /
    `get_event_hub` and living next to the class it serves.
- `backend/src/straticate/main.py` — the lifespan creates a fresh sampler after
  the hub and manager, registers its listener (after the hub's, so a terminal
  event reaches the wire before sampling stops), stores it on
  `app.state.telemetry_sampler`, and closes **sampler → manager → hub**.
- `backend/src/straticate/api/jobs.py` — `create_job` registers the separator
  with the sampler immediately after `manager.submit(...)` returns.
- `docs/contracts/websocket-events.md` — the sampling behaviour under
  `runtime_metrics` (interval, active-job-only, no sample before stats, the
  verbatim `gpu` block, the zero-connections skip). No payload changed.

## Out of scope

- Any schema change. `RuntimeMetricsEvent`, `ModelInfo`, `GpuMetrics` and
  `ProcessingMetrics` already existed (feature 005) and are untouched, so the
  OpenAPI document and `frontend/src/api/generated/api.d.ts` cannot drift.
- The event hub, job manager, separators, the executor adapter, the registry,
  the resolvers and the device detector — all consumed, none modified.
- NVML, real GPU sampling, PyTorch (026 — no dependency added); persisted
  telemetry history or a metrics REST endpoint; the telemetry panel (020) and
  any other frontend file.

## Expected modules/files

- `backend/src/straticate/telemetry/{__init__,sampler}.py` (new)
- `backend/src/straticate/main.py` (lifespan wiring only)
- `backend/src/straticate/api/jobs.py` (one registration call)
- `backend/tests/test_telemetry_sampler.py` (new)
- `docs/contracts/websocket-events.md` · `docs/features/019-telemetry-sampler.md` · `ROADMAP.md`

## Acceptance criteria

- [x] A job run through the real `JobManager` publishes `runtime_metrics`
      events whose JSON matches `docs/contracts/websocket-events.md` exactly.
- [x] Sampling starts when the job starts and stops at its terminal event; no
      `runtime_metrics` is published before a job starts or after it ends.
- [x] The `gpu` block is the separator's `DeviceStats` verbatim, and `null`
      when the separator reports no device.
- [x] No sample is published when `runtime_stats()` is `None` or reports a
      different `job_id`.
- [x] Nothing is published while `hub.connection_count == 0` — the tick does
      not even read the separator's snapshot.
- [x] A raising sample tick is logged and neither kills the sampler nor
      affects the job; the listener itself never raises.
- [x] Registrations are dropped on terminal events and by `aclose()` — no
      leak, including for a job cancelled while still queued.
- [x] The sampler is created, wired and closed by the lifespan, with a fresh
      instance per cycle; shutdown order is sampler → manager → hub.
- [x] `uv run ruff format --check .` · `ruff check .` · `pyright` (strict) ·
      `pytest` all green.

## Required tests

`backend/tests/test_telemetry_sampler.py`, in three tiers. Everything is
`asyncio.Event`-gated; no `sleep` is used as synchronization (see "Asserting
that nothing happens" below).

Unit (recording `EventHub` double + stub separator): event construction from a
snapshot through all three projections; the serialized shape against the
contract's field sets; `device=None` → `gpu: null`; no event for absent,
mismatched or unregistered stats; a non-positive interval rejected; nothing
published before `job_started`; start on `job_started`; stop and
drop-the-registration on each of `job_completed` / `job_cancelled` /
`job_failed`, including for a job that never started; a terminal event for a
*different* job leaving sampling alone; the zero-connections skip (asserting
the separator was never even polled); a raising tick swallowed with sampling
continuing; the listener swallowing an internal failure; the late-registration
fallback (and that it does not start for an already-finished job); `aclose()`
cancelling sampling and dropping registrations, being safe twice, and leaving a
sampler that starts nothing.

Wiring (`straticate.main.lifespan`): a fresh sampler per cycle, closed on exit;
the listener actually registered (a job started through the real manager is
sampled and reaches a connected socket); the `sampler → manager → hub` close
order; the hub still closed when the sampler's teardown raises.

End to end (real manager + real hub + real `FakeSeparator` + generated WAV
fixture, driven through `POST /api/v1/jobs` over `httpx.ASGITransport`): a
connected fake client receives at least one contract-shaped `runtime_metrics`
strictly between `job_started` and `job_completed` and none after it, carrying
the resolved model and the separator's own `backend: "fake"` device block; plus
the register-vs-start race, using a separator that has publishable statistics
from the instant its first stage begins and refuses to finish until the client
has seen a sample.

## Notes / decisions

### The separator is the authority on its own device stats (018's sketch is superseded)

`gpu` is `stats.device.to_gpu_metrics()` verbatim, and `null` when
`stats.device is None`. Nothing is substituted from `DeviceDetector`.

`docs/features/018-device-detection.md` §"What feature 019 (runtime telemetry)
should call" sketches the opposite — copy `id` / `name` / `backend` /
`memory_total_bytes` from the resolved `ComputeDevice` and emit `gpu: null` on
a CPU backend. **That sketch predates feature 014 and is superseded by this
feature; do not follow it.** Reasons:

- `FakeSeparator` reports an honestly-labelled `backend: "fake"` device profile
  (014), which is precisely what lets the whole telemetry path be demonstrated
  and tested on a GPU-free machine. This project is developed on one:
  `GET /system/devices` returns CPU only. Under 018's sketch the panel would
  always be empty here and feature 020 would have nothing to render.
- A real separator (026) reports its real CUDA identity through the same
  accessor and reports `device=None` on CPU, which still yields the contract's
  `gpu: null`. The sketch's behaviour therefore falls out of the separator's
  own report — there is nothing the device detector needs to contribute.
- Only the separator knows the *dynamic* half (`memory_allocated_bytes`,
  `memory_peak_bytes`, and the optional NVML `utilization` /
  `temperature_celsius`). Splitting a single JSON object across two authorities
  invites the identity fields and the memory fields to describe different
  devices; one authority per block cannot.

`straticate.system.devices` remains the authority on *static* device facts for
`GET /system/devices` and for job device resolution (015). It is simply not
consulted by telemetry.

### The register-vs-start race

The job ID only exists once `manager.submit(...)` returns, but by then the job
may already be running — so `register` cannot be keyed on it "before" submit,
and a naive `await` in between would let the manager's worker start the job and
emit `job_started` while the sampler still knows nothing.

Solved on both sides:

1. **Primary: an `await`-free window.** `create_job` calls
   `sampler.register(job.id, executor.separator)` *immediately* after `submit`
   returns, with no suspension point in between. `submit` only enqueues the job
   and its `job_created` event — the worker and the dispatcher are separate
   tasks on the same loop and cannot run until the handler yields — so the
   registration is always in place before `job_started` is even dispatched.
   Feature 015 already requires this handler to be `await`-free between
   resolution and submit for exactly this kind of atomicity, so the
   registration simply joins that block. The `RuntimeError → 503` translation
   around `submit` was narrowed to just the `submit` call so the registration
   sits outside the `try`, where its own failure would not be mis-reported as a
   shutdown.
2. **Belt and braces: the sampler tolerates a late registration.** A
   `job_started` for an unknown job is remembered
   (`_awaiting_registration`); if the registration arrives afterwards, sampling
   begins at once instead of the job losing its telemetry entirely. The memo is
   cleared by the job's terminal event, so a job that was never registered
   leaves nothing behind.

A test drives the real endpoint with a separator that has publishable
statistics from the instant its first stage begins and does not finish until
the connected client has seen a `runtime_metrics` — a sampler that missed the
start would hang the test rather than pass it by luck.

### Sampling on the loop, publishing without awaiting

`hub.publish` is synchronous and single-loop by contract (013), and
`runtime_stats()` is a cheap snapshot read, so the sampler is an ordinary
`asyncio.Task` on the application's loop and offloads nothing. The loop samples
**first and then sleeps**, so the opening sample of a job is taken as soon as it
starts (usually returning `None`, because the separator has not begun) rather
than one interval later.

Stopping is synchronous too: `_cancel_task()` calls `task.cancel()` without
awaiting, because it runs inside the job manager's dispatcher, which may not
block. The task can only ever be suspended at its interval sleep, so it is torn
down without taking another sample.

### Ordering of the two manager listeners

The lifespan registers `hub.publish` **before** `sampler.on_job_event`. Dispatch
calls listeners in registration order within one synchronous block, so a
terminal event is serialized into every client's outbound queue *before* the
sampler is asked to stop — and, because no task can run mid-dispatch, no sample
can slip in between. A client therefore never sees `runtime_metrics` after the
terminal event of the job it belongs to.

### Shutdown order: sampler → manager → hub

The sampler is closed first so that no sample can be published into a hub that
is about to tear its connections down. The manager is closed second, preserving
013's ordering: it drains its event queue (including the cancellation of a job
that was still running) into the hub, with the hub's listener still registered.
The hub is closed last, in a `finally`, so it is torn down even if closing the
sampler or the manager raises. The sampler's listener stays registered through
the manager drain — a closed sampler ignores events, so removing it earlier
would buy nothing and would complicate the existing `finally` structure.

### Only one job at a time, but several registrations

ARCHITECTURE.md §6 guarantees one active job, so there is at most one sampling
task; but several jobs can be *registered* while they queue. Registrations are
therefore a dict keyed by job ID, and every terminal event — including the
immediate cancellation of a job that never left the queue — drops its entry, as
does `aclose()`. `TelemetrySampler.registered_job_ids` exposes the set so a test
can assert the absence of a leak directly.

### Nobody listening → no work at all

A tick with `hub.connection_count == 0` returns before reading the separator's
snapshot: telemetry is a live readout with no history, so a sample nobody can
receive has no value. (`hub.publish` would discard it anyway; skipping earlier
also keeps a real separator's future NVML calls out of the idle path.)

### Asserting that nothing happens, without sleeping

Tests that must prove *nothing* was published cannot wait on an event. Instead
the sampler is given `IDLE_INTERVAL = 60 s`: because the loop samples before it
sleeps, a started job produces exactly one sample and any further sample would
be a minute away. A handful of `asyncio.sleep(0)` loop ticks then flushes
everything that *could* have run, and the assertion is deterministic rather
than timing-dependent. Tests that need repeated ticks use a 1 ms interval and
still gate on an `asyncio.Event`.

### Known limitations

- No telemetry history: samples are dropped if nobody is connected and are
  never replayed. A client connecting mid-job sees the next sample and nothing
  earlier. REST remains the source of truth for job state.
- Sampling is per active job, so a future multi-job scheduler would need more
  than one task (the structure already keys registrations by job ID).
- The numbers a `FakeSeparator` reports are fabricated (014); the honest
  `backend: "fake"` label is what keeps that visible in the UI.
- `docs/features/018-device-detection.md` still contains the superseded sketch
  described above; it was left untouched (it is another feature's document) and
  is corrected here instead.
