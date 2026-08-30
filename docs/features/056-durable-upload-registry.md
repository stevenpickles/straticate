# [056] Durable upload registry

Branch: `056-durable-upload-registry`
Status: PR OPEN
Dependencies: —
PR: #… (when open)

## Objective

Uploaded-audio records survive a backend restart: `GET /audio/{id}` answers
200 after a restart, and a new separation job can be created against an
upload from a previous run. Before this feature the registry was in-memory
only (feature 006 recorded "a persistent registry or startup sweep can be a
later feature") — every restart orphaned the files already sitting under
`{data_dir}/audio` and made every previously issued `audio_id` a 404.

## Scope

- `backend/src/straticate/audio/storage.py`:
  - A sidecar `{data_dir}/audio/{audio_id}/audio.json` beside
    `original{ext}` — `AudioFile.model_dump_json()` verbatim, the same wire
    shape the API already returns. No second schema.
  - `AudioStore.register()` now writes the sidecar, atomically
    (`audio.json.{uuid}.tmp` → `os.replace`, same directory, no `fsync` —
    the trade-off is documented on the module and on `register()`), and
    only ever after the caller already has a probed, valid upload (a
    rejected upload still leaves neither file nor sidecar behind).
  - A new `AudioStore.load()`: a read-only sweep of `{data_dir}/audio` that
    rebuilds the in-memory registry from whatever sidecars survived the
    previous run. Never repairs or deletes anything — an orphan (sidecar
    without file, or file without sidecar) is left exactly as found, for a
    later feature to surface or prune. A sidecar that fails to parse is
    logged at WARNING and skipped; one bad record never fails startup. A
    stray `*.tmp` from an interrupted write is ignored.
  - `AudioStore.delete()`/`remove_files()` needed no change — the sidecar
    dies with the directory's `shutil.rmtree`, same as the original file.
- `backend/src/straticate/main.py`: one line (plus a comment) in
  `lifespan()` — `audio_store.load()` — added right after the device
  detector's own startup probe, and for the same reason `load()`'s
  docstring gives: `create_app()` runs at **import** for the module-level
  `app` in `main.py` (`uvicorn straticate.main:app`), before either entry
  path has configured logging, so a sweep run from `AudioStore.__init__`
  would print an unformatted WARNING with no timestamp or logger name for a
  corrupt sidecar — exactly the trap feature 029 already documented and
  fixed for the device probe. `load()` therefore runs once per *running*
  application, from the lifespan, after logging exists.
- `backend/tests/test_audio_durability.py` (new): the restart suite below.

## Out of scope

- Durable **job** records (`jobs/*`, `jobs/store.py`) — feature 057's, built
  in parallel on its own branch. This feature does not touch `jobs/*.py`.
- Deletion/disk-usage/prune endpoints (058–060), orphan surfacing or
  pruning, and any frontend change. An orphan left by `load()` is reported
  here (see Known Limitations) but not acted on.
- No schema changes; no OpenAPI regeneration was needed (the public
  request/response shapes of `/audio/*` are unchanged).

## Expected modules/files

- `backend/src/straticate/audio/storage.py`
- `backend/src/straticate/main.py` (one line + comment in `lifespan()`)
- `backend/tests/test_audio_durability.py` (new)
- `docs/features/056-durable-upload-registry.md` (this file)
- `ROADMAP.md` (own ledger row only)

## Acceptance criteria

- [x] `GET /audio/{id}` returns 200 with an identical record after the
      backend process restarts (proved to fail first against the
      unmodified, in-memory-only store — see Notes).
- [x] A new separation job can be created against an `audio_id` from a
      previous run, and completes (proves `jobs/resolution.py`'s
      `resolve_audio` reads the restored record — that module is otherwise
      untouched).
- [x] A sidecar without its original file, or a file without a sidecar,
      is a clean 404 `audio_not_found` at request time — no crash, no
      directory recreated, nothing deleted.
- [x] A corrupt `audio.json` is logged at WARNING and skipped; it does not
      fail application startup, and does not affect any other record.
- [x] A pre-existing `audio.json.{uuid}.tmp` is ignored by the sweep.
- [x] A rejected upload (bytes ffprobe cannot decode) still leaves neither
      the original file nor a sidecar behind.
- [x] `DELETE /audio/{id}` removes the sidecar along with everything else;
      after a restart the record is gone.
- [x] Public API of `AudioStore` (`register`/`get`/`delete`) is unchanged in
      signature, so `api/audio.py`, `jobs/resolution.py` and every
      pre-existing test compile and pass untouched.
- [x] Full backend quartet green: `ruff format --check`, `ruff check`,
      `pyright`, `pytest`.

## Known Limitations

- **Orphans are never surfaced or pruned here.** A sidecar-without-file or
  file-without-sidecar left by `load()` sits on disk forever as far as this
  feature is concerned — a later feature (058–060, disk usage / pruning)
  is where that gets a UI and an endpoint. Noted, not fixed, per this
  feature's explicit out-of-scope list.
- **No `fsync` on the sidecar write.** A power cut between the write and
  the next successful upload can lose the *most recent* upload's record
  (the client re-uploads; ffprobe is sub-second). This mirrors the
  documented trade-off already accepted for the export artifact
  (`api/export.py`) and is a deliberate asymmetry with the model installer,
  whose weights are expensive to re-download and do `fsync` before
  publishing.
- **`register()` now does filesystem I/O it previously didn't** (a
  `mkdir` + a small synchronous file write). It runs on the request's
  event loop, same as `prepare_original_path`'s directory creation already
  did; the write is a few hundred bytes of JSON, so the stall is
  negligible and in line with what the endpoint already does on this path.

