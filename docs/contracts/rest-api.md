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
| GET | `/system/storage` | `StorageReport` |

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

### Free disk space (feature 040)

`GET /system/storage` reports the room available for model weights on the
filesystem holding `Settings.models_dir` — the directory an install writes into
(`{models_dir}/weights/{model_id}/weights.bin`).

`StorageReport`:

```json
{
  "free_bytes": 2147483648,
  "total_bytes": 512110190592
}
```

- Read **fresh on every request**, never cached: free space changes constantly
  and the underlying call is one syscall. (`/system/devices`, by contrast, is
  probed once at startup because devices cannot change during a run.) The read
  is a *blocking* filesystem call — it can hang for as long as an unresponsive
  network mount does — so the route runs it in a worker thread and the event
  loop keeps serving everything else meanwhile.
- **`null` means unknown, and it is a documented state rather than an error.**
  A host that cannot answer — a models directory whose entire path is missing,
  a permissions failure, a filesystem the platform has no answer for — still
  responds `200`, with both fields `null`. Clients render that as "unknown" and
  treat it as the *cautious* case; they must not treat it as "fine", and must
  not substitute `0`.
- **`free_bytes: 0` is not unknown.** It is a full disk — the case worth
  warning loudest about. (This is why unknown is spelled `null` here while an
  unknown `ComputeDevice.memory_total_bytes` is spelled `0`: a machine with
  zero bytes of RAM is impossible, so `0` is unambiguous there.)
- When `models_dir` does not exist yet — the normal state of a fresh checkout,
  since the first install creates it — the figures describe its **nearest
  existing ancestor**, which is the filesystem those directories will be
  created on. No error, and no separate state.
- No filesystem path is returned. The client has no use for one, and it would
  put the server's directory layout (and its user's home directory) on the wire
  for no benefit.

This is a figure the browser genuinely cannot obtain: weights are written by
the backend, on the machine running Straticate, and
`navigator.storage.estimate()` describes the page origin's quota inside the
*browser's* profile directory — a different number about a different disk.

Nothing refuses an install on the strength of this report; see
`docs/features/040-free-disk-space-endpoint.md` for why it warns rather than
blocks. `POST /models/{id}/install` is unchanged, and an install that runs out
of disk still fails the way it always did (`download_failed` with
`detail.reason: "filesystem_error"`), leaving nothing behind.

