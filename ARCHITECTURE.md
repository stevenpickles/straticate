# Straticate Architecture

This document is the authoritative description of Straticate's system design.
Significant architectural changes must be reflected here in the same PR that
introduces them.

## 1. Guiding principle

**The machine-learning model is a replaceable inference backend.**

Straticate is never coupled to a specific network architecture (RoFormer, MDX,
MDXC, Demucs, …). Application code works exclusively in terms of:

- model IDs
- model capabilities
- separation modes
- stems
- jobs
- compute devices
- results

Model-specific details (segment size, overlap, FFT size, batch size) live
behind the separation-engine interface and, optionally, in per-model default
inference parameters in the model catalog.

## 2. System components

```text
┌────────────────────────────────────────┐
│                Browser                 │
│          React + TypeScript            │
│  upload · configure · progress ·       │
│  telemetry · stem player · export      │
└───────────────────┬────────────────────┘
                    │
             HTTP + WebSocket
                    │
┌───────────────────▼────────────────────┐
│               FastAPI                  │
│                                        │
│  API            REST resource ops      │
│  Job Manager    queue, states, cancel  │
│  Audio Mgmt     upload, metadata, tmp  │
│  Model Catalog  manifests, capabilities│
│  Telemetry      GPU/model/proc metrics │
└───────────────────┬────────────────────┘
                    │
┌───────────────────▼────────────────────┐
│          Separation Engine             │
│                                        │
│   Separator interface                  │
│     ├── FakeSeparator                  │
│     ├── RoFormerSeparator              │
│     ├── MDXSeparator                   │
│     ├── MDXCSeparator                  │
│     └── DemucsSeparator                │
└───────────────────┬────────────────────┘
                    │
                 PyTorch
                    │
             CUDA / CPU / future
```

## 3. Repository layout

```text
straticate/
├── README.md · LICENSE · ARCHITECTURE.md · ROADMAP.md
├── DEVELOPMENT.md · CONTRIBUTING.md · AGENTS.md
│
├── backend/
│   ├── pyproject.toml · uv.lock
│   ├── src/straticate/
│   │   ├── api/          # FastAPI routers, request/response wiring
│   │   ├── schemas/      # Pydantic models — the shared API contract
│   │   ├── audio/        # upload, temp storage, FFmpeg metadata/encoding
│   │   ├── jobs/         # job model, queue, state machine, cancellation
│   │   ├── models/       # model catalog, manifests, model manager
│   │   ├── inference/    # Separator interface + implementations
│   │   ├── telemetry/    # device stats, VRAM, utilization, NVML (optional)
│   │   ├── system/       # compute-device detection and abstraction
│   │   ├── config.py · logging.py · main.py
│   └── tests/
│
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── api/          # REST client + generated types from OpenAPI
│   │   ├── ws/           # WebSocket client, typed event handling
│   │   ├── state/        # application state (upload → configure → job → results)
│   │   ├── components/   # UI components
│   │   ├── audio/        # Web Audio playback engine (sync, solo, mute)
│   │   └── App.tsx · main.tsx
│   └── tests/
│
├── models/
│   ├── catalog.json      # versioned model catalog (logical model entries)
│   └── schemas/          # JSON Schema for model manifests
│
├── docs/
│   ├── contracts/        # rest-api.md, websocket-events.md
│   └── features/         # one document per numbered feature
│
├── scripts/              # dev/automation scripts
├── testdata/             # small audio fixtures (seconds, not songs)
└── .github/workflows/    # CI
```

## 4. Backend / frontend boundary

The boundary is the REST API plus one WebSocket endpoint.

- **REST** (`/api/v1/...`) — commands and resource operations: upload audio,
  read metadata, list models/modes/devices, create/cancel jobs, fetch results,
  stream stems, export. See [docs/contracts/rest-api.md](docs/contracts/rest-api.md).
