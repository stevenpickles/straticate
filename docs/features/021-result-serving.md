# [021] Result management + stem serving

Branch: `021-result-serving`
Status: PR OPEN
Dependencies: 014, 015
PR: #20

## Objective

A completed job's outputs are reachable over HTTP.
`GET /api/v1/jobs/{id}/result` returns the `SeparationResult` the separator
produced, and `GET /api/v1/jobs/{id}/stems/{stem}` streams that stem's audio
with byte `Range` support, so the browser's stem player (023) can seek without
downloading whole files. Export (022) builds on the same lookup rules.

## Scope

- `backend/src/straticate/api/results.py` — a new `APIRouter(prefix="/jobs",
  tags=["results"])` with two `async def` handlers, the shared
  `_completed()` lookup, the `stem_media_type()` suffix mapping, and the three
  errors this feature introduces.
- `backend/src/straticate/main.py` — one import and one `include_router` line.
- `docs/contracts/rest-api.md` — the "Results, stems, export" section gained
  concrete status codes, the full `Range` behaviour table and an error table.
- `frontend/src/api/generated/api.d.ts` — regenerated for the two new paths.

## Out of scope

- `backend/src/straticate/schemas/**` and `models/schemas/**`.
  `SeparationResult` and `Stem` already existed and are returned as-is; no
  schema changed.
