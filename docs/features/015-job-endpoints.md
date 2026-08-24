# [015] Job REST endpoints (create/list/get/cancel)

Branch: `015-job-endpoints`
Status: PR OPEN
Dependencies: 006, 010, 012, 014, 018
PR: #…

## Objective

The two halves of the application are joined. `POST /api/v1/jobs` resolves the
uploaded audio (006), the model behind the requested mode + quality tier (010),
the compute device (018) and a `Separator` for that model (014), builds a
`SeparatorJobExecutor` and submits it to the `JobManager` (012) — returning
immediately with a `queued` `Job` while events flow out through the `EventHub`
(013) to the already-built frontend clients (016). `GET /jobs`, `GET
/jobs/{job_id}` and `POST /jobs/{job_id}/cancel` complete the resource.

## Scope

- `backend/src/straticate/inference/registry.py` — `SeparatorRegistry`: the
  architecture-keyed seam turning a catalog `Model` into a `Separator`, plus
  `SeparatorBuilder`, `separator_info_from_model`, `fake_separator_builder`
  and `default_separator_builders`.
- `backend/src/straticate/jobs/resolution.py` — the pure resolvers
  `resolve_model` / `resolve_audio` / `resolve_device` (no FastAPI imports).
- `backend/src/straticate/api/jobs.py` — the `/jobs` router (four `async def`
  handlers) and the `get_separator_registry` dependency accessor.
- `backend/src/straticate/main.py` — the registry is constructed in
  `create_app()` onto `app.state.separator_registry`; the jobs router is
  registered under `API_PREFIX`. The lifespan block (012/013) is untouched.
- Public surfaces: `straticate/inference/__init__.py`,
  `straticate/jobs/__init__.py`.
- `docs/contracts/rest-api.md` — the "Jobs" section gained concrete status
  codes, the list ordering, the resolved-`device_id` rule and the error table.
- `frontend/src/api/generated/api.d.ts` — regenerated (it was missing
  `/api/v1/system/devices` from 018 as well as the new `/api/v1/jobs*` paths).

## Out of scope

- `backend/src/straticate/schemas/**` and `models/schemas/**` — every schema
  this feature needs already exists; none was changed.
- The job manager, event hub, `Separator`/`FakeSeparator`, the executor
  adapter, the model catalog service and device detection (012/013/014/010/018)
  are consumed, not modified.
- `GET /jobs/{id}/result`, stem streaming and export (021/022).
- The telemetry sampler and `runtime_metrics` (019).
- Any frontend source or test file other than the regenerated
  `api/generated/api.d.ts` (011/017/020 own the UI).
- Real ML models, PyTorch, model downloads.
- Job persistence across restarts; a job cleanup/retention policy.

## Expected modules/files

- `backend/src/straticate/api/jobs.py`
- `backend/src/straticate/inference/registry.py`
- `backend/src/straticate/jobs/resolution.py`
- `backend/src/straticate/{inference,jobs}/__init__.py`
- `backend/src/straticate/main.py`
- `backend/tests/test_api_jobs.py` · `test_inference_registry.py` ·
  `test_jobs_resolution.py`
- `frontend/src/api/generated/api.d.ts`
- `docs/contracts/rest-api.md` · `docs/features/015-job-endpoints.md` ·
  `ROADMAP.md`

## Acceptance criteria

- [x] `POST /api/v1/jobs` returns 201 with a `Job` in state `queued` whose
      `model_id` is the model resolved from `mode_id` + `quality_id` and whose
      `configuration.device_id` is the resolved device — and it returns
      **before** the separation runs (proved with a gated separator: the
      response arrives while the separator has not been entered).
- [x] A job created through the API runs to `completed` on the job manager with
      real stems under `{data_dir}/jobs/{job_id}/stems/`, and
      `GET /api/v1/jobs/{id}` then reports `completed`, `progress == 1.0` and a
      populated `result`.
- [x] A WebSocket client connected to `/api/v1/ws` observes the full event
      sequence for an API-created job (`job_created` → `job_started` →
      `job_stage_changed`… → `job_progress`… → `job_completed`).
- [x] `GET /api/v1/jobs` lists jobs in submission order; `GET
      /api/v1/jobs/{id}` 404s with `job_not_found` for an unknown ID.