- **WebSocket** (`/api/v1/ws`) — server-push events: job lifecycle, processing
  progress, runtime telemetry, GPU statistics. See
  [docs/contracts/websocket-events.md](docs/contracts/websocket-events.md).

There is no high-frequency progress polling.

**Contract authority:** Pydantic models in `backend/src/straticate/schemas/`
plus FastAPI's generated OpenAPI document are the single source of truth.
Frontend TypeScript types are *generated* from the OpenAPI document
(`openapi-typescript`); WebSocket payloads reuse the same Pydantic schemas,
exported into the OpenAPI components so they are generated too. Backend and
frontend never maintain duplicate hand-written schemas.

## 5. Audio flow

```text
Browser file → POST /api/v1/audio (multipart)
      → validation (size, decodability — never trust the extension)
      → temporary storage (working directory keyed by audio ID)
      → ffprobe metadata extraction (duration, format, channels,
        sample rate, bit depth, size)
      → AudioFile + AudioMetadata returned to client

Job start → decode to canonical PCM (FFmpeg) → separator consumes PCM
      → separator produces per-stem PCM → encode outputs
        (WAV PCM24 default · WAV float32 · FLAC)
      → results stored under the job's output directory
      → stems streamed to the browser for preview; exported on demand
```

FFmpeg is the single compatibility layer for decode/probe/encode. Target input
formats: WAV, FLAC, MP3, AAC, M4A, AIFF, OGG.

## 6. Job model

Every separation is an asynchronous job. The initiating HTTP request returns
immediately with the created job; all subsequent information arrives over the
WebSocket (with REST reads available for reconnection/refresh).

### State machine

```text
queued → preparing → decoding → loading_model → separating
       → post_processing → encoding → completed

any non-terminal state → cancelled   (user cancellation)
any non-terminal state → failed      (error)
```

Terminal states: `completed`, `cancelled`, `failed`. Transitions are emitted as
`job_stage_changed` WebSocket events; illegal transitions are programming
errors and must raise.

### Job record

```text
job ID (ULID) · input audio reference · separation configuration
model ID · compute device · state · progress (0..1)
timestamps (created/started/finished, per-stage)
runtime metrics snapshot · outputs (stems) · error information
```

### Scheduling

Initial policy: **one GPU = one active inference job**. A single in-process
asyncio queue; additional jobs wait in `queued`. Inference itself runs in a
worker thread (or subprocess later, if isolation demands it) so the event loop
stays responsive. No Redis, no Celery, no distributed scheduler.

### Cancellation

Cooperative: a cancellation token is checked between chunks by the separator.
Cancelling a `queued` job removes it from the queue directly.

## 7. Separation engine abstraction

```python
class Separator(Protocol):
    @property
    def info(self) -> SeparatorInfo: ...          # model descriptor (§9 fields)

    def runtime_stats(self) -> SeparatorRuntimeStats | None: ...   # telemetry source (§12)

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,      # (chunks done/total, audio seconds)
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None = None,   # (stage)
    ) -> SeparationResult: ...
```

Rules:

- The job manager and API never know which architecture runs underneath.
- Separators report progress as `completed_chunks / total_chunks` — real work,
  never a timer, once real inference exists. Stage changes are a *separate*
  callback so a separator announces only the stages it really performs; the
  `JobExecutor` adapter forwards both verbatim and invents nothing.
- `job_id` and `output_dir` are keyword-only inputs, not part of the
  `SeparationConfiguration` contract: a separator writes its stems where the
  caller tells it to, and the on-disk layout stays the application's decision
  (`{data_dir}/jobs/{job_id}/stems/{stem}.wav`).
- A separator instance is long-lived and runs **one separation at a time**,
  which is what makes `runtime_stats()` unambiguous. Compute that would block
  the event loop is offloaded by the separator itself.
- **Constructing** one is offloaded by the *registry*, not the separator: a real
  backend reads hundreds of megabytes of weights on a cache miss, so
  `SeparatorRegistry.aget()` runs the builder in a worker thread and serializes
  concurrent misses for the same model behind one lock. `get()` remains for
  synchronous callers and is documented as blocking; request handlers use
  `aget`.
