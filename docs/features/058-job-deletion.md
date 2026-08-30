# [058] Job deletion + exports authority in layout

Branch: `058-job-deletion`
Status: PR OPEN
Dependencies: 057
PR: #98

## Objective

`DELETE /api/v1/jobs/{job_id}` removes a terminal job wholesale — record,
stems and exports in one `rmtree`. Before this feature nothing could remove
the stems and exports a completed job produced; only the audio *upload* it was
separated from could be deleted (`DELETE /audio/{audio_id}`), leaving derived
output as orphaned disk usage forever. This is A1's worst case, and this
feature is what finally answers it. Exports-directory authority also moves
into `jobs/layout.py`, so no future path exists where stems die and their
exports survive — the D3 constraint, now codified structurally rather than by
convention.

## Scope

- `jobs/layout.py` gains `EXPORTS_DIRECTORY` and `job_exports_dir(data_dir,
  job_id)`. `api/export.py` reads and writes through it instead of building
  `{data_dir}/jobs/{job_id}/exports` itself (import move, no behavior change).
- `JobManager.remove(job_id)`: refuses a non-terminal job (`409 job_active`,
  `detail: {job_id, state}`), otherwise pops the in-memory entry and returns
  it. No event is emitted (symmetric with `restore()`).
- `DELETE /jobs/{job_id}` in `api/jobs.py`: `404 job_not_found` for an unknown
  job, `409 job_active` for a non-terminal one (cancel first — deleting under
  a running executor is the corruption this feature removes, not a case it
  adds), else `manager.remove()`, then a synchronous unlink of
  `job_record_path(...)` (an unlinkable record aborts the delete honestly with
  `500` and re-seeds the entry via `manager.restore()` rather than
  half-deleting — see Notes), then `await
  asyncio.to_thread(shutil.rmtree, ..., ignore_errors=True)` of
  `{data_dir}/jobs/{job_id}`.
- A deleted job's record cannot survive a restart: it is gone from disk, so
  the next boot's `JobStore.load_all()` never sees it.
- Contract: `docs/contracts/rest-api.md` (new route, `job_active`, the
  best-effort-on-Windows trade). `frontend/src/api/generated/api.d.ts`
  regenerated (a new route drifts it even with no schema change).

## Out of scope

- Disk-usage reporting (059) and prune (060) — a locked file `rmtree` leaves
  behind is documented debris for 060 to sweep, not something this feature
  cleans up itself.
- Frontend UI for deletion. The endpoint is the deliverable; a later feature
  can wire a button to it.
- Audio-upload deletion semantics (`DELETE /audio/{audio_id}`, feature 056) —
  unchanged.
- Any change to `api/system.py` or the `system/` package (feature 059's
  territory, developed in parallel).

## Expected modules/files

- `backend/src/straticate/jobs/layout.py` — `EXPORTS_DIRECTORY`,
  `job_exports_dir`.
- `backend/src/straticate/jobs/manager.py` — `JobManager.remove`.
- `backend/src/straticate/api/jobs.py` — `DELETE /jobs/{job_id}`.
- `backend/src/straticate/api/export.py` — import move only.
- `backend/tests/test_api_job_deletion.py` (new), `backend/tests/test_jobs_manager.py`
  (removal unit tests), `backend/tests/test_api_export.py` (import move).
- `docs/contracts/rest-api.md`, `ROADMAP.md`,
  `frontend/src/api/generated/api.d.ts` (regenerated).

## Acceptance criteria

- [x] Deleting a completed job returns `204`; its directory (record, stems,
      exports) is gone from disk; `GET /jobs/{job_id}` is `404 job_not_found`
      afterward; a restart never lists it again.
- [x] Deleting a `queued` or actively-processing job is refused with `409
      job_active` and `detail.state` naming the job's current state; the job
      and its directory are untouched. Cancelling and waiting for the
      terminal event, then deleting, succeeds.
- [x] Deleting an unknown job is `404 job_not_found`.
- [x] A file locked by an open handle (the Windows case: a stem or export
      mid-download through `FileResponse`) does not fail the request —
      deletion still answers `204`, the job is gone from every other
      endpoint, and the locked file is left as debris.
- [x] `job.json` itself resisting removal — locked on Windows, or any other
      `OSError` — aborts the whole delete with `500` instead of leaving a
      half-deleted job: the manager entry is re-seeded, the job is served,
      listed and untouched on disk exactly as before the request, and a
      restart still lists it.
- [x] The fail-first check: before the route existed, `DELETE
      /api/v1/jobs/{id}` answered `405 Method Not Allowed` (the path already
      existed for GET/POST). See Notes.
- [x] `job_exports_dir` is the only place `api/export.py` builds the exports
      path; `EXPORTS_DIRECTORY` moved out of that module.
- [x] Backend quartet green; frontend `npm run typecheck` green after
      `api.d.ts` regeneration.

## Required tests

