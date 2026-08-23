# WebSocket Event Contract (v1)

Status: **authoritative** — the Pydantic event models in
`backend/src/straticate/schemas/events.py` (feature 005) are the source of
truth, exposed via OpenAPI components so TypeScript types are generated, not
hand-written. The WebSocket event *hub* (the server that emits these events)
lands with feature 013.

Endpoint: `WS /api/v1/ws`. Server → client push only (initially); the server
broadcasts to all connected clients. All messages are JSON objects
discriminated by `type`. Every job event carries `job_id`. REST
(`GET /jobs/{id}`) remains the source of truth for reconnect/refresh — events
are notifications, not the database.

## Event types

```text
job_created · job_started · job_stage_changed · job_progress
runtime_metrics · job_completed · job_cancelled · job_failed
```

### `job_created`

```json
{ "type": "job_created", "job_id": "01JOB...", "job": { "…": "full Job object" } }
```

### `job_started`

```json
{ "type": "job_started", "job_id": "01JOB...", "started_at": "2026-08-23T12:00:05Z" }
```

### `job_stage_changed`

```json
{ "type": "job_stage_changed", "job_id": "01JOB...", "stage": "separating", "previous_stage": "loading_model" }
```

`stage` uses the job state machine's processing states.

### `job_progress`

Throttled server-side (≤ ~4 Hz). Progress is real work:
`chunks_completed / chunks_total`.

```json
{
  "type": "job_progress",
  "job_id": "01JOB...",
  "stage": "separating",
  "progress": 0.65,
  "chunks_completed": 31,
  "chunks_total": 48,
  "elapsed_seconds": 18.2,
  "audio_processed_seconds": 148.0,
  "audio_total_seconds": 227.4
}
```

### `runtime_metrics`

Sampled ~1 Hz while a job runs. GPU block is `null` on CPU; `utilization` and
`temperature_celsius` are `null` when NVML is unavailable (NVML is optional).

```json
{
  "type": "runtime_metrics",
  "job_id": "01JOB...",
  "model": {
    "id": "vocals-hq-001",
    "display_name": "Vocals — High Quality",
    "architecture": "mel_band_roformer",
    "version": "1.0",
    "separation_mode": "vocals",
    "stem_count": 2
  },
  "gpu": {
    "device_id": "cuda:0",
    "name": "NVIDIA GeForce RTX 5090",
    "backend": "cuda",
    "memory_allocated_bytes": 9234179686,
    "memory_peak_bytes": 10133099161,
    "memory_total_bytes": 34359738368,
    "utilization": 0.91,
    "temperature_celsius": 63
  },
  "processing": {
    "stage": "separating",
    "chunks_completed": 31,
    "chunks_total": 48,
    "elapsed_seconds": 18.2,
    "audio_processed_seconds": 148.0,
    "realtime_factor": 7.9
  }
}
```

### `job_completed`

```json
{ "type": "job_completed", "job_id": "01JOB...", "result": { "…": "SeparationResult" } }
```

### `job_cancelled`

```json
{ "type": "job_cancelled", "job_id": "01JOB...", "stage_at_cancellation": "separating" }
```

### `job_failed`

```json
{
  "type": "job_failed",
  "job_id": "01JOB...",
  "error": { "code": "cuda_out_of_memory", "message": "…", "detail": {} }
}
```

## Client behavior

- On connect (or reconnect), fetch current jobs via REST, then apply events.
- Unknown `type` values must be ignored (forward compatibility).
- The frontend WS layer decodes events into the generated types and feeds the
  application store; components never touch the raw socket.
