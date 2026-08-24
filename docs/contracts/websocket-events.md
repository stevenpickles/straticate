# WebSocket Event Contract (v1)

Status: **authoritative** — the Pydantic event models in
`backend/src/straticate/schemas/events.py` (feature 005) are the source of
truth, exposed via OpenAPI components so TypeScript types are generated, not
hand-written. The WebSocket event *hub* (the server that emits these events)
is `backend/src/straticate/jobs/hub.py` (feature 013).

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

Sampling behaviour (feature 019, `backend/src/straticate/telemetry/sampler.py`):

- **Interval:** one sample per `DEFAULT_SAMPLE_INTERVAL_SECONDS` (1.0 s,
  ARCHITECTURE.md §12). The first sample of a job is taken as soon as it
  starts, then one per interval.
- **Only while a job is active.** Sampling begins on `job_started` and stops on
  the job's terminal event (`job_completed` / `job_cancelled` / `job_failed`).
  No `runtime_metrics` is ever emitted before a job starts or after it ends;
  the terminal event is handed to the hub before sampling stops, so a client
  never sees telemetry after the terminal event for that job.
- **Never before the separator has statistics.** The payload is built entirely
  from the running separator's snapshot. While that snapshot is absent — or
  still describes a *previous* job on a reused separator — the tick publishes
  nothing at all rather than a stale or mismatched sample. A short job may
  therefore produce no `runtime_metrics` at all, and clients must not depend on
  receiving one.
- **The `gpu` block is the separator's own device report, verbatim**, and
  `null` when the separator reports no device (the "running on CPU" shape).
  Nothing is substituted from `GET /system/devices`: what the telemetry panel
  shows is what actually ran the audio. A `backend` value of `"fake"`
  (the development separator) is legitimate — `backend` is an open set.
- **Nobody connected → nothing sampled.** When no WebSocket client is
  connected the tick does no work: no snapshot is read and no event is built.
  Telemetry is a live readout, never a recording — there is no history and no
  REST endpoint to fetch past samples, and a client that connects mid-job
  simply receives the next sample.
- **Telemetry never affects a job.** A sampling failure is logged and
  swallowed; it neither ends sampling nor reaches the job pipeline.

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

## Connection lifecycle

The server accepts the connection immediately (no handshake message) and
starts pushing at once. There is no client → server protocol in v1: anything a
client sends is read and discarded, never parsed, and never closes the
connection.

Close codes the server may send:

| Code | Meaning | Client action |
| --- | --- | --- |
| `1001` | Server is shutting down. | Reconnect with backoff. |
| `1011` | Sending to this client failed. | Reconnect. |
| `1013` | This client could not keep up; its outbound buffer overflowed with events that may not be dropped. | Reconnect, then resync over REST. |

Backpressure: each connection has a bounded server-side outbound buffer. When
it overflows, the oldest buffered `job_progress` / `runtime_metrics` message
is dropped (both are periodic samples superseded by the next one). Events that
cannot be reconstructed — `job_created`, `job_started`, `job_stage_changed`,
and the terminal `job_completed` / `job_cancelled` / `job_failed` — are never
dropped: a client whose buffer is saturated with them is disconnected with
`1013` instead. Terminal events are therefore either delivered or followed by
a visible disconnect, never silently lost.

## Client behavior

- On connect (or reconnect), fetch current jobs via REST, then apply events.
- Unknown `type` values must be ignored (forward compatibility).
- The frontend WS layer decodes events into the generated types and feeds the
  application store; components never touch the raw socket.
- Reconnect on any close code and refetch over REST; the server never replays
  missed events.