`backend/tests/test_api_job_deletion.py` — delete a completed job (record +
stems + exports all removed; an export is built first so `exports/` is
non-empty), delete a job that was never exported, delete a queued job
(refused, then succeeds once drained), delete a running job (refused, then
succeeds after cancel + terminal event), delete an unknown job, a second
delete of an already-deleted job (`404`, not a repeat `204`), a deleted job
does not survive a restart (via `tests/restart_harness.py`), a sibling job is
unaffected by another job's deletion, the lock-tolerance path that runs on
every platform (a real open file handle held during `shutil.rmtree`; the
record is still gone, the job still 404s, and a restart still does not
resurrect it), a Windows-only companion (`skipif` gated) that additionally
asserts the locked file itself survives on disk as debris, and a
cross-platform record-unlink-failure test (`Path.unlink` monkeypatched to
raise for `job.json`) proving the fail-first case: the delete answers `500`
and the job comes back fully intact — served, listed, its directory
untouched — rather than resurrecting a half-deleted job on the next restart.

`backend/tests/test_jobs_manager.py` — `JobManager.remove` in isolation:
completed job popped and unreachable via `get`/`list_jobs`, no event emitted,
queued/running jobs refused with `job_active` and the entry left intact,
unknown job is `job_not_found`, and `remove` remains callable after
`aclose()`.

## Notes / decisions

### The fail-first check

Before `DELETE /jobs/{job_id}` was added, `api/jobs.py` already routed GET and
POST under `/jobs/{job_id}` and `/jobs/{job_id}/cancel`, so FastAPI answered
any DELETE to `/api/v1/jobs/{job_id}` with the standard `405 Method Not
Allowed` — proof the route was genuinely new rather than already handled by
an existing handler. Verified directly against a real app instance before
implementing, and again (by temporarily reverting the route) after: the
before-case answered
`{"error":{"code":"method_not_allowed","message":"Method Not Allowed","detail":{}}}`
at `405`; after implementing, the same request answers `204` for a terminal
job.

### Why `remove()` lives on `JobManager`, split from the `rmtree`

`JobManager.remove()` only ever touches the in-memory entry map — it validates
state and pops the entry, nothing else. The route in `api/jobs.py` does the
filesystem removal itself, in that order: refuse first, touch disk second.
That ordering is what makes the 409 case exact — a refused delete leaves
*nothing* touched, not the entry and not a single byte on disk. It also keeps
`straticate.jobs` free of any dependency on `shutil` or on the on-disk layout
beyond the path-building already in `jobs/layout.py`; the manager's job is
state, not file I/O.

### Why exports-directory authority moved into `jobs/layout.py`

