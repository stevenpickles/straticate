# REST API Contract (v1)

Status: **authoritative** — the Pydantic schemas in
`backend/src/straticate/schemas/` and the OpenAPI document exported from them
(feature 005) are the source of truth; this file describes intent and
conventions. Export with
`uv run python -m straticate.scripts.export_openapi` (from `backend/`).

All routes are prefixed `/api/v1`. JSON everywhere except uploads (multipart)
and stem streaming (audio bytes). IDs are ULIDs.

## Conventions

- Errors use a single envelope, HTTP status + body:

```json
{
  "error": {
    "code": "audio_not_decodable",
    "message": "The uploaded file could not be decoded as audio.",
    "detail": {}
  }
}
```

- `code` is a stable machine-readable string (snake_case); `message` is
  human-readable; `detail` is optional structured context.
- Commands that start long work return immediately (`202`-style semantics);
  progress flows over the WebSocket.

## System

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/health` | `{ "status": "ok" }` |
| GET | `/version` | `{ "version": "0.1.0" }` |
| GET | `/system/devices` | `ComputeDevice[]` |

`ComputeDevice`:

```json
{
  "id": "cuda:0",
  "backend": "cuda",
  "name": "NVIDIA GeForce RTX 5090",
  "memory_total_bytes": 34359738368
}
```

`backend` is an open enum: `cuda`, `cpu` initially; later `mps`, `directml`, …

## Audio

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/audio` | Multipart upload (`file` field). Validates and probes. → `201` + `AudioFile` |
| GET | `/audio/{audio_id}` | Fetch `AudioFile` |
| DELETE | `/audio/{audio_id}` | Remove uploaded audio and derived data → `204` |

Upload validation runs in order: size limit (configurable via
`STRATICATE_MAX_UPLOAD_BYTES`, default 1 GiB) → ffprobe decodability.
Error codes: `audio_too_large` (413), `audio_not_decodable` (422),
`audio_not_found` (404, GET/DELETE); a missing `file` part is a standard
`validation_error` (422).

`AudioFile`:

```json
{
  "id": "01ABC...",
  "filename": "Midnight Train.flac",
  "size_bytes": 44771328,
  "uploaded_at": "2026-08-23T12:00:00Z",
  "metadata": {
    "duration_seconds": 227.4,
    "container": "flac",
    "codec": "flac",
    "channels": 2,
    "sample_rate_hz": 44100,
    "bit_depth": 24,
    "bit_rate_bps": 1411000
  }
}
```

Metadata comes from `ffprobe` on the actual media — never from the filename
extension. `bit_depth`/`bit_rate_bps` are nullable (lossy formats).

## Models and modes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/models` | Installed/available logical models → `Model[]` |
| GET | `/models/{model_id}` | One `Model` |
| GET | `/separation-modes` | `SeparationMode[]` derived from model capabilities |

Unknown model ID → `404` with code `model_not_found`.

`Model` is the API-facing projection of a catalog manifest
(`models/schemas/model-manifest.schema.json`):

```json
{
  "id": "fake-vocals-001",
  "display_name": "Fake Vocals (development)",
  "architecture": "fake",
  "version": "1.0",
  "separation_mode": "vocals",
  "quality_tier": null,
  "stems": ["vocals", "instrumental"],
  "sample_rate": 44100,
  "requirements": { "recommended_vram_mb": 0, "minimum_ram_mb": null },
  "capabilities": { "cuda": true, "cpu": true }
}
```

Manifest fields that are *not* user-facing — `artifact`, `licensing`,
`default_inference_parameters` — are deliberately absent from `Model` and never
appear in any response: users choose modes and quality tiers, never
architectures or inference parameters (ARCHITECTURE.md §1, §9).

`quality_tier` is `fast | balanced | high_quality | null` (feature 010; `null`
means `balanced`). It is the tier this model backs inside its separation mode,
and it is unique per mode — the tier ID is what `SeparationConfiguration.quality_id`
selects.

`SeparationMode` (what the frontend renders — never hardcoded client-side):

```json
{
  "id": "vocals",
  "display_name": "Vocal Isolation",
  "stems": ["vocals", "instrumental"],
  "quality_options": [
    { "id": "fast", "display_name": "Fast", "model_id": "vocals-fast-001" },
    { "id": "high_quality", "display_name": "High Quality", "model_id": "vocals-hq-001" }
  ]
}
```