- [x] `POST /api/v1/jobs/{id}/cancel` cancels a queued job immediately and
      requests cooperative cancellation of a running one; the job ends
      `cancelled`, a `job_cancelled` event is emitted, and cancelling an
      already-terminal job is idempotent (200, no error).
- [x] Every unresolvable reference produces the documented code/status in the
      standard error envelope: `audio_not_found`, `separation_mode_not_found`,
      `quality_option_not_found`, `device_not_found` (404) and
      `separator_unavailable` (501).
- [x] All four handlers are `async def`.
- [x] Nothing in the API layer names a model architecture, a stem, a mode or a
      device type: all of it comes from the catalog and the detector.
- [x] `frontend/src/api/generated/api.d.ts` is regenerated and includes the
      `/api/v1/jobs*` and `/api/v1/system/devices` paths.
- [x] Backend `ruff format --check` · `ruff check` · `pyright` (strict) ·
      `pytest` and frontend `format:check` · `lint` · `typecheck` · `test` ·
      `build` all green.

## Required tests

`backend/tests/test_jobs_resolution.py` — model resolution happy path (two- and
four-stem modes), unknown mode, unknown quality option, and a mode whose option
names a model the catalog does not contain; audio resolution happy path,
unknown ID, and a registered record whose file was deleted; device resolution
with an explicit ID, with `None`, and with an unknown ID.

`backend/tests/test_inference_registry.py` — a fake-architecture catalog model
yields a `FakeSeparator` whose `info` mirrors the catalog entry; a six-stem
catalog-only model resolves with no code change; repeated `get()` returns the
same instance and different models get different ones; an unregistered
architecture raises `separator_unavailable` (501) with the documented detail; a
custom builder map and `register()` are honoured; the default registry covers
exactly the fake architecture; the fake builder's tuning is passed through.

`backend/tests/test_api_jobs.py` — the real application with the lifespan
running on the test's own event loop (`app.router.lifespan_context`), a real
job manager, hub and `FakeSeparator` (delays zeroed via an injected
`SeparatorRegistry`), generated WAV fixtures, and every wait gated by an
`asyncio.Event`: create → 201 `queued` with the resolved model and device (both
pinned and omitted); "the handler returns before the separation runs"; the full
lifecycle to `completed` with stems on disk for **both** the two-stem and the
four-stem mode; list ordering; get + its 404; cancel of a queued job (never
started), of a running job (gated, `job_cancelled` emitted, ends `cancelled`)
and of a terminal job (idempotent); every documented error code with the exact
envelope shape; and one `TestClient` + `portal.call` test driving a real
WebSocket client through the whole REST → manager → hub → client path.

## Interfaces for downstream features

### Resolving a job's model and device (017, 019, 021)

A `Job` record carries everything needed to re-resolve what it ran with:

```python
model  = catalog.get_model(job.model_id)                     # feature 010
device = detector.get_device(job.configuration.device_id)    # feature 018
```

`job.configuration.device_id` is **always populated** — feature 015 submits a
copy of the request configuration with the resolved device ID substituted, so
019's telemetry sampler and the UI never have to re-run the default selection.
`job.model_id` is likewise always the resolved model, never empty, for jobs
created through the API.

The pure resolvers themselves are exported from `straticate.jobs`
(`resolve_model`, `resolve_audio`, `resolve_device`) if another feature needs
the same lookups with the same error codes.

### Where the stems are (021, 022)