## Audio

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/audio` | Multipart upload (`file` field). Validates and probes. → `201` + `AudioFile` |
| GET | `/audio/{audio_id}` | Fetch `AudioFile` |
| DELETE | `/audio/{audio_id}` | Remove uploaded audio and derived data → `204` |

Upload validation runs in order: size limit (configurable via
`STRATICATE_MAX_UPLOAD_BYTES`, default 1 GiB) → ffprobe decodability.
Error codes: `audio_too_large` (413), `audio_not_decodable` (422),
`audio_probe_timed_out` (504), `audio_not_found` (404, GET/DELETE); a missing
`file` part is a standard `validation_error` (422).

`audio_probe_timed_out` is deliberately *not* `audio_not_decodable`. Every
FFmpeg and ffprobe invocation is bounded by `STRATICATE_FFMPEG_TIMEOUT_SECONDS`
(default 600), and expiry means the tool was killed without reaching a verdict —
so the client is told the probe timed out (`detail.timeout_seconds`) and may
retry, rather than being told its file is broken. See **Timeouts** below.

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

| Method | Path | Purpose | Status |
| --- | --- | --- | --- |
| GET | `/models` | Every catalogued logical model → `Model[]` | `200` |
| GET | `/models/{model_id}` | One `Model`, including installation state and progress | `200` |
| POST | `/models/{model_id}/install` | Start downloading the model's weights; returns immediately | `202` |
| DELETE | `/models/{model_id}/weights` | Remove installed weights → the updated `Model` | `200` |
| GET | `/separation-modes` | `SeparationMode[]` derived from model capabilities | `200` |

Unknown model ID → `404` with code `model_not_found`.

`Model` is the API-facing projection of a catalog manifest
(`models/schemas/model-manifest.schema.json`):

```json
{
  "id": "vocals-hq-001",
  "display_name": "Vocals — High Quality",
  "architecture": "mel_band_roformer",
  "version": "kim-vocal-2",
  "development_only": false,
  "separation_mode": "vocals",
  "quality_tier": "high_quality",
  "stems": ["vocals", "instrumental"],
  "sample_rate": 44100,
  "requirements": { "recommended_vram_mb": 8192, "minimum_ram_mb": 8192 },
  "capabilities": { "cuda": true, "cpu": true },
  "licensing": { "code_license": "MIT", "weights_license": "MIT" },
  "installation": {
    "state": "available",
    "requires_download": true,
    "total_bytes": 913106900,
    "downloaded_bytes": null,
    "progress": null,
    "error": null
  }
}
```

### Development fixtures are not served by default

A manifest entry may declare `development_only: true`, meaning it exists to
exercise the application — CI, the backend suite, the Playwright tier — and does
not separate audio. The fake separator of ARCHITECTURE.md §8 is exactly that: a
comb filter that is "not separation and never pretends to be". Such entries are
**excluded from the catalog** unless the server sets
`STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1`
(`Settings.include_development_models`, default off, feature 032).

The filter is applied once, where the catalog is loaded, so it is the same on
every surface:

| surface | default (fixtures hidden) | `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1` |
| --- | --- | --- |
| `GET /models` | fixture entries absent | present, `development_only: true` |
| `GET /models/{fixture_id}` | `404 model_not_found` | `200` |
| `POST /models/{fixture_id}/install`, `DELETE .../weights` | `404 model_not_found` | as before (`409 model_not_downloadable` — a fixture has no artifact) |
| `GET /separation-modes` | no fixture-backed tier; a mode left with **no** tier is not served at all | unchanged from before feature 032 |
| `POST /jobs` naming a fixture's tier | `404 quality_option_not_found`, or `404 separation_mode_not_found` if the whole mode was fixture-backed | accepted |

`/models` filters too, not only `/separation-modes`. `/models` is arguably an
inventory rather than a rendering source, but Straticate is local-first with no
authentication and a single audience: an inventory that lists a comb filter
`/separation-modes` refuses to offer is a contradiction a client must reconcile,
and a fixture a client can see is one it can offer to install. **No new error
code was introduced.** On a server that hides fixtures, the ID names nothing the
catalog contains and `model_not_found` says precisely that; a distinct "hidden"
code would be a second condition every client must handle and would advertise an
entry the server chose not to serve.

Between features 032 and 028 this meant `standard_stems` was **not** a
separation mode under default settings at all: its only model was a fixture, so
the mode disappeared rather than being served with an empty `quality_options`
list. Feature 028 gave it a real model (`standard-stems-001`), so a default
server serves both modes again, each with the tiers its real models back —
`vocals` with `high_quality` and `standard_stems` with `balanced`. The rule is
unchanged and is the part that matters: **no mode is ever served with an empty
`quality_options` list**, because an empty mode is a choice the frontend would
render and nobody could act on.

`development_only` appears on `Model` (never on `QualityOption`), so a client
that has deliberately opted in can label what it is showing. On a default server
it is `false` on every model returned.

Manifest fields that are *not* user-facing — `artifact` and
`default_inference_parameters` — are deliberately absent from `Model` and never
appear in any response: users choose modes and quality tiers, never
architectures, download URLs, checksums or inference parameters
(ARCHITECTURE.md §1, §9).

`licensing` **is** user-facing (feature 025). A user should be able to read a
model's terms *before* installing its weights, which is the only moment the
terms can still change the decision. It is the manifest's `licensing` block
verbatim, or `null` when the manifest declares none; every field inside is
nullable, and `null` means "not declared", never "not permitted":

```json
{
  "code_license": "MIT",
  "weights_license": "MIT",
  "redistribution_permitted": true,
  "commercial_use_permitted": true,
  "attribution": "Upstream Author"
}
```

`quality_tier` is `fast | balanced | high_quality | null` (feature 010; `null`
means `balanced`). It is the tier this model backs inside its separation mode,
and it is unique per mode — the tier ID is what `SeparationConfiguration.quality_id`
selects.

### Installation state

`installation` answers a question the catalog alone cannot: **are this model's
weights on disk?** Being catalogued and being ready to run are different facts.

| field | meaning |
| --- | --- |
| `state` | `available` · `downloading` · `installed` · `failed` |
| `requires_download` | whether the model has a weights artifact at all |
| `total_bytes` | artifact size from the manifest; `null` when there is none |
| `downloaded_bytes` | bytes received so far; `null` unless downloading |
| `progress` | `downloaded_bytes / total_bytes` in `[0, 1]`; `null` unless downloading |
| `error` | why the last attempt failed; `null` unless `state` is `failed` — same shape as the envelope's `error` |

**A model with no `artifact` block is `installed` by definition** and
`requires_download` is `false`: it needs no weights, so it is never presented as
something to download, and installing or removing its weights is a
`model_not_downloadable` (409). Every built-in model — the development fixtures
today, which a default server does not serve at all (see above) — is in this
state permanently.

```text
available ──POST install──▶ downloading ──verified──▶ installed
     ▲                           │                        │
     │                           └── failure ──▶ failed    │
     └────────── DELETE weights ◀──────────────────────────┘