Before this feature, `api/export.py` built `{data_dir}/jobs/{job_id}/exports`
itself (`job_output_dir(...) / EXPORTS_DIRECTORY`, with `EXPORTS_DIRECTORY`
defined locally in that module). That was harmless on its own, but it meant
two things had to independently agree about where a job's derived output
lives: the export router, and — after this feature — the deletion route's
`rmtree` of the *whole* job directory. Two independent path-builders is
exactly the shape a future third one (a hypothetical second export-like
feature) could get wrong in a way that leaves an export outside the tree a
`DELETE` removes. Moving `EXPORTS_DIRECTORY` and a `job_exports_dir()`
function into `jobs/layout.py` — beside `job_output_dir` and
`job_record_path`, which already play this role for the record and the stems
(`inference/layout.py`'s `job_stems_dir`) — makes it structurally impossible
for an export to live somewhere `job_output_dir` doesn't reach: there is now
exactly one function in the whole codebase that knows where a job's exports
are, and `DELETE /jobs/{job_id}` doesn't even call it — it just removes
everything under `job_output_dir`, exports included by construction. This is
the D3 constraint (a job's exports must not outlive its stems) reframed as "a
job's exports cannot even be *built* anywhere the deletion path doesn't reach"
rather than as a rule two modules have to remember to honor.

### Best-effort removal on Windows, stated plainly

`shutil.rmtree(directory, ignore_errors=True)` — the same call
`audio/storage.py` already uses for upload deletion (feature 006). The reason
it has to be tolerant here specifically: `GET /jobs/{job_id}/export` and `GET
/jobs/{job_id}/stems/{stem}` both serve files via `FileResponse`, which holds
an open file handle for the duration of the download. Windows refuses to
unlink a file that is still open (verified directly: a plain `open()` held
across a `shutil.rmtree()` call raises `PermissionError` for that file, in
this repository's own environment). Failing the whole `DELETE` because one
file among possibly a dozen stems and exports happens to be mid-download would
make deletion undependable exactly when a user is most likely to attempt it
(cleaning up disk space while other downloads may be in flight). Instead the
request answers `204` unconditionally once `manager.remove()` has succeeded:
the job is authoritatively gone (its record is what every other endpoint
consults, and the record is removed along with everything unlocked), and
whatever a held-open handle prevented from being unlinked is debris — the
directory and any locked file inside it may still exist on disk, but nothing
in the API will ever mention the job again. A later pruning feature (060) is
the natural place to sweep debris like this; this feature does not attempt to
retry or wait for handles to close, which would turn a fast, synchronous
delete into an open-ended one.

### The record unlinks first, synchronously, so a locked `job.json` cannot half-delete a job

`straticate.jobs.store`'s module docstring states the invariant a job
directory keeps: it is self-describing, so nothing can "be half-deleted into a
record that points at stems that are gone, or stems no record mentions." A
single `shutil.rmtree(..., ignore_errors=True)` over the whole job directory
can violate the second half of that if `job.json` happens to be the one file
`rmtree` cannot remove (locked on Windows; any other `OSError` elsewhere): the
stems and exports vanish, the record survives, and a restart serves a
`completed` job whose result files are all `404`.

The fix makes the record's removal unconditional instead of best-effort: this
handler unlinks `job_record_path(...)` by itself, synchronously, *before* the
`rmtree`. If that unlink fails, `manager.remove()`'s already-popped entry is
re-seeded with `manager.restore([job])` — safe, because the popped job is
necessarily terminal, the one state `restore()` accepts — and the `OSError` is
re-raised, so the client gets a `500` and the job is exactly as it was before
the request: served, listed, and untouched on disk. Only once the record is
confirmed gone does the (now thread-offloaded — see below) `rmtree` run over
whatever is left, best-effort as before. This keeps the store's invariant
holding in both directions: a job never has a record without its stems, and
after this fix it never has stems without a record either, because the record
is the first and only unconditionally-required removal.

### The `rmtree` runs in a worker thread

A job directory can be large — a measured 1.17 GB tree took 175 ms to remove,
run inline on the event loop, which every other connected client (progress
events, other requests) would feel as a stall. `await
asyncio.to_thread(shutil.rmtree, ..., ignore_errors=True)` moves it off the
loop. This is safe specifically because `manager.remove()` pops the entry
*synchronously*, with no `await` between it and the thread being started (the
record unlink in between is also synchronous): no other request can observe
this job id as still active, so nothing can submit, cancel, or race a second
delete against it while the thread runs. The record unlink itself stays
synchronous and inline rather than also being offloaded — it is a few hundred
bytes, and it is the step the delete's correctness now depends on, not merely
its cleanliness.

### Idempotence, by design, is at the resource level, not the response

A second `DELETE` of an already-deleted job is `404 job_not_found`, not a
repeated `204`. This differs from `POST /jobs/{job_id}/cancel`, which *is*
idempotent at the response level (a terminal job's second cancel is still
`200`). The difference is what each verb is answering: cancel's contract is
"this job is not going to run" and that stays true forever once reached, so
repeating the request is harmless to answer the same way. Delete's contract is
"this specific resource no longer exists", and after the first successful
delete it genuinely doesn't — a second request is describing a resource that
was never there from its own point of view, which `404` says honestly. This
matches `DELETE /audio/{audio_id}`'s existing behavior (feature 006): deleting
an unknown or already-deleted audio ID is also `audio_not_found`, not a `204`.

## Known limitations

- **Locked-file debris has no automatic cleanup.** A file held open during a
  `DELETE` survives on disk until whatever held it closes and something else
  removes the directory. Nothing in this feature retries, waits, or lists such
  debris — that is 060's job.
- **An export build racing a delete can recreate an empty orphan directory.**
  `build_artifact` (feature 022) calls `artifact.parent.mkdir(parents=True,
  exist_ok=True)` before it writes. If a `GET /jobs/{job_id}/export` request
  is mid-build when `DELETE /jobs/{job_id}` runs, the `mkdir` can execute after
  the `rmtree` has already removed the job directory, recreating an empty
  `exports/` (and job) directory that nothing then populates — the stems the
  build needs are gone. This is not a resurrection: the record is unlinked
  before the `rmtree` even starts, so the job is already gone from every
  endpoint's point of view by the time the race could happen, and the racing
  export request answers `export_failed` on its own rather than serving
  anything stale. It is the same debris category as a locked file: acceptable
  because it is prune's (060) responsibility to sweep, not because this
  feature prevents it. (This is why the route's docstring no longer claims
  there is *no* surviving path an export could land on — only that no second
  path-building function points at one.)
- **No audit of what a delete actually removed.** The response carries no
  detail about partial removal (a locked file, a permissions failure on some
  other platform); the client learns only that the job is gone from the API.
  A future feature could report this if it turns out to matter in practice.
- **Deletion is not itself an event.** No WebSocket frame announces a job
  being deleted, symmetric with how `restore()` emits nothing for a job seeded
  from a previous process — a connected client watching a job list would need
  to notice the `404` on its own next `GET`, or a future feature could add a
  `job_deleted` event.
