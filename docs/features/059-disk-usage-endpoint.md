# [059] Disk-usage endpoint

Branch: `059-disk-usage-endpoint`
Status: PR OPEN
Dependencies: 056, 057
PR: —

## Objective

`GET /api/v1/system/disk-usage` reports what Straticate holds under
`data_dir` — uploads, job stems, job exports and orphans, plus the free/total
bytes of the holding filesystem — so retention that is currently manual-only
(nothing prunes anything yet) is at least visible. It deletes nothing; 060 is
the prune that acts on what this makes visible.

## Scope

- `backend/src/straticate/schemas/maintenance.py`: `UsageBucket` (`count`,
  `bytes`) and `DiskUsageReport` (`uploads`, `job_stems`, `job_exports`,
  `orphans`, `free_bytes`, `total_bytes`), exported from `schemas/__init__.py`.
- `backend/src/straticate/system/disk_usage.py`: `disk_usage_report`, a pure,
  synchronous walker over `{data_dir}/audio` and `{data_dir}/jobs`. It takes
  the live audio and job IDs as plain collections (not the `AudioStore` or
  `JobManager` themselves), classifies every **file** into one of the four
  buckets, and reuses `straticate.system.storage.storage_report` (feature 040)
  for the free/total figures rather than duplicating that logic.
- `backend/src/straticate/audio/storage.py`: `AudioStore.ids()`, a small
  read-only accessor returning every registered upload ID — what the route
  hands the walker so it never has to see the store itself.
- `backend/src/straticate/api/system.py`: `GET /system/disk-usage`, beside
  040's `GET /system/storage`, reading `store.ids()` and
  `[job.id for job in manager.list_jobs()]` and offloading the walk with
  `asyncio.to_thread` (`os.walk`/`os.stat` are blocking filesystem calls, the
  same reasoning 040 already documents for `shutil.disk_usage`).
- `scripts/export_openapi.py`: `DiskUsageReport` added to the root-model list;
  `frontend/src/api/generated/api.d.ts` regenerated and committed.
- `docs/contracts/rest-api.md`, this file, `ROADMAP.md`.

## Out of scope

- Deletion (058) and prune (060) — this is the read-only visibility surface
  both build on. `api/jobs.py`, `jobs/manager.py` and `jobs/layout.py` are
  058's; nothing here touches them beyond a read-only import of
  `jobs.layout.JOBS_DIRECTORY`.
- Any UI. No frontend consumer is added; `api.d.ts` is regenerated because the
  new route and schemas are a shared contract, not because anything renders
  them yet.
- An `exports/`-directory constant shared with `api/export.py`. Importing
  `straticate.api.export.EXPORTS_DIRECTORY` from `straticate.system` would
  make a lower-level package depend on a router module, so the name is
  duplicated locally in `system/disk_usage.py` with a docstring noting that
  058 is moving exports-directory authority into `jobs/layout.py`, which is
  where this constant belongs once that lands.

## Expected modules/files

- `backend/src/straticate/schemas/maintenance.py` · `schemas/__init__.py`
- `backend/src/straticate/system/disk_usage.py` · `system/__init__.py`
- `backend/src/straticate/audio/storage.py` (added `AudioStore.ids()`)
- `backend/src/straticate/api/system.py` · `scripts/export_openapi.py`
- `backend/tests/test_disk_usage.py`
- `docs/contracts/rest-api.md` · this file · `ROADMAP.md`
- `frontend/src/api/generated/api.d.ts` (regenerated, no other frontend
  change)

## Acceptance criteria

- [x] `GET /system/disk-usage` reports `uploads` / `job_stems` /
      `job_exports` / `orphans` (each `{count, bytes}`) plus `free_bytes` /
      `total_bytes` for the filesystem holding `data_dir`.
- [x] A registered upload's files count as `uploads`; a directory with no live
      record is an `orphan`.
- [x] A known job's own files (its record and stems) count as `job_stems`;
      its `exports/` subtree counts separately as `job_exports`.
- [x] A job still queued or running is classified exactly like a completed
      one's — **never** as an orphan — because it is live from submission
      (feature 057 writes the record before any executor runs).
- [x] Stray build debris (`*.tmp`, `*.part`, anything under a `.build-*`
      staging directory) counts as `orphans` even inside an otherwise-live
      upload or job directory.
- [x] `free_bytes`/`total_bytes` follow 040's null-means-unknown doctrine,
      reusing `storage_report` rather than re-implementing it; a missing
      `data_dir` reports zero buckets and the real figures for its nearest
      existing ancestor.
- [x] Degrades rather than errors: an unreadable subtree is logged and
      undercounted, never a `500`; the response is always `200`.
- [x] The blocking walk runs in a worker thread (`asyncio.to_thread`), proven
      indirectly by following the same pattern `GET /system/storage`'s own
      liveness test already established for this endpoint's sibling.