```

`failed` is a report, not a resting place: **nothing is on disk in that state**,
and the next install clears it. There is no `update` — remove and install again.

**Progress is polled from this resource, not pushed as a WebSocket event.** The
"no polling loops" rule (ARCHITECTURE.md §3, §11) is about *job progress*:
chunk-grained, ~4 Hz, during inference. A model install is rare, user-initiated
and coarse, its state is part of the model resource a client fetches anyway, and
REST is already the documented source of truth for reconnect and refresh. The
reasoning is written up in
[docs/features/025-model-download-manager.md](../features/025-model-download-manager.md);
adding a `model_install_progress` event later would be purely additive.

**`POST /models/{model_id}/install` returns immediately** (`202`), like every
command that starts long work. A multi-hundred-megabyte transfer never holds an
HTTP request open. The response body is the model in state `downloading`;
progress and the outcome are read from `GET /models/{model_id}`. Installing a
model whose weights are already present is an idempotent no-op reporting
`installed`. Removing weights that are not installed is likewise a no-op
reporting `available`.

**`DELETE /models/{model_id}/weights` cancels a running install** rather than
refusing it. "I do not want this model's weights" includes the ones being
fetched, and it is also the escape hatch from a download that will not finish:
the network bound is *per operation*, not a total budget, so a host trickling
bytes just under the timeout could otherwise hold a model in `downloading` until
the process was restarted. The cancelled download removes its own partial file
before the response is sent.

Model management error codes:

| code | status | when |
| --- | --- | --- |
| `model_not_found` | 404 | unknown `model_id` (get / install / remove). An ID that could not be a model ID at all — a traversal attempt, an absolute path — is simply not a catalog key and exits here |
| `model_not_downloadable` | 409 | the model declares no `artifact`; it is built in and always installed. `detail` carries `model_id` |
| `model_busy` | 409 | an install is already running for this model. `detail` carries `model_id`. **Install only** — `DELETE .../weights` cancels a running install instead of refusing |

**`download_failed` and `checksum_mismatch` are install-failure codes, not HTTP
statuses.** The request that started the install returned long before the
download could fail, so — exactly as `audio_decode_timed_out` does for a job
(see **Timeouts**) — the code arrives in `installation.error`:

| code | when | `detail` |
| --- | --- | --- |
| `download_failed` | the artifact could not be fetched in full | `model_id`, `reason`, plus the sizes involved |
| `checksum_mismatch` | the bytes are not the SHA-256 the catalog pins | `model_id`, `actual` (the digest that arrived) |

`download_failed`'s `reason` is a **classification, not a message**:
`http_status` (the host answered, but not `200` — `detail.status_code` says
what), `connection_failed` (refused, reset, or timed out), `size_exceeded` (the
body is larger than the manifest's `size_bytes`, refused from the declared
`Content-Length` or stopped mid-stream), `size_mismatch` (it ended short),
`filesystem_error` (the artifact could not be written), or `unexpected_error`
(the install raised something the manager does not classify — reported rather
than swallowed, because a model silently returning to `available` is
indistinguishable from "never tried"). OS error strings and FFmpeg-style
diagnostics name absolute server paths, so they are logged and never returned —
the same discipline `export_failed` applies.

**No failure ever names the download URL, and `checksum_mismatch` never returns
the pinned digest.** `installation.error` is served to every API caller, and
large weights are routinely hosted behind presigned URLs whose query string *is*
the credential — so the URL is logged, never returned, and the message names the
model instead. By the same rule the failure `detail` carries facts about *what
happened* (the digest and byte count that actually arrived) plus `size_bytes`,
which the model resource already publishes as `installation.total_bytes`. It
never carries a field copied out of the private `artifact` block.

**An incomplete or hash-mismatched artifact is never installed and never
loadable** (ARCHITECTURE.md §9). The download streams to a temporary `.part`
file, the pinned SHA-256 is verified *before* anything is published, the file is
`fsync`ed so the bytes are on stable storage rather than in the page cache, and
only then is it `os.replace`d into place (with the containing directory synced
afterwards where the platform allows). Nothing ever re-hashes installed weights,
so a file that is published torn would be loaded silently forever — the sync is
what stops a power loss just after a "successful" install from producing one.
The `.part` is removed on every failure path, including cancellation and
shutdown. A pinned checksum is
*enforced*, never trusted: a checkpoint host that is renamed or taken down
serves a 404 page, not weights, and installing something plausible-looking would
be the exact failure this exists to prevent.

Weights live under `Settings.models_dir` at
`{models_dir}/weights/{model_id}/weights.bin` and are **never committed to the
repository**. Resumable downloads, mirrors and in-place updates are out of scope.

**Which models a mode offers is unaffected by installation state.**
`GET /separation-modes` still lists every catalogued model's tier, installed or
not (feature 010's open question, deliberately left open until 026).

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
mode ID; tier labels are humanized tier IDs. A mode with **no** models — every one of them a
hidden development fixture — is not derived at all, so an empty
`quality_options` list is never served.

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
  "device_id": "cuda:0",
  "stereo_handling": "as_is"
}
```