- Implementations: `FakeSeparator` first (see §8), then RoFormer-, MDX/MDXC-,
  and Demucs-family separators. The first real one is `RoFormerSeparator`
  (feature 026): a **vendored**, pinned Mel-Band RoFormer architecture under
  `inference/roformer/vendor/` plus Straticate's own chunked overlap-add loop.
  Architecture code that a published checkpoint loads into is pinned source, not
  a dependency — a dependency's next release can rename a module or reshape a
  layer, and the first symptom is a state dict that no longer loads.
- A separator declares nothing about UI; the frontend renders choices from
  model capabilities served by the backend.

## 8. Fake separator (architectural milestone)

Before any real model is integrated, `FakeSeparator` implements the full
interface: simulated chunk-based processing, deterministic real-time progress,
fake model/GPU statistics, cooperative cancellation, and placeholder stems
(derived from the source audio so outputs are predictable and playable). It
powers all normal CI and enables the complete
upload → configure → job → WebSocket → progress → telemetry → results → export
loop with no CUDA, no model downloads, and no ML infrastructure.

The placeholder stems are **not** separation and never pretend to be: the
source is decoded with FFmpeg and each stem is one cheap, deterministic
feed-forward comb filter of it (per-stem delay, polarity and gain), so the
outputs are playable, audibly distinct, never silent, and byte-reproducible.
The number of stems always comes from the model's stem list, so two-stem and
four-stem modes exercise the same code path. See
[docs/features/014-fake-separator.md](docs/features/014-fake-separator.md).

## 9. Model catalog and model management

The catalog (`models/catalog.json`, validated by
`models/schemas/model-manifest.schema.json`) lists *logical models*:

```json
{
  "schema_version": 1,
  "id": "vocals-hq-001",
  "display_name": "Vocals — High Quality",
  "architecture": "mel_band_roformer",
  "version": "1.0",
  "separation_mode": "vocals",
  "stems": ["vocals", "instrumental"],
  "sample_rate": 44100,
  "requirements": { "recommended_vram_mb": 8192 },
  "capabilities": { "cuda": true, "cpu": true }
}
```

Later fields: download URL, artifact size, SHA-256, code/weights licenses,
redistribution and commercial-use permissions, attribution, minimum RAM,
default inference parameters.

**Model manager**: discover → download → verify → install → remove. Download
flow: temporary artifact → SHA-256 verification → atomic rename into the models
directory. An incomplete or hash-mismatched artifact is never loaded or
executed. Weights are never shipped in the repository.

Implemented by feature 025. Installed weights live at
`{models_dir}/weights/{model_id}/weights.bin`, with the in-flight download as a
`.part` sibling so publishing is a same-filesystem `os.replace`; the model ID is
validated against the manifest's own pattern before it becomes a path. A model
whose manifest declares no `artifact` — every built-in separator — is *installed*
by definition and is never offered as a download. `update` (as distinct from
remove-then-install) and resumable transfers remain future work.