Modes are derived, not stored: models are grouped by `separation_mode`, `stems`
come from the models (which must agree), and each model contributes one
`QualityOption` for its tier, ordered `fast → balanced → high_quality`. A mode
served by a single model still exposes one option. Mode labels come from the
catalog file's optional `separation_modes` table, falling back to a humanized
mode ID; tier labels are humanized tier IDs.

## Jobs

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| POST | `/jobs` | Create a separation job → `Job` (state `queued`), returns immediately | `201` |
| GET | `/jobs` | List jobs, oldest first | `200` |
| GET | `/jobs/{job_id}` | Fetch `Job` (reconnect/refresh source of truth) | `200` |
| POST | `/jobs/{job_id}/cancel` | Request cooperative cancellation → `Job` | `200` |

Create request (`SeparationConfiguration`):

```json
{
  "audio_id": "01ABC...",
  "mode_id": "vocals",
  "quality_id": "high_quality",
  "device_id": "cuda:0"
}
```

`device_id` optional — backend picks the best device by default.

`Job`:

```json
{
  "id": "01JOB...",
  "audio_id": "01ABC...",
  "configuration": { "audio_id": "01ABC...", "mode_id": "vocals", "quality_id": "high_quality", "device_id": "cuda:0" },
  "model_id": "vocals-hq-001",
  "state": "separating",
  "progress": 0.65,
  "created_at": "...",
  "started_at": "...",
  "finished_at": null,
  "error": null,
  "result": null
}
```

States: `queued · preparing · decoding · loading_model · separating ·
post_processing · encoding · completed · cancelled · failed` (see
ARCHITECTURE.md §6). On `completed`, `result` is a `SeparationResult`.

**`configuration.device_id` is always the resolved device.** Creating a job
resolves the compute device — the request's `device_id`, or the backend's
preferred device when the request omitted it — and records *that* on the job.
So `Job.configuration.device_id` is never null in a response or an event, even
though the create request's field is optional.

**`GET /jobs` returns jobs in submission order (oldest first)** — the order the
backend accepted them, which is also the order they run in (the queue is FIFO
with one active job, ARCHITECTURE.md §6). Clients that want newest-first sort
client-side. Job records are in-memory only: the list is empty after a restart.

**Cancellation is a request, not a stop.** `POST /jobs/{job_id}/cancel` takes no
body. A `queued` job is cancelled immediately; a running one is asked to stop at
its next cooperative checkpoint, so the returned `Job` may still be in a
processing state and the authoritative transition arrives as a `job_cancelled`
WebSocket event. Cancelling a job that already reached a terminal state is a
**no-op that still returns `200`** — the operation is idempotent and never
produces a conflict.

Job error codes:

| code | status | when |
| --- | --- | --- |
| `audio_not_found` | 404 | `audio_id` is unknown, or its file is gone from disk |
| `separation_mode_not_found` | 404 | `mode_id` is not one of the derived separation modes |
| `quality_option_not_found` | 404 | `quality_id` is not an option of that mode |
| `device_not_found` | 404 | `device_id` is not a detected compute device |
| `separator_unavailable` | 501 | no separator implementation exists for the resolved model's architecture |
| `job_not_found` | 404 | unknown `job_id` (get/cancel) |
| `service_unavailable` | 503 | the job manager is shutting down (create/cancel) |

A malformed create body is the standard `validation_error` (422). References are
resolved in the order audio → mode → quality → device → separator, so the first
unresolvable one is what the client is told about.

## Results, stems, export

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/jobs/{job_id}/result` | `SeparationResult` |
| GET | `/jobs/{job_id}/stems/{stem_name}` | Stream stem audio for preview (supports `Range`) |
| GET | `/jobs/{job_id}/export?format=wav_pcm24&stems=vocals,instrumental` | Download stems in the requested format (zip when multiple), plus `separation.json` |

`SeparationResult`:

```json
{
  "job_id": "01JOB...",
  "model_id": "vocals-hq-001",
  "stems": [
    { "name": "vocals", "duration_seconds": 227.4, "sample_rate_hz": 44100, "channels": 2 },
    { "name": "instrumental", "duration_seconds": 227.4, "sample_rate_hz": 44100, "channels": 2 }
  ],
  "metrics": { "processing_seconds": 29.0, "realtime_factor": 7.83 }
}
```

Export formats: `wav_pcm24` (default), `wav_float32`, `flac`.