`device_id` optional — backend picks the best device by default.

`stereo_handling` optional, `"as_is"` by default — see *Stereo handling* below.

`Job`:

```json
{
  "id": "01JOB...",
  "audio_id": "01ABC...",
  "configuration": { "audio_id": "01ABC...", "mode_id": "vocals", "quality_id": "high_quality", "device_id": "cuda:0", "stereo_handling": "as_is" },
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

**`stereo_handling` says what to do with the input's stereo image** before it
is separated, and is a statement about the *user's audio* rather than an
inference parameter (ARCHITECTURE.md §1; the argument is in
`docs/features/041-mono-folddown-option.md`).

| value | effect |
| --- | --- |
| `as_is` | **Default.** The decoded mixture is separated untouched — bit-for-bit the behaviour before feature 041 |
| `mono` | The mixture is folded to `(L + R) / 2` before separating. Every stem then comes back with **one channel**, and each `Stem.channels` says so |

It is never applied unasked and never inferred from the audio: a server does not
detect a wide stereo mix and quietly correct it. A mono source is unaffected by
either value. Omitting the field is exactly equivalent to sending `"as_is"`, so
a client written before this feature is unchanged; the field is always present
on a `Job`'s echoed `configuration`.

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
| `separation_mode_not_found` | 404 | `mode_id` is not one of the derived separation modes — including a mode whose only models were hidden development fixtures |
| `quality_option_not_found` | 404 | `quality_id` is not an option of that mode — including a tier backed only by a hidden development fixture |
| `device_not_found` | 404 | `device_id` is not a detected compute device |
| `model_device_unsupported` | 409 | the resolved model's `capabilities` do not include the resolved device's `backend`. `detail` carries `model_id`, `device_id`, `device_backend` and `supported_backends` |
| `model_weights_missing` | 409 | the resolved model is catalogued but its weights are not installed. `detail` carries `model_id` |
| `separator_unavailable` | 501 | no separator implementation exists for the resolved model's architecture |
| `model_weights_invalid` | 500 | the installed weights do not load into this build's architecture |
| `model_parameters_invalid` | 500 | the resolved model's catalog entry carries inference parameters this build cannot use |
| `job_not_found` | 404 | unknown `job_id` (get/cancel) |
| `service_unavailable` | 503 | the job manager is shutting down (create/cancel) |

A malformed create body is the standard `validation_error` (422). References are
resolved in the order audio → mode → quality → device → separator, so the first
unresolvable one is what the client is told about.

The last two are **deployment faults, not client mistakes**: a corrupted install,
or a catalog entry that does not match its checkpoint. They are `500`s because
there is nothing a client can do about either, and they surface here — rather
than mid-job — because creating a job is where a separator is first built.

### Device selection and model capabilities

A model manifest declares which compute backends its weights run on
(`capabilities`, ARCHITECTURE.md §9), and job creation consults it. The two
cases differ deliberately:

- **`device_id` was given.** It is honoured or refused with
  `model_device_unsupported` — never silently swapped for a different device.
- **`device_id` was `null`** ("let the backend pick"). The first *detected*
  device the model supports is chosen, still preferring CUDA over CPU. Only when
  no detected device can run the model at all is `model_device_unsupported`
  returned.

`model_weights_missing` follows from the same principle: a model whose
downloadable artifact has not been installed cannot run, and that is knowable at
create time. Install it with `POST /models/{model_id}/install` and retry; the
model's `installation` block on `GET /models` says which models need it. Quality
options are **not** hidden for uninstalled models — a client that wants to
present an "Install" affordance has everything it needs from `GET /models`.

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

`Accept-Ranges`, `Content-Range`, `ETag`, `Last-Modified` and
`Content-Disposition` are listed in `Access-Control-Expose-Headers`, so a
cross-origin fetch can read them — CORS otherwise hides every response header
outside the "simple" set, which would leave a page that talks to `:8000`
directly able to receive a range and unable to see which range it got. Allowed
origins come from `STRATICATE_CORS_ORIGINS` (default: the Vite dev server).

Setting `STRATICATE_CORS_ORIGINS='["*"]'` allows any origin and **disables
credentialed CORS** (no `Access-Control-Allow-Credentials`), because `"*"` plus
credentials would make Starlette echo each caller's own `Origin` back and let
every origin read credentialed responses. Name origins explicitly to keep
credentials enabled.

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
| `export_timed_out` | 504 | *(export only)* FFmpeg exceeded its bounded run time; `detail` carries `job_id` and `format` |
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

`export_timed_out` is not one of those reasons but a code of its own:
`export_failed` says the encode was attempted and failed, which is a statement
about the audio or the disk, while a timeout is a statement about the server.
The remedies differ, so the codes do.

## Timeouts

Every FFmpeg and ffprobe invocation runs with a wall-clock bound —
`STRATICATE_FFMPEG_TIMEOUT_SECONDS`, default 600, or whatever `Settings` the
running application was built with; the bound is passed down to each call site
rather than re-read from the environment. The subprocesses run in
worker threads drawn from one shared pool, so an unbounded one is not a slow
request but a thread held forever, and enough of them would starve probing and
separation as well as the export that started them.

On expiry the subprocess is killed and the surface that started it reports its
own code — never a generic one, and never a code that means something else:

| surface | code | status |
| --- | --- | --- |
| `POST /audio` (ffprobe) | `audio_probe_timed_out` | 504 |
| a separation job's decode (FFmpeg) | `audio_decode_timed_out` | *(job `error.code`)* |
| `GET /jobs/{job_id}/export` (FFmpeg) | `export_timed_out` | 504 |

`audio_decode_timed_out` never appears as an HTTP status: a job that times out
while decoding fails, and the code arrives in the job's `error.code` (and in
the `job_failed` WebSocket event), alongside `audio_decode_failed` for input
that genuinely could not be decoded.

## Codes a running separation can report

These reach a client as a failed job's `error.code` (and in the `job_failed`
event), not as the status of the request that created the job:

| code | when |
| --- | --- |
| `audio_decode_failed` | FFmpeg could not decode the input |
| `audio_decode_timed_out` | FFmpeg exceeded its bounded run time (above) |
| `separation_mode_mismatch` | the separator was handed a configuration for a mode it does not serve — a wiring bug, reported rather than silently producing the wrong stems |
| `compute_device_unavailable` | the device the job resolved to is no longer usable by this process (for example a CUDA runtime that has gone away since detection) |
| `model_weights_missing` | the weights disappeared between job creation and the run |

`model_weights_invalid` and `model_parameters_invalid` are **not** in this table:
a separator is built when a job is *created*, so those two are answers to
`POST /jobs` (both `500`) and are listed with the other job-creation errors
above. They reach a job's `error.code` only if a build somehow first succeeds and
a later one does not.
