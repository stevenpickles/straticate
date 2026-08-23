# [006] Audio upload, validation, storage, and metadata extraction

Branch: `006-audio-upload`
Status: PR OPEN
Dependencies: 005
PR: #6

## Objective

A working `/api/v1/audio` resource: multipart upload with validation,
temporary storage under the configured data directory, ffprobe-based
metadata extraction from the actual media, fetch, and delete.

> **Note — feature 007 folded in.** The `AudioFile` contract requires real
> probed metadata, so ledgered feature 007 (ffprobe metadata extraction) is
> deliberately absorbed by this feature. The 007 ledger row is marked
> `MERGED` with a "folded into 006" note.

## Scope

- `backend/src/straticate/audio/storage.py` — `AudioStore`: files under
  `{data_dir}/audio/{audio_id}/original{ext}`, in-memory registry of
  `AudioFile` records, ULID string IDs (`python-ulid`), `get`/`delete`
  (delete removes the upload's directory). **First real consumer of
  `Settings.data_dir`** (default `./data` relative to CWD; directories
  created lazily).
- `backend/src/straticate/audio/probe.py` — `probe_audio(path)` running
  `ffprobe -v error -print_format json -show_format -show_streams` via
  `asyncio.to_thread` (never blocks the event loop). Maps duration,
  container (first `format_name` token), codec, channels, sample rate,
  nullable bit depth (`bits_per_raw_sample`/`bits_per_sample`), nullable
  bit rate. The filename extension is never trusted; files ffprobe cannot
  decode (or with no audio stream) are rejected.
- `backend/src/straticate/api/audio.py` — router under `/api/v1`:
  - `POST /audio` (multipart `file`) → 201 + `AudioFile`. Validation
    order: size limit (new `Settings.max_upload_bytes`, default 1 GiB,
    streamed to disk in 1 MiB chunks and aborted at the limit) → probe →
    register. Errors: `audio_too_large` (413), `audio_not_decodable`
    (422); a missing `file` part yields FastAPI's standard
    `validation_error` (422) envelope.
  - `GET /audio/{audio_id}` → `AudioFile`; 404 `audio_not_found`.
  - `DELETE /audio/{audio_id}` → 204; 404 `audio_not_found`.
- `AudioStore` is created in `create_app()`, exposed via `app.state` and
  `Depends` accessors (`get_audio_store`, `get_app_settings`).
- New settings wired for real: `data_dir` (consumed by `AudioStore`) and
  `max_upload_bytes` (`STRATICATE_MAX_UPLOAD_BYTES`).

## Out of scope

- Frontend anything; jobs; decode-to-PCM (job pipeline concern).
- Persistence of the registry across restarts; cleanup daemons.

## Expected modules/files

- `backend/src/straticate/audio/{__init__.py,storage.py,probe.py}`
- `backend/src/straticate/api/audio.py`
- `backend/src/straticate/config.py` (adds `max_upload_bytes`)
- `backend/src/straticate/main.py` (store creation, router registration)
- `backend/tests/test_audio.py`
- `docs/contracts/rest-api.md` (status/error-code clarifications)

## Acceptance criteria

- [x] `POST /audio` accepts multipart uploads, streams to
      `{data_dir}/audio/{id}/original{ext}`, probes with ffprobe, returns
      201 + `AudioFile` with real metadata
- [x] Size limit enforced while streaming → 413 `audio_too_large`
- [x] Undecodable input → 422 `audio_not_decodable`; no files left behind
- [x] `GET`/`DELETE` behave per contract; unknown ID → 404
      `audio_not_found`; delete removes record and directory
- [x] All quality gates green (ruff format/check, pyright strict, pytest)

## Required tests

- Successful upload returns correct metadata (duration ≈ 1.0 s, 2
  channels, 44100 Hz, sane container/codec, bit_depth 16)
- Text file upload → 422 `audio_not_decodable`
- Oversize upload (tiny `max_upload_bytes` override) → 413
  `audio_too_large`
- GET returns the same record; GET/DELETE unknown ID → 404
- DELETE removes record and files (second GET 404s, directory gone)
- Lying extension (`.mp3` containing WAV bytes) still probes as WAV

Fixtures are generated at test time with the stdlib `wave` module (no
committed audio binaries; ffmpeg is not needed to generate them — ffprobe
only probes them).

## Notes / decisions

- Feature 007 is folded into this feature (see note above).
- New runtime dependencies: `python-ulid` (IDs) and `python-multipart`
  (FastAPI multipart parsing).
- The registry is in-memory only; files under `{data_dir}/audio` from a
  previous run are orphaned. Acceptable for now — a persistent registry or
  startup sweep can be a later feature.
- Disk writes in the upload loop are synchronous per 1 MiB chunk; fine for
  the local-first scale of this app.
- ffprobe absent from PATH would surface as a 500 `internal_error`; CI and
  the documented dev environment install FFmpeg.
