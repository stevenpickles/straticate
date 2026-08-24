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

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| GET | `/jobs/{job_id}/result` | `SeparationResult` of a completed job | `200` |
| GET | `/jobs/{job_id}/stems/{stem_name}` | Stream stem audio for preview (supports `Range`) | `200` / `206` |
| GET | `/jobs/{job_id}/export?format=wav_pcm24&stems=vocals,instrumental` | Download stems in the requested format (zip when multiple), plus `separation.json` | `200` |

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

**A result exists only for a `completed` job.** All three routes read the same
record through the same lookup: any other state — still processing, `cancelled` or `failed` — is a
`409` `result_not_available` carrying the job's current `state` in `detail`,
so a client can say *why* there is nothing to play without a second error code
to branch on. `GET /jobs/{job_id}` remains the place to read the full record,
including a failed job's `error`.

**The result's `stems` list is the authority on which stem names exist.**
`stem_name` is validated against `SeparationResult.stems` — never against a
directory listing and never against a hardcoded set — so two-stem and
four-stem jobs behave identically and a file that appears in a job's output
directory without being in the result is not servable.

### Stem streaming and `Range`

Stems are served from the job's output directory
(`{data_dir}/jobs/{job_id}/stems/{stem}.wav`; see
[docs/features/014-fake-separator.md](../features/014-fake-separator.md)). The
`Content-Type` follows the file's suffix — `audio/wav` for the 16-bit WAV the
separator writes today, `audio/flac` when feature 022's formats land.
`Content-Disposition` is `inline` (the export route is where downloads live).

Byte ranges are fully supported, so an `<audio>` element or a Web Audio fetch
can seek without downloading the whole stem:

| Request | Response |
| --- | --- |
| no `Range` | `200`, whole file, `Accept-Ranges: bytes`, `Content-Length`, `ETag`, `Last-Modified` |
| `Range: bytes=0-99` | `206`, exactly those 100 bytes, `Content-Range: bytes 0-99/{size}` |
| `Range: bytes=N-` | `206`, bytes `N` … `size-1`, `Content-Range: bytes N-{size-1}/{size}` |
| `Range: bytes=-N` | `206`, the final `N` bytes |
| `Range` at or past `size` | `416`, `Content-Range: bytes */{size}` |
| unparsable `Range` | `400` |
| `If-Range` matching the `ETag`/`Last-Modified` | the range is honoured; otherwise the whole file |

`416` and `400` are the only responses on these routes that are **not** the
JSON error envelope: they come from the byte-range layer as plain text, which
is what a media client reading `Content-Range` expects (RFC 9110). Every
application error below uses the envelope.

### Export

`GET /jobs/{job_id}/export` transcodes a completed job's stems and returns them
as a download. Two query parameters, both optional:

| parameter | values | default |
| --- | --- | --- |
| `format` | `wav_pcm24` · `wav_float32` · `flac` | `wav_pcm24` |
| `stems` | comma-separated stem names, e.g. `vocals,drums` | **every stem in the result** |

`stems` is validated against `SeparationResult.stems`, exactly as
`stem_name` is on the streaming route. Surrounding whitespace on each name is
ignored, the selection is deduplicated and returned in the result's own order —
so `drums,bass`, `bass,drums` and `bass,drums,bass` describe the same export —
and any name the result does not list is a `stem_not_found` 404. A
present-but-empty value (`?stems=`) is a `validation_error` 422: **omitting**
the parameter is how you ask for all of them.

**How many stems you asked for decides the response shape:**

| selection | response | `Content-Type` | `Content-Disposition` |
| --- | --- | --- | --- |
| exactly one stem | the transcoded audio file itself | `audio/wav` or `audio/flac` (by suffix) | `attachment; filename="{job_id}-{format}-{stem}.{ext}"` |
| more than one (including the default) | a zip: one file per stem, named `{stem}.{ext}`, plus `separation.json` | `application/zip` | `attachment; filename="{job_id}-{format}.zip"` |

**A single-stem export therefore carries no `separation.json`.** That is a
deliberate choice, not an oversight: the point of a one-stem export is to hand
the user one file they can drop straight into a DAW, and wrapping it in a zip
to carry a manifest would defeat that. A client that wants the manifest can ask
for two or more stems, or read the same record from
`GET /jobs/{job_id}/result`.

`separation.json` — the job's `SeparationResult` verbatim under `result`,
alongside the export's own metadata:

```json
{
  "format": "wav_pcm24",
  "model_id": "vocals-hq-001",
  "stems": ["vocals", "instrumental"],
  "exported_at": "2026-08-24T10:29:47.512345+00:00",
  "result": {
    "job_id": "01JOB...",
    "model_id": "vocals-hq-001",
    "stems": [
      { "name": "vocals", "duration_seconds": 227.4, "sample_rate_hz": 44100, "channels": 2 },
      { "name": "instrumental", "duration_seconds": 227.4, "sample_rate_hz": 44100, "channels": 2 }
    ],
    "metrics": { "processing_seconds": 29.0, "realtime_factor": 7.83 }
  }
}
```

`stems` lists what is actually in the archive (which may be a subset);
`result.stems` lists everything the job produced. `result` is byte-for-byte the
object `GET /jobs/{job_id}/result` serves, so it parses with the same
`SeparationResult` type and no parallel contract exists.

**Bit depth is honest.** The separator writes 16-bit PCM WAV, so `wav_pcm24`
and `wav_float32` change the container encoding and add **no information** — a
24-bit export does not recover detail the stems never had. Sample rate,
channel count and duration are always the source's, unchanged. This note stops
being true when a real separator (feature 026) produces higher-precision
output; the formats exist now so the export path is complete and so a user
whose downstream tools require 24-bit or float files gets them.

Export artifacts are built once and cached under
`{data_dir}/jobs/{job_id}/exports/`, keyed by format and the sorted stem list:
a completed job's stems are immutable, so a repeated identical download is
served straight from disk. Simultaneous identical requests share a single
build — the second waits for the first and then serves the cached file rather
than transcoding again. Nothing ever deletes these artifacts (see feature 021's
note: no retention policy exists yet).

A client that disconnects mid-download does not abort the export: the build
finishes and publishes its artifact, so the next request for it is a cache hit.

### Error codes

| code | status | when |
| --- | --- | --- |
| `job_not_found` | 404 | unknown `job_id` |
| `result_not_available` | 409 | the job exists but is not `completed`; `detail` carries `job_id` and the current `state` |
| `stem_not_found` | 404 | the job's result lists no stem with that name; `detail` carries `available_stems` |
| `stem_file_missing` | 404 | the result lists the stem but its file is gone from disk (an orphaned job directory from a previous process — job records are in-memory only) |
| `export_failed` | 500 | *(export only)* a transcode or archive step failed; `detail` carries `job_id`, `format` and a short `reason` classification |
| `validation_error` | 422 | *(export only)* an unknown `format`, or a present-but-empty `stems` |

A stem name that could not be a stem name at all (path traversal, an absolute
path, a URL-encoded separator) is simply not in the result's stem list, so it
comes back as a clean `stem_not_found` 404 — never a 500 and never a file from
outside the job's stem directory. The same holds for a name inside `stems=`.

`export_failed`'s `reason` is one of `transcode_failed` (FFmpeg exited
non-zero) or `filesystem_error` (the archive or the artifact could not be
written) — a **classification, not a message**. FFmpeg's stderr and OS error
strings name absolute server paths, so they are written to the server log and
never to the response, exactly as `internal_error` does for an unhandled
exception. Clients should branch on the code, show `reason` only in diagnostic
output, and never parse it for detail it does not carry.