## Required tests

- `backend/tests/test_audio_durability.py`:
  - `test_a_fresh_store_does_not_see_a_previous_runs_upload` — isolates the
    pre-existing-bug proof down to the store, independent of a full app
    restart.
  - `test_get_audio_survives_a_restart` — the headline.
  - `test_create_job_against_a_restored_upload_completes` — proves the
    resolution path.
  - `test_sidecar_without_file_is_a_404_not_a_crash`
  - `test_file_without_sidecar_is_a_404_and_boots_clean`
  - `test_corrupt_sidecar_is_a_warning_not_a_startup_failure`
  - `test_stray_tmp_file_is_ignored`
  - `test_rejected_upload_leaves_neither_file_nor_sidecar`
  - `test_delete_removes_the_sidecar_and_the_record_is_gone_after_restart`
- Full existing backend suite stays green (`pytest`, unmodified files).

## Notes / decisions

- **Fail-first proof.** Before wiring `audio_store.load()` into
  `main.py`'s `lifespan()`, the new test file was run against the
  branch as it stood with `AudioStore.register()` already writing sidecars
  but nothing yet reading them back. Four tests failed exactly as
  expected — a plain 404 where 200/201 was asserted:
  `test_get_audio_survives_a_restart`,
  `test_create_job_against_a_restored_upload_completes` (a 404
  `audio_not_found` from `POST /jobs`),
  `test_corrupt_sidecar_is_a_warning_not_a_startup_failure` (no warning was
  ever logged, because nothing read the sidecar at all) and
  `test_stray_tmp_file_is_ignored`. Restoring the one-line `load()` call
  turned all nine green. This is the same proof repeated at the unit level
  in `test_a_fresh_store_does_not_see_a_previous_runs_upload`, which needs
  no app restart at all — a second `AudioStore(data_dir)` over a directory
  that already has a valid sidecar simply starts empty.
- **Where `load()` runs, and why not `__init__`.** The design note asked to
  "check how the store is constructed and put the sweep where it runs
  per-app, documenting the reasoning." `AudioStore(settings.data_dir)` is
  built inside `create_app()`, and `main.py` has a module-level
  `app = create_app()` — so anything run from `__init__` executes at
  **import** time, before either entry path (`uvicorn straticate.main:app`
  or `serve()`) has configured logging. `main.py`'s own `lifespan`
  docstring already documents exactly this trap for the device probe
  (feature 029: a bare `logging.lastResort` swallows DEBUG entirely and
  prints WARNING without a timestamp or logger name) and moved the fix
  into the lifespan, which is the earliest point that runs once per
  *running* application with logging already configured. `load()` follows
  the same rule for the same reason, called from `lifespan()` right after
  `detector.refresh()`.
- **Minimal `main.py` hunk.** Feature 057 (durable job records, on its own
  parallel branch) also adds one `load()`-style call to the same
  `lifespan()` function. Both hunks were kept to the smallest reasonable
  size — one guarded call plus a short comment, following the exact
  `cast(X | None, getattr(app.state, "...", None))` shape the detector and
  installer already use — specifically so the merge between the two
  branches has no reason to conflict beyond adjacent lines.
- **`AudioFile.model_validate_json` doubles as the sidecar's schema
  guard.** A `ValidationError` (pydantic) is a `ValueError` subclass, same
  as `json.JSONDecodeError`, so `load()` catches `(OSError, ValueError)` to
  cover an unreadable file, malformed JSON, and JSON that no longer matches
  the current `AudioFile` shape, all as one "corrupt, skip it" case.
- **Sweep keys by directory name, not the sidecar's own `id` field.** In
  every case `register()` produces they're identical; keying by the
  directory is the defensive choice for a hand-edited or otherwise
  corrupted sidecar, so a mismatched `id` can never restore a record under
  the wrong key.
- Ran the full backend quartet from `backend/`:
  `uv run --extra torch ruff format --check .` ·
  `uv run --extra torch ruff check .` · `uv run --extra torch pyright` ·
  `uv run --extra torch pytest` — all green (see PR for exact counts).