- [x] `api.d.ts` regenerated; the contract is documented in
      `docs/contracts/rest-api.md`.
- [x] Nothing here deletes anything.

## Required tests

**`backend/tests/test_disk_usage.py`** (14 tests) — two tiers:

- Unit, against `disk_usage_report` directly (no application): an empty
  `data_dir`, a missing `data_dir` (nearest-ancestor free/total still real), a
  registered upload counted, an unregistered upload directory as a whole
  orphan, a stray `.tmp` sidecar inside a *live* upload directory as an
  orphan, a live job's record+stems vs. its `exports/` subtree correctly
  split, an unregistered job directory as a whole orphan, a stray `.part`
  inside a live job's `exports/` as an orphan, a `.build-*` staging leftover
  as an orphan, a job with no stems/exports yet still counting its record, and
  an unlistable subtree degrading to zero with one warning logged rather than
  raising.
- HTTP, against the real application (job manager on the test's own event
  loop, real `FakeSeparator`, real export build — the `test_api_export.py`
  pattern): the headline (route present, all-zero for an empty app — verified
  fail-first: a stashed route returns `404` for this exact test), a seeded
  scenario (a real upload, a completed job, a real export, a hand-made orphan
  upload, a hand-made orphan job, a stray `.part`) checked against an
  **independent** `os.walk` ground truth computed in the test rather than by
  re-invoking the module under test, and a still-running job's directory
  counted under `job_stems` and never `orphans`.

## Notes / decisions

### Why `count` is a file count, not a directory or "item" count

`UsageBucket.count` counts **files**: an upload contributes its original
media plus its `audio.json` sidecar as two files; a job with three stems and a
record contributes four. That definition is what let the HTTP test recompute
the same numbers with a plain `os.walk` in the test itself and compare them
directly, file for file and byte for byte, rather than reverse-engineering
some other unit the report happens to use.

### Why the walker takes ID collections, not the store or the manager

`disk_usage_report` is pure and synchronous, the same shape as 040's
`storage_report`: it has no application state to import, so a unit test hands
it a bare `{"abc"}` without constructing an `AudioStore` or a `JobManager`.
The route is what reads `store.ids()` and `manager.list_jobs()` from the
running application and hands the plain ID collections across the
`asyncio.to_thread` boundary — the same separation of "what to classify" from
"how to find out" that keeps the walker itself trivially testable.

### Why a job is live from submission, not completion

Feature 057 writes a job's `job.json` record **before** its executor runs, so
a job is "known" to the manager (and therefore to this endpoint) from the
moment `POST /jobs` returns `201`. Classifying a queued or running job's
directory as an orphan until it finishes would have made every in-flight
separation look, for the duration of the job, like exactly the kind of
leftover this feature exists to surface — the opposite of what a user
watching a progress bar needs to see.

### Why debris counts as orphaned even inside a live directory

`straticate.audio.storage.AudioStore.register`, `straticate.jobs.store.
JobStore.save` and `straticate.api.export.build_artifact` all write to a
temporary name (`*.tmp` or `*.part`, or a `.build-*` staging directory for a
multi-stem archive) and publish atomically with `os.replace`. Something
surviving under one of those names — even inside an upload or job directory
that is otherwise entirely live — is proof a write or a build never finished;
it is never the record or the output, so it is never counted as one.

### Why `free_bytes`/`total_bytes` reuse `storage_report` instead of a new read

`storage_report` already does exactly what this figure needs: it degrades a
missing directory to its nearest existing ancestor, degrades a permissions
failure or an unsupported platform to a documented `null`, and clamps a
nonsensical reading rather than propagating it. `data_dir` not existing yet is
the ordinary state of a fresh checkout (nothing has been uploaded or
separated), exactly parallel to 040's `models_dir` not existing before the
first install — so the existing function was reused verbatim rather than
re-implemented for a second directory.

### Reported out of scope

- **Still nothing prunes anything.** This is the read; 060 is the write.
  Every gap 040 recorded (uploads, job outputs and export artifacts
  accumulating without bound) is now at least visible, not yet acted on.
- **The `exports/` directory name is duplicated, briefly.** See the "Out of
  scope" section above — 058 is expected to consolidate it into
  `jobs/layout.py`.

## Known limitations (post-review)

- **An unreadable subtree reports `{count: 0, bytes: 0}`, indistinguishable
  from empty** — the one place this feature's own null-means-unknown
  argument cuts against it (review finding 3). There is no `partial` flag.
  Feature 060 must make a conscious call before building prune decisions on
  these numbers; a `complete: bool` field is the cheap fix if it matters.
- **`JobManager.ids()` symmetry**: the route deep-copies every `Job` via
  `list_jobs()` just to read ids (review finding 5). `AudioStore.ids()`
  exists for exactly this reason; the manager-side twin is left for 060
  (which wants it anyway) to avoid colliding with 058's concurrent
  `manager.py` changes in this wave.