- `backend/src/straticate/api/jobs.py`, `jobs/resolution.py`,
  `inference/registry.py` (015's) and the `telemetry/` package plus the
  `lifespan` function (019's) — consumed or untouched, never edited.
- Export, format conversion, zip bundling and `separation.json` — **022**.
- The stem player and any frontend UI — 023/024.
- A retention/cleanup policy for job directories, and persistence of job
  records across restarts (see Known limitations).
- Real ML models, PyTorch.

## Expected modules/files

- `backend/src/straticate/api/results.py`
- `backend/src/straticate/main.py`
- `backend/tests/test_api_results.py`
- `frontend/src/api/generated/api.d.ts`
- `docs/contracts/rest-api.md` · `docs/features/021-result-serving.md` ·
  `ROADMAP.md`

## Acceptance criteria

- [x] `GET /jobs/{id}/result` returns the completed job's `SeparationResult`
      exactly as the contract documents (and byte-for-byte the same object
      `GET /jobs/{id}` carries under `result`).
- [x] Unknown job → 404 `job_not_found`; incomplete/cancelled/failed job → 409
      `result_not_available` with the current state in `detail`.
- [x] `GET /jobs/{id}/stems/{stem}` streams a real, playable WAV whose bytes
      are byte-identical to the file the separator wrote.
- [x] `Range` is supported: `206` + correct `Content-Range` + the exact byte
      slice; open-ended and suffix ranges work; an unsatisfiable range is
      rejected with `416` + `Content-Range: bytes */{size}`; a plain `GET`
      advertises `Accept-Ranges: bytes`.
- [x] Unknown stem → 404 `stem_not_found`; a result-claimed stem whose file is
      gone → 404 `stem_file_missing`; traversal attempts → clean 404, never a
      file outside the job's stem directory.
- [x] Works for a four-stem job as well as a two-stem one — nothing is
      hardcoded to two stems, and nothing in the module names a stem, a mode
      or an architecture.
- [x] `frontend/src/api/generated/api.d.ts` regenerated (two new paths, no
      other change) and the frontend still builds.
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest`
      all green.

## Required tests

`backend/tests/test_api_results.py` (52 tests) follows `test_api_jobs.py`'s
conventions: the real application with the lifespan running on the test's own
event loop, a real `JobManager` and `EventHub`, an injected
`SeparatorRegistry` whose `FakeSeparator` has every simulated delay zeroed,
generated WAV fixtures from `tests/audio_fixtures.py`, and `asyncio.Event`
gating everywhere — no `sleep()` as synchronization.

Covered: the result of a completed job for the two-stem **and** four-stem mode,
and its equality with the job record's `result`; 404 for an unknown job; 409
for every non-completed state — `queued` (a second job held behind a gated
first), `preparing`, `decoding`, `loading_model`, `separating`,
`post_processing`, `encoding` (a `StageGatedSeparator` parks the job in each),
`cancelled` and `failed`; a full stem download byte-compared against the file
on disk for every stem of both modes, with the four served files proven
mutually distinct; `Content-Type`, `Accept-Ranges`, `Content-Disposition`,
`ETag`, `Last-Modified`; the media type derived from the suffix (`.wav`,
`.WAV`, `.flac`, unknown); `Range` — first 100 bytes, a mid-file slice, an
open-ended range, a suffix range, an unsatisfiable range, a malformed range,
and a ranged request for a missing stem; unknown stem; a stem file deleted
after completion (its sibling still serves) and a wholly removed job
directory; a file planted in the stem directory that the result does not list;
14 URL-encoded traversal stem names; 9 raw unnormalized URL paths driven
straight at the ASGI app; and the exact error-envelope shape for every code.

## Notes / decisions

### `Range` — Starlette already does it, correctly

The pinned `starlette==1.6.0` `FileResponse` implements the whole byte-range
protocol, so this feature writes **no** range code:

- `Accept-Ranges: bytes`, `Content-Length`, `ETag` and `Last-Modified` on every
  response (`__init__` / `set_stat_headers`);
- a single `Range` → `206` with `Content-Range: bytes {start}-{end}/{size}` and
  exactly that slice, streamed in 64 KiB chunks;
- `bytes=N-` and `bytes=-N` both parse (`_parse_ranges`);
- multiple ranges → `206 multipart/byteranges` with a generated boundary;
- an unsatisfiable range → `416` with `Content-Range: bytes */{size}`;
- an unparsable one → `400`;
- `If-Range` is honoured against the `ETag`/`Last-Modified`, falling back to
  the whole file.

The tests assert that behaviour against real bytes rather than trusting it, so
a pin bump that regressed it would fail CI here. Writing the header handling by
hand would have duplicated a correct, streaming, well-tested implementation.

**One deliberate envelope exception:** the `416` and `400` responses are
Starlette's `PlainTextResponse`, not the JSON error envelope, because they are
produced below the application by the byte-range layer. That is the right shape
for a media client — it wants the RFC 9110 status and `Content-Range`, not a
JSON body — and it is documented in the contract. Every *application* error on
these routes (404/409) uses the envelope.

### Why one 409 code for every non-completed state

`result_not_available` covers "still running", "cancelled" and "failed" alike,
with the job's `state` in `detail`. The client's situation is identical in all
three — there is nothing to fetch — so a second code would only give every
client a second branch to write, while the `state` field gives the UI
everything it needs to phrase the message ("still separating…" vs. "you
cancelled this job"). `GET /jobs/{id}` remains the source of truth for the full
record, including a failed job's `error`. This mirrors 015's reasoning for
reusing `audio_not_found` rather than minting a code per drift case.

### Why the *result* is the authority on stem names, not the filesystem

`stem_name` is checked against `job.result.stems` before any path is built.
Three consequences, all deliberate:

1. Nothing lists the stem directory, so a leftover or planted file that the
   result does not claim is not servable.
2. Nothing is hardcoded to two (or four) stems: a six-stem model served by a
   catalog-only entry works with no code change, exactly as 015 arranged.
3. Path traversal is not a special case to defend against — `../secret` is
   simply not one of the job's stem names, so it exits at the same `404
   stem_not_found` as any other typo, long before a path exists.

Path construction then goes through `inference/layout.stem_path()`, which
rejects anything not matching `^[a-z][a-z0-9_]*$`, and uses `job.id` (the
manager's own key) rather than the URL's `job_id` string. The `ValueError` arm
is therefore unreachable in practice and is mapped to the same 404 rather than
being allowed to become a 500.

### Why `stem_file_missing` is a 404 and not a 500

Job records are in-memory while stems are on disk, so the two can drift: a
previous process's job directory is orphaned (014's known limitation), or a
directory is removed underneath a live job. From the client's point of view the
resource is gone — that is a 404, with its own code so a player can distinguish
"you asked for a stem this job never had" from "the audio has disappeared" and
offer to re-run the job.

### Why a separate router file

The endpoints are `/jobs/{id}/…`, but they live in `api/results.py` with
`APIRouter(prefix="/jobs", tags=["results"])` rather than in `api/jobs.py`.
They are a different resource with a different dependency set (no catalog, no
device detector, no separator registry — just the manager and the settings),
and keeping them separate meant this feature never touched the file feature 019
was editing in parallel. The `/jobs` prefix keeps the URLs where the contract
puts them; the `results` tag keeps them a distinct group in the OpenAPI
document.

## Interfaces for downstream features

### For 023 (stem player)

```text
GET /api/v1/jobs/{job_id}/result            -> SeparationResult
GET /api/v1/jobs/{job_id}/stems/{stem_name} -> audio bytes
```

- The stem list to render comes from `SeparationResult.stems` (which also
  carries each stem's `duration_seconds`, `sample_rate_hz` and `channels`) —
  or from `Job.result` if the job record is already in hand. Never hardcode
  stem names or a stem count.
- Each stem URL is directly usable as an `<audio src>` or `HTMLAudioElement`
  source: the response is `audio/wav`, `Content-Disposition: inline`, and
  advertises `Accept-Ranges: bytes`, so the browser's own media stack will
  issue ranged requests and let the user seek before the file has finished
  downloading. A `fetch()` + `decodeAudioData()` path for Web Audio works the
  same way; so does a manual `Range: bytes=start-end` request, which returns
  `206` with exactly `end - start + 1` bytes and a `Content-Range` header.
- Responses carry an `ETag` and `Last-Modified`, so repeat playback is
  cache-friendly and `If-Range` reseeks are honoured.
- A `409 result_not_available` means "not yet" (or "never"): read
  `error.detail.state` to decide between waiting for the WebSocket
  `job_completed` event and telling the user the job was cancelled or failed.
- `404 stem_file_missing` means the job record outlived its files (a restart,
  or a manual cleanup): offer to re-run rather than retrying the URL.

### For 022 (export)

- The same two lookups are the export path's front door: fetch the job from
  the manager (which raises `job_not_found`), then require
  `state == completed` with a populated `result` or raise
  `result_not_available` (409) carrying the state. `api/results.py`'s
  `_completed()` is exactly that; promote it to a shared helper rather than
  re-deriving the codes, and validate any requested `stems=` list against
  `result.stems` for `stem_not_found`.
- Source files are `stem_path(settings.data_dir, job.id, stem)` —
  `{data_dir}/jobs/{job_id}/stems/{stem}.wav`, 16-bit PCM WAV — and
  `job_output_dir(settings.data_dir, job.id)` is the natural place to write
  `separation.json` and any transcoded or zipped artifact.
- `STEM_MEDIA_TYPES` in `api/results.py` maps a file suffix to the media type
  served. Adding FLAC output to the export path means adding entries there, not
  changing a handler; the `.flac` entry is already present.
- Export is a *download*, so it should send `Content-Disposition: attachment`
  — the streaming route here is deliberately `inline`.

## Known limitations

- **No retention or cleanup policy.** Nothing ever deletes a job's output
  directory; disk use grows with every job. Deleting the input audio
  (`DELETE /audio/{id}`) does not remove the stems derived from it. Unclaimed
  work, called out here rather than improvised.
- **Job records remain in-memory.** After a restart every job is gone while its
  stems are still on disk, so `GET /jobs/{id}/result` 404s for output that
  exists — and, in the other direction, a running server whose data directory is
  cleaned out reports `stem_file_missing`. A persistent job registry is still
  unclaimed (014 and 015 both note it).
- **Multi-range requests are answered as `multipart/byteranges`** because
  Starlette implements it. No client the application ships sends one, and
  nothing here parses that response; it is untested beyond Starlette's own
  suite.
- **No conditional-request short-circuit.** `ETag`/`Last-Modified` are sent but
  `If-None-Match` does not produce a `304` — `FileResponse` does not implement
  it, and for a local-first tool re-sending a stem is cheap.