`straticate.inference.layout` remains the single definition:
`job_stems_dir(settings.data_dir, job.id)` is the directory this feature tells
the executor to write into, and `stem_path(data_dir, job_id, stem)` names one
file. The stem *names* come from `job.result.stems` (or the model's `stems`),
never from a hardcoded list.

### Obtaining the registry, and how 026 registers a real builder

`SeparatorRegistry` lives on `app.state.separator_registry`, built in
`create_app()`; endpoints get it with `Depends(get_separator_registry)`
(`straticate.api.jobs`). It maps **architecture → builder**:

```python
SeparatorBuilder = Callable[[Model], Separator]
```

Feature 026 adds its architecture in exactly one place — either by extending
`default_separator_builders()` in `straticate/inference/registry.py` or by
calling `registry.register("mel_band_roformer", build_roformer)` — and nothing
else changes: not the API, not the resolvers, not the catalog service. Adding
another *fake* model to `models/catalog.json` needs no code change at all,
because the fake builder constructs its `SeparatorInfo` from the catalog entry
(`separator_info_from_model`), not from a constant.

`get(model)` caches one instance per model ID, lazily. That is safe because the
manager runs one job at a time (ARCHITECTURE.md §6) and a separator's own
contract is one separation at a time (014). A real builder may therefore load
weights eagerly in its constructor and rely on the instance being reused across
jobs. `FAKE_VOCALS_INFO` / `FAKE_STANDARD_INFO` still exist for 014's
catalog-consistency test, but are never consulted on the resolution path.

Tests inject a registry with fast builders:

```python
app.state.separator_registry = SeparatorRegistry(
    {FAKE_ARCHITECTURE: fake_separator_builder(chunk_delay_seconds=0.0, model_load_seconds=0.0)}
)
```

### Cancel semantics (017)

`POST /jobs/{id}/cancel` is a **request**, not a stop, and takes no body:

- a `queued` job is cancelled synchronously and comes back `state: "cancelled"`;
- a running job comes back in whatever processing state it is in — the UI must
  wait for the `job_cancelled` event (or re-`GET` the job) for the authoritative
  transition, and should render a "cancelling…" affordance meanwhile;
- cancelling a terminal job is a **no-op returning 200**. There is no conflict
  response and no "not cancellable" error.

## Notes / decisions

### Why the registry is keyed by architecture

Keying by model ID would mean editing Python every time the catalog gains an
entry, which is precisely the coupling ARCHITECTURE.md §1 forbids. Architecture
is the smallest key that identifies "code that can run this", and it is already
an open string set in the manifest. The API layer never sees it: it hands the
registry a `Model` and receives a `Separator`.

### Why the resolvers live in `straticate.jobs`, not `straticate.inference`

`straticate.inference` already imports `straticate.jobs` (the executor adapter
needs `JobContext`, the base protocol needs `CancellationToken`), so the
resolvers — which are pure and have nothing to do with inference — go in
`jobs/resolution.py` and the registry stays in `inference/`. That keeps the
import direction one-way. `straticate.api.jobs` is the only module that sees
both.

### Why the submitted configuration is a copy with the resolved device

`SeparationConfiguration.device_id` is optional in the *request* ("let the
backend pick"), but a job record with a null device is useless downstream: 019
would have to re-run the default selection to label its telemetry, and the UI
could not show what a job is running on. Substituting the resolved ID at submit
time makes `Job.configuration.device_id` a total function of the job, in
responses and in every event alike, at the cost of one `model_copy`.

### Why `service_unavailable` (503) rather than a 500

`JobManager.submit()`/`cancel()` raise `RuntimeError` once the manager is
closed, i.e. during and after shutdown. A request that lands in that window has
not hit a bug — the server is going away — so it is translated into
`ApplicationError("service_unavailable", …, status_code=503)`. Reads (`GET
/jobs`, `GET /jobs/{id}`) keep working, because the manager's read API remains
available after `aclose()`.

### Why a missing file is `audio_not_found` and not a new code

Audio records are in-memory while the files are on disk, so the two can drift
(a previous process's registry is gone; a file was removed underneath us).
From the client's point of view there is nothing to separate either way, and a
second code would only give every client a second branch to write. The
resolver therefore checks the file exists and reports the existing 006 code.

## Known limitations

- **Feature 016's `frontend/src/api/jobs.test.ts` mocks a
  `job_not_cancellable` (409) response that this backend never produces.**
  Cancellation is idempotent: a terminal job returns 200. Feature 017 must not
  build UI around that code — treat a cancel response as advisory and wait for
  `job_cancelled`. That test is out of scope here and was deliberately left
  untouched; it is a valid test of the *client's* error mapping, just not of a
  reachable server behaviour.
- The same file's `listJobs` TSDoc says "newest-first as ordered by the
  backend"; the backend orders oldest-first (submission order). The comment is
  frontend-owned and was not edited here — 017 should correct it, or sort
  client-side.
- Job records and audio records are in-memory only, so both lists are empty
  after a restart while the files they described are still on disk (orphans).
  A persistent registry and a retention policy are still unclaimed work.
- Separator instances are cached for the process lifetime and never evicted.
  With only the fake engine that is free; a real separator holding GPU weights
  will need an eviction policy (feature 026's problem, noted here so it is not
  forgotten).
- The `SeparatorRegistry` is created in `create_app()` and therefore shared
  across lifespan cycles of the same app object (unlike the manager and hub,
  which are per-cycle). It holds no per-run state, so this is intentional.
