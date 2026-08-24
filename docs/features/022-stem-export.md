# [022] Stem export (WAV PCM24 / float32 / FLAC)

Branch: `022-stem-export`
Status: PR OPEN
Dependencies: 021
PR: #…

## Objective

A completed job's stems are downloadable in the format the user asked for.
`GET /api/v1/jobs/{job_id}/export` transcodes them with FFmpeg to 24-bit WAV,
32-bit float WAV or FLAC and hands back either the bare audio file (exactly one
stem) or a zip of the stems plus a `separation.json` manifest (more than one).
This is the last backend piece of milestone M1; the export UI (024) has
everything it needs.

## Scope

- `backend/src/straticate/api/export.py` — a new
  `APIRouter(prefix="/jobs", tags=["export"])` with one `async def` handler,
  the `ExportFormat` query enum, the format → `(container, codec, suffix)`
  table, the `ExportManifest` model written as `separation.json`, the
  artifact-cache naming, and the threaded FFmpeg/zip build.
- `backend/src/straticate/api/job_outputs.py` — the shared lookup 021's doc
  asked for: `completed_job()`, `stem_source_path()` and the
  `result_not_available` / `stem_not_found` / `stem_file_missing` builders,
  promoted out of `api/results.py` so both routers use one definition.
- `backend/src/straticate/api/results.py` — uses those helpers; no behaviour
  change (its tests are untouched and still pass).
- `backend/src/straticate/main.py` — one import and one `include_router` line.
- `docs/contracts/rest-api.md` — the "Results, stems, export" section gained an
  "Export" subsection (parameters, the single-vs-multiple rule,
  `separation.json`, the bit-depth honesty note) and two error-code rows.
- `frontend/src/api/generated/api.d.ts` — regenerated for the new path.

## Out of scope

- `backend/src/straticate/schemas/**` and `models/schemas/**`. `ExportFormat`
  and `ExportManifest` are the export router's own types, not shared contract
  entities; **no schema changed**.
- The job manager, event hub, separators, registry, telemetry sampler and
  `api/jobs.py` — consumed, never edited. `api/results.py` only for the shared
  lookup above.
- Every frontend file other than the regenerated `api.d.ts` — feature 023 owns
  the frontend.
- A retention/cleanup policy, a persistent job registry, `304`/conditional
  requests — still unclaimed, as 021 left them.
- The export UI (024). Real ML models, PyTorch.

## Expected modules/files

- `backend/src/straticate/api/export.py` · `api/job_outputs.py` ·
  `api/results.py` · `main.py`
- `backend/tests/test_api_export.py`
- `frontend/src/api/generated/api.d.ts`
- `docs/contracts/rest-api.md` · `docs/features/022-stem-export.md` ·
  `ROADMAP.md`

## Acceptance criteria

- [x] All three formats produce files a decoder accepts: `ffprobe` reports
      `pcm_s24le`/24-bit for `wav_pcm24`, `pcm_f32le`/32-bit for
      `wav_float32` and `flac` for `flac`, each with the source's sample rate,
      channel count and duration.
- [x] One stem → the bare audio file with `Content-Disposition: attachment`
      and the media type for its suffix; more than one → a zip whose entries
      are exactly the requested stems plus `separation.json`.
- [x] `separation.json` parses and embeds the job's `SeparationResult`
      verbatim — byte-identical to what `GET /jobs/{id}/result` serves, and it
      round-trips through `SeparationResult.model_validate`.
- [x] Omitting `stems` exports all of them; four-stem and two-stem jobs both
      work and nothing is hardcoded to two stems.
- [x] Documented errors: `job_not_found` (404), `result_not_available` (409
      with the state in `detail`), `stem_not_found` (404 with
      `available_stems`), `stem_file_missing` (404), `export_failed` (500),
      unknown `format` → `validation_error` (422).
- [x] Traversal attempts inside `stems` are a clean `stem_not_found` — never a
      500, never a file from outside the job's stem directory (13 parametrised
      cases).
- [x] The event loop stays responsive during an export: two tests prove it
      (see "How the loop is kept free" below).
