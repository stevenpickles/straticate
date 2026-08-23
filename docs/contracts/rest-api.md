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
| POST | `/audio` | Multipart upload (`file` field). Validates and probes. → `AudioFile` |
| GET | `/audio/{audio_id}` | Fetch `AudioFile` |
| DELETE | `/audio/{audio_id}` | Remove uploaded audio and derived data |

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

## Jobs

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/jobs` | Create a separation job → `Job` (state `queued`), returns immediately |
| GET | `/jobs` | List jobs |
| GET | `/jobs/{job_id}` | Fetch `Job` (reconnect/refresh source of truth) |
| POST | `/jobs/{job_id}/cancel` | Request cooperative cancellation → `Job` |

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
  "configuration": { "mode_id": "vocals", "quality_id": "high_quality", "device_id": "cuda:0" },
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