Two manifest blocks are **retained but private**, carried on the loader's
`CatalogEntry` rather than on the public `Model`: `artifact` (feature 025 — the
download URL and the pinned digest) and `default_inference_parameters` (feature
026 — the checkpoint's own hyperparameters plus its chunking). Only the
separator registered for that `architecture` knows what the latter means, which
is what makes adding another checkpoint of a known architecture a pure data
edit.

A model's `capabilities` are consulted **when a job is created**: an explicit
`device_id` the model does not support is refused with `model_device_unsupported`
(409), and a job that pinned no device gets the first detected device the model
does support. Weights that are catalogued but not installed are likewise refused
at create time (`model_weights_missing`, 409) rather than failing mid-run.

**User-facing quality tiers** (Fast / Balanced / High Quality) are a mapping
over catalog entries; users are never asked to choose architectures or
inference parameters. Advanced per-model tuning may appear later behind an
optional advanced UI.

The mapping is declared in the catalog: a model's optional `quality_tier`
(`fast` / `balanced` / `high_quality`, defaulting to `balanced`) names the tier
it backs, and it must be unique within its `separation_mode` because the tier ID
is what a job's `quality_id` selects. Separation modes are derived from the
catalog at startup — stems from the models (which must agree), labels from the
catalog file's optional `separation_modes` table or a humanized ID — so no mode,
stem list, or tier is hardcoded in application code.

## 10. Compute device abstraction

The backend exposes *logical* compute devices; raw PyTorch device objects never
leak through application-level APIs.

```json
{
  "id": "cuda:0",
  "backend": "cuda",
  "name": "NVIDIA GeForce RTX 5090",
  "memory_total_bytes": 34359738368
}
```

Priority: 1) NVIDIA CUDA, 2) CPU fallback. The `backend` discriminator is an
open set so MPS, MLX, DirectML, and other accelerators can be added without
API changes.

## 11. WebSocket architecture

One endpoint: `WS /api/v1/ws`. All messages are JSON objects with a `type`
discriminator, defined as Pydantic models (see
[docs/contracts/websocket-events.md](docs/contracts/websocket-events.md)):

```text
job_created · job_started · job_stage_changed · job_progress
runtime_metrics · job_completed · job_cancelled · job_failed
```

Design rules:

- The server broadcasts to all connected clients (single-user local app);
  per-job subscription filtering can come later if needed.
- Events carry enough state to render without an extra REST fetch; REST remains
  the source of truth for reconnect/refresh.
- `job_progress` is throttled server-side (~4 Hz max) — chunk-grained, not
  sample-grained.
- `runtime_metrics` is sampled on an interval (~1 Hz) while a job runs.

## 12. Runtime telemetry

A telemetry sampler runs while a job is active and publishes
`runtime_metrics` events combining:

- **Model:** display name, architecture, version, separation mode, stem count.
- **GPU** (when available): name, backend, total/allocated/peak VRAM (PyTorch
  memory APIs), utilization and temperature (NVML *optional* — basic operation
  must never require NVML).
- **Processing:** stage, current/total chunks, elapsed time, audio processed,
  processing speed, and **real-time factor** (`RTF = audio duration /
  processing duration`) — a standard performance metric for this project.

## 13. Frontend architecture

- **State flow** mirrors the product workflow: `select → configure → separate →
  inspect → export`, held in a small typed store (React context/reducer to
  start; no heavyweight state library without documented need).
- **API layer** wraps the generated OpenAPI types; **WS layer** decodes typed
  events and feeds the store. Both are mockable so frontend features develop
  against contracts, not a running backend.
- **Playback** uses the Web Audio API: stems decoded into buffers, one shared
  clock, per-stem gain nodes for solo/mute/gain, synchronized seek. Playback is
  an inspection tool — no editing features.

## 14. Engineering constraints

Prefer small modules, typed boundaries, explicit state, testable components,
replaceable implementations, small PRs, documented contracts. Do not introduce
Redis, Celery, Kubernetes, microservices, cloud infrastructure, authentication,
or a distributed job scheduler unless a concrete requirement emerges: the first
version is a locally hosted application using the local machine and its GPU.

Normal CI never requires CUDA, a GPU, or multi-gigabyte model downloads — the
fake separator covers it, and the real separator's plumbing is tested against a
synthetic few-kilobyte checkpoint built at test time. Real GPU/model validation
is a separate, manually-triggered integration tier (`pytest -m integration`).

PyTorch is a runtime dependency from feature 026 onwards, and it is pinned to
PyTorch's **CPU wheel index** so that this constraint survives contact with it:
the default PyPI wheel bundles a multi-gigabyte CUDA runtime on Linux, which CI
would download on every run for a machine with no GPU. Installing a CUDA build
is one documented command and changes no code, no settings and no API
(DEVELOPMENT.md, *PyTorch and CUDA*).