- [x] A repeated identical export serves the cached artifact without
      re-transcoding (asserted by counting FFmpeg invocations), and a `.part`
      file is never served.
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest`
      all green (436 backend tests, 55 of them new); the frontend still
      formats, lints, type-checks, tests and builds with the regenerated
      types.

## Required tests

`backend/tests/test_api_export.py` (55 tests) follows `test_api_results.py`'s
conventions: the real application with the lifespan running on the test's own
event loop, a real `JobManager` and `EventHub`, an injected `SeparatorRegistry`
whose `FakeSeparator` has every simulated delay zeroed, generated WAV fixtures
from `tests/audio_fixtures.py`, and `asyncio.Event` gating everywhere — no
`sleep()` as synchronization. The transcodes are real (CI installs FFmpeg) and
the output is verified with **ffprobe**, not trusted.

Covered: every format probed for codec, bit depth, sample rate, channel count
and duration, both inside an archive and as a single file; the default format;
an unknown format as `validation_error`; the zip's exact entry names and count
for a two-stem and a four-stem job; a subset of two; the four stems in an
archive proven mutually distinct; stem order and duplicates producing the same
export; a repeated single stem still being a bare file; whitespace around
names; a single-stem export proven to carry no manifest; the manifest's field
set, its embedded result compared against `GET /result` and re-validated
through `SeparationResult`, its timezone-aware `exported_at`, and its `stems`
listing only what was exported; `job_not_found`; `result_not_available` for
`preparing`/`decoding`/`separating`/`encoding` plus `cancelled` and `failed`;
`stem_not_found` with `available_stems`; 13 traversal / absolute-path /
URL-encoded / empty-entry selections; four blank `stems` values as
`validation_error`; a stem file deleted after completion (its sibling still
exports); a mocked and a **real** FFmpeg failure as `export_failed`; a failed
export leaving no `.part` behind and rebuilding cleanly afterwards; the cache
hit, the cache key covering format and selection, the artifact file names on
disk, a planted stale `.part` never being served, the digest fallback for a
many-stem model; and the two event-loop liveness tests. Every error envelope's
exact shape is asserted.

## Notes / decisions

### How the loop is kept free, and how that is proved

FFmpeg and the zip writer are the "expensive work in a request handler"
AGENTS.md principle 4 and ARCHITECTURE.md §14 forbid: a four-stem, ten-minute
job is seconds of CPU, and blocking the loop would stall the job worker, the
event dispatcher and every WebSocket client. Every blocking step therefore runs
in a worker thread through `asyncio.to_thread` — the transcode, the zip build
and even the final `os.replace`. The handler itself only awaits.

`asyncio.to_thread` + `subprocess.run` was chosen over
`asyncio.create_subprocess_exec` deliberately: it is exactly the pattern
`audio/probe.py` and `inference/pcm.py` already use for their subprocesses, and
it does not depend on the event loop implementation having subprocess support
(Windows' selector loop does not).

Two tests prove it, and both were confirmed to **fail** when the `to_thread`
calls are replaced with direct calls:

- `test_other_requests_are_served_while_an_export_is_in_flight` parks the
  transcode's *worker thread* on a `threading.Event`, then completes a
  `GET /health` and a `GET /jobs/{id}/result` while it is parked.
- `test_the_job_worker_keeps_running_during_an_export` runs a whole separation
  to its `job_completed` event while an export is parked — which exercises the
  manager's worker and the event dispatcher, not just the HTTP layer.

Neither test sleeps: the "the thread is now inside the transcode" signal is a
`threading.Event` awaited through `asyncio.to_thread`.

### Why one stem is not a zip

The contract was ambiguous here; the decision is that the number of stems
*requested* decides the shape. One stem is one file the user drops into a DAW,
so wrapping it in a zip purely to carry a manifest would make the common case
worse. The cost is that a single-stem export has no `separation.json` — a
documented choice (see `docs/contracts/rest-api.md`), not a bug. Everything the
manifest holds is still available from `GET /jobs/{id}/result`.

The count is taken **after** deduplication, so `?stems=vocals,vocals` is one
stem and therefore a bare file.

### Why `separation.json` embeds `SeparationResult` verbatim

The archive documents itself with the contract the API already publishes: the
`result` key is byte-for-byte what `GET /jobs/{id}/result` returns, so a
consumer parses it with the type it already has and no parallel "export
result" contract exists to drift. The wrapper adds only what the *export*
knows: the `format`, the stem names actually in the archive (which may be a
subset of `result.stems`), an `exported_at` timestamp, and the `model_id`.

`ExportManifest` lives in `api/export.py`, not in `schemas/`: no route returns
it, so it stays out of the OpenAPI component list. `ExportFormat` lives there
too for the same reason — it is one endpoint's query vocabulary, not a shared
entity — though as a query enum FastAPI does register it as a component, which
is exactly what 024's format picker wants.

### Bit depth: honest, not magical

The separator writes 16-bit PCM WAV (feature 014), so `wav_pcm24` and
`wav_float32` change the container encoding and add **no information**. A
24-bit export does not recover detail the stems never had. This is stated in
the contract as well as here, because a user could reasonably assume otherwise.
It stops being true when a real separator (026) produces higher-precision
output; the formats exist now so the export path is complete and so a user
whose downstream tools need 24-bit or float files gets them.

Sample rate and channel count are never passed to FFmpeg, so the source's are
preserved exactly — the export changes the encoding and nothing else.

### The artifact cache

A completed job's stems are immutable, so the built file is a safe cache. It is
written under `{data_dir}/jobs/{job_id}/exports/` with a deterministic name
derived from the format and the **sorted** stem list
(`wav_pcm24-instrumental-vocals.zip`, `flac-vocals.flac`), so the order the
client listed its stems in cannot produce a second copy, and a repeated
download is instant. A stem list long enough to make the name unwieldy falls
back to a SHA-256 digest, keeping path lengths bounded on every platform.

Every build writes to a `.part` file **unique to that call**
(`{name}.{uuid4}.part`) and publishes it with `os.replace`. That is 014's
discipline with one addition: two concurrent identical exports cannot share a
partial file, and because only the final name is ever served, a leftover
`.part` can never be handed to a client. A failed build unlinks its `.part`, so
the next request rebuilds rather than serving rubbish.

### Why a separate router file

`api/export.py` rather than a third handler in `api/results.py`: the export
path has a different dependency set and a different failure mode (a
subprocess), and 021 is a merged, tested module that this feature should not
churn. The `/jobs` prefix keeps the URL where the contract puts it; the
`export` tag keeps it a distinct group in the OpenAPI document.

### The shared lookup

021's doc asked that its `_completed()` be promoted rather than re-derived.
`api/job_outputs.py` now holds it as `completed_job()`, together with
`stem_source_path()` (result-validated name → an existing file, raising
`stem_not_found` or `stem_file_missing`) and the three error builders. Both
routers import from there, so the four status codes have exactly one
definition. `api/results.py` shrank by ~70 lines with no behaviour change —
its 52 tests were not touched and still pass.

## Interfaces for downstream features

### For 024 (export UI)

```text
GET /api/v1/jobs/{job_id}/export?format={format}&stems={a,b,c}
```

Both query parameters are optional.

- **`format`** — `wav_pcm24` (default), `wav_float32` or `flac`. The generated
  types expose it as `components["schemas"]["ExportFormat"]`, so build the
  format picker from that union rather than a hand-written list. Label the
  24-bit and float options honestly: the stems are 16-bit today, so those
  formats change the encoding without adding information.
- **`stems`** — a comma-separated list. Build it by joining the selected stem
  names with `,` and nothing else (no spaces needed; they are tolerated). The
  names come from `SeparationResult.stems` — from `GET /jobs/{id}/result` or
  the `Job.result` the UI already holds — **never** from a hardcoded list, so
  a two-stem and a six-stem model both work. **Omit the parameter entirely to
  export everything**; do not send `stems=` with an empty value, which is a
  422.

The response shape depends only on how many *distinct* stems were selected:

| selection | body | `Content-Type` | filename in `Content-Disposition` |
| --- | --- | --- | --- |
| 1 | the audio file | `audio/wav` / `audio/flac` | `{job_id}-{format}-{stem}.{ext}` |
| 2+ or omitted | a zip: `{stem}.{ext}` per stem + `separation.json` | `application/zip` | `{job_id}-{format}.zip` |

Both are `Content-Disposition: attachment`, so the simplest correct UI is an
`<a href>` (or `window.location.assign`) straight at the URL — the browser
downloads it with the offered filename and no blob handling is needed. If you
need progress or error rendering instead, `fetch()` the URL and check the
status before turning the body into a blob; the error body is the standard JSON
envelope.

A single-stem export deliberately contains no `separation.json`. If the UI
wants to show what was exported, it already has the `SeparationResult`.

Errors the UI must render (all the standard envelope,
`{"error": {code, message, detail}}`):

| code | status | what the UI should do |
| --- | --- | --- |
| `job_not_found` | 404 | the job is gone (a restart — records are in-memory); offer to re-run |
| `result_not_available` | 409 | read `detail.state`: still running → wait for the WebSocket `job_completed` event and re-enable the button; `cancelled`/`failed` → say so and hide export |
| `stem_not_found` | 404 | the selection is stale; refresh from `detail.available_stems` |
| `stem_file_missing` | 404 | `detail.stem`'s audio is gone from disk; offer to re-run the job |
| `export_failed` | 500 | the transcode failed; `detail.reason` is diagnostic (not user-facing). Offer a retry |
| `validation_error` | 422 | a UI bug — an unknown `format`, or `stems=` sent empty instead of omitted |

The first download of a given format/selection runs FFmpeg (seconds for a long
four-stem job), so show a pending state on the button; **repeats of the same
format and selection are served from a cached artifact and return
immediately**, and the selection is order-insensitive, so re-selecting the same
stems in a different order still hits the cache.

## Known limitations

- **Export artifacts are never cleaned up.** Every distinct format/selection a
  user downloads leaves a file under `{data_dir}/jobs/{job_id}/exports/`
  forever, on top of the stems themselves. This is consistent with 021's
  existing note that nothing has a retention policy — the whole data directory
  grows without bound and no feature owns cleanup yet. Called out here rather
  than improvised.
- **The cache is never invalidated.** It does not need to be while a completed
  job's stems are immutable, but if a future feature ever rewrites a completed
  job's stems in place, it must delete that job's `exports/` directory.
- **`export_failed` is coarse.** An FFmpeg non-zero exit, a full disk and a
  zip failure all produce the same code; the distinguishing text is in
  `detail.reason`, which is diagnostic rather than something a UI should
  parse.
- **No `304`/conditional requests.** The response carries `ETag` and
  `Last-Modified` (Starlette's `FileResponse`), but `If-None-Match` does not
  short-circuit — the same gap 021 documented for stem streaming.
- **Transcodes run one stem at a time.** A four-stem export is four sequential
  FFmpeg invocations rather than four concurrent ones. This keeps CPU pressure
  bounded next to a running separation; parallelising it is a measurable
  improvement nobody has asked for yet.
- **Job records remain in-memory.** As with 021, a restart loses every job
  record while its stems and exports stay on disk.
