# [060] Prune endpoint

Branch: `060-prune-endpoint`
Status: PR OPEN
Dependencies: 058, 059
PR: #101

## Objective

`POST /api/v1/system/prune` reclaims disk space under `data_dir` in typed
classes — export caches, orphans, terminal jobs — each opt-in and each safe by
construction. It completes M5's retention story: 059 made everything visible,
058 made a single job deletable, and this makes the whole lot reclaimable in
one call. It never deletes unasked, and the job the server is running is
excluded from every class.

## Scope

- `schemas/maintenance.py`: `ReclaimClass` (`export_caches` / `orphans` /
  `terminal_jobs`), `PruneRequest` (three booleans defaulting to `false`, plus
  `older_than_seconds`), `PruneClassReport` (`items_removed`, `bytes_freed`),
  `PruneFailure` (`reclaim_class`, `target`, `reason`) and `PruneReport`
  (three class reports + totals + `failures`). Exported from
  `schemas/__init__.py`.
- `schemas/maintenance.py` also gains `DiskUsageReport.complete` — 059's
  deferred handoff, resolved here (see Notes).
- `system/prune.py` (new): `plan_prune` measures every candidate and returns
  the concrete `PruneTarget` list plus planning-time refusals; `execute_prune`
  removes exactly that list and reports what actually went. Both pure,
  synchronous and blocking, the shape `disk_usage_report` established.
- `system/disk_usage.py`: `_walk_root`/`_is_debris`/`_DEBRIS_DIR_PREFIX`
  become public `walk_files`/`is_debris`/`DEBRIS_DIR_PREFIX` and the walker
  returns a `WalkResult` carrying `complete`. `EXPORTS_DIRECTORY` is now
  imported from `jobs/layout.py` rather than duplicated (059 left that for
  "once 058 lands"; 058 has landed).
- `jobs/removal.py` (new): 058's `DELETE /jobs/{job_id}` route body, factored.
  `detach_job` (pop the entry, unlink the record, re-seed on failure —
  synchronous, no awaits), `remove_job_directory` (the best-effort `rmtree`),
  and `remove_job` (both, with the tree removal offloaded). `api/jobs.py`'s
  handler is now one call to `remove_job`.
- `jobs/manager.py`: `JobManager.ids()` — 059's other deferred handoff. The
  disk-usage route switches to it.
- `audio/storage.py`: `AudioStore.pending_ids()`, an in-flight-upload
  reservation taken by `prepare_original_path` and released by `register` /
  `remove_files` (see Notes — this is what stops prune deleting an upload that
  is still arriving).
- `api/system.py`: `POST /system/prune`, beside 059's disk-usage route.
- `scripts/export_openapi.py`: `PruneRequest`/`PruneReport` added to the root
  model list; `frontend/src/api/generated/api.d.ts` regenerated and committed.
- `docs/contracts/rest-api.md`, this file, `ROADMAP.md`.

## Out of scope

- **Auto-prune policies** (retention rules, schedules, background sweeps) —
  v0.4.0. This endpoint is manual, opt-in, and does exactly what one request
  asks for.
- **Any UI.** `api.d.ts` is regenerated because a route and its schemas are a
  shared contract, not because anything renders them yet.
- **Audio-deletion semantics** (`DELETE /audio/{audio_id}`, feature 056) —
  unchanged. `AudioStore` gains a read-only accessor and a reservation; no
  deletion behaviour moves.
- Features 062/063/066's files.

## Expected modules/files

- `backend/src/straticate/schemas/maintenance.py` · `schemas/__init__.py`
- `backend/src/straticate/system/prune.py` (new) · `system/disk_usage.py` ·
  `system/__init__.py`
- `backend/src/straticate/jobs/removal.py` (new) · `jobs/manager.py` ·
  `jobs/__init__.py`
- `backend/src/straticate/api/system.py` · `api/jobs.py`
- `backend/src/straticate/audio/storage.py`
- `backend/src/straticate/scripts/export_openapi.py`
- `backend/tests/test_prune.py` (new) · `backend/tests/test_disk_usage.py`
  (the `complete` field)
- `docs/contracts/rest-api.md` · this file · `ROADMAP.md`
- `frontend/src/api/generated/api.d.ts` (regenerated, no other frontend
  change)

## Acceptance criteria

- [x] `POST /system/prune` accepts `{export_caches, orphans, terminal_jobs,
      older_than_seconds}` and returns per-class `{items_removed,
      bytes_freed}` plus totals and a `failures` list.
- [x] Nothing is removed unless its class is named: `{}` frees zero and is a
      valid request.
- [x] `export_caches` removes only terminal jobs' `exports/`; the record and
      stems survive, and re-exporting rebuilds the identical artifact.
- [x] `orphans` removes record-less `audio/`/`jobs/` directories and
      `*.tmp`/`*.part`/`.build-*` debris, using **059's own classifier**, and
      spares every live upload and job. Afterwards `GET /system/disk-usage`
      reports `orphans: {count: 0, bytes: 0}`.
- [x] `terminal_jobs` removes finished jobs through 058's own code path
      (record-first unlink, entry pop, offloaded `rmtree`), honours
      `older_than_seconds` against `finished_at`, and reports a per-job
      failure rather than failing the whole prune.
- [x] A queued or running job — and its directory — is untouched by every
      class; the job runs to completion and its stems serve afterwards.
- [x] An upload still being written is not treated as an orphan.
- [x] A target that could not be measured in full is refused
      (`reason: "unreadable"`) and left alone; the readable targets are still
      reclaimed.
- [x] The three class figures partition what was removed, and match an
      independent `os.walk` ground truth computed in the test.
- [x] A second identical request frees `0` items and `0` bytes.
- [x] A pruned job stays gone across a restart; an unpruned one survives.
- [x] `DiskUsageReport.complete` distinguishes an unreadable subtree from an
      empty one.
- [x] The fail-first check: before the route existed, `POST
      /api/v1/system/prune` answered `404 not_found`. See Notes.
- [x] Backend quartet green; frontend `format:check`, `lint`, `typecheck`
      green after `api.d.ts` regeneration.

## Required tests

**`backend/tests/test_prune.py`** (32 tests) — two tiers, the split
`test_disk_usage.py` established:

- Unit, against `plan_prune`/`execute_prune` directly (no application):
  an empty request plans nothing; `export_caches` plans only terminal jobs'
  `exports/`; `orphans` plans record-less directories and debris; a
  `.build-*` staging directory collapses to one directory target; a running
  and a queued job's directories are never planned in any class;
  `terminal_jobs` claims a whole job directory and the other two classes
  stand down (no double counting, asserted against `measure()`);
  `export_caches` and `orphans` do not both count the same exports;
  `older_than_seconds` selects only jobs outside the window; a terminal job
  with no `finished_at` is kept while a window is set; a selected job whose
  directory is already gone is still planned (its entry must still drop); an
  unreadable directory and an unlistable root are refused rather than
  guessed; `execute_prune` totals per class and, when a directory survives
  its own removal, **subtracts the survivors** rather than assuming;
  `AudioStore` reserves an in-flight upload and the very same directory is an
  orphan without the reservation.
- HTTP, against the real application (real job manager on the test's own
  event loop, real `FakeSeparator` writing real stems, real export builds):
  the headline (route present, `{}` frees nothing — verified fail-first);
  `export_caches` leaves stems and record and the export **rebuilds** to the
  identical bytes; pruning **everything while a job runs** leaves that job's
  tree (including a hand-planted `.part`) untouched, and the job then
  completes and its stems serve; `orphans` removes two hand-made orphan
  directories, a stray `.part`, a stray `.tmp` and a `.build-*` staging
  directory while every live thing still answers, and the follow-up
  disk-usage report agrees (`orphans` zero, `complete` true); a second
  identical prune frees nothing; the totals are the sum of the classes and
  match an independent `os.walk`; `terminal_jobs` honours the retention
  window (two jobs, one aged via a hand-written record restored through
  `tests/restart_harness.py`); a pruned job stays gone across a restart while
  its sibling survives; an upload still being written is not an orphan; one
  unreadable orphan is reported and costs the readable one nothing;
  four malformed requests are `422`; and the walk really runs off the event
  loop (040's parked-thread proof, third reuse).

**`backend/tests/test_disk_usage.py`** — `complete` is `true` for an empty
data directory, and `false` for both an unlistable subtree and a file that
vanished mid-walk.

## Notes / decisions

### The fail-first check

Before this feature, `api/system.py` registered nothing at
`/api/v1/system/prune` — no method at all, so it was a plain `404`, not the
`405` feature 058's new method on an existing path produced. Verified twice
against a real application instance with `api/system.py` reverted to its
`origin/dev` state: first ad hoc
(`{"error":{"code":"not_found","message":"Not Found","detail":{}}}` at `404`),
then by running the headline test itself, which failed with
`assert 404 == 200`. After implementing, the same request answers `200` with
all-zero figures.

### Why the route lives in `api/system.py`, not a new `api/maintenance.py`

The prune acts on exactly what `GET /system/disk-usage` reports: the same
`data_dir`, the same classification, the same three dependencies (settings,
audio store, job manager). Putting it in a second router would split one
surface — `/system/*` — across two modules and make the read and the write
halves of one idea look like unrelated features to anyone reading the
routers. `api/system.py` is 90 lines of route bodies; there is no size
pressure to split, and if `/system/*` ever grows a maintenance sub-surface,
moving both routes together is the change to make, not moving one.

### Plan, then remove — and why that is not just tidiness

`plan_prune` measures every candidate and returns paths; `execute_prune`
removes them. Three properties fall out of the split that would each have
needed separate machinery otherwise:

- **The report's classes partition what was removed**, so the totals are a
  plain sum. Overlaps — a job whose whole directory is going, whose
  `exports/` and whose debris would each be counted a second time — are
  resolved once, at planning time, while everything is still there to be
  measured. Resolving them during removal would mean measuring things that
  were already gone.
- **The counts are measurements, not estimates.** They describe files that
  existed, taken from a walk, not a running tally of what a `rmtree` was
  asked to do.
- **A test can assert what *would* be removed without removing it**, which is
  most of the unit tier and the only way to test the "never touch a running
  job" rule without racing a real one.

### Never delete what you could not see — 059's deferred call, resolved

Feature 059 recorded that an unreadable subtree reports `{count: 0, bytes: 0}`,
indistinguishable from an empty one, and left the decision to 060. The answer
is `DiskUsageReport.complete`, wired through the walker's `onerror` and
`stat`-failure paths, and prune treats it as binding: a target it could not
measure in full is not removed, and is reported as a `PruneFailure` with
reason `unreadable`.

For a *report*, an undercount is cosmetic. For a *delete* it is not: "there is
nothing here" and "I could not look" are opposite instructions, and only one
of them is recoverable when it turns out to be wrong. The refusal is
per-target rather than per-class, which is both safer and more useful — the
target nobody could see inside is the only one left alone, every other target
was seen in full and is reclaimed normally. A blanket class-level skip would
have let one unreadable directory veto an otherwise clean prune without
making anything safer.

Note that a *vanished* file (a `stat` that raises `FileNotFoundError`) also
clears the flag, even though a file that is gone hides nothing. Telling the
two apart means trusting an `errno` to distinguish "no longer there" from
"there and unreadable", and the cost of being wrong is asymmetric: an
over-cautious flag defers a prune, an over-confident one deletes a directory
nobody could see inside.

### The two things prune must never touch, and why the filesystem cannot say so

Both hazards have the same shape — a directory that looks exactly like
garbage while being the opposite — and neither is decidable from disk:

1. **A running job's directory.** `FakeSeparator` (and the real separator)
   writes each stem to `{stem}.wav.part` and renames it into place. Those
   files *are* debris by name and live output by fact. Any job that has not
   reached a terminal state is therefore excluded from all three classes,
   before any other rule applies.
2. **An upload that is still arriving.** Between
   `AudioStore.prepare_original_path` and `AudioStore.register` — the whole
   multipart stream plus the ffprobe after it — an upload has a directory
   full of real bytes and no record, which is precisely 059's definition of
   an orphan. A 40-minute upload of a large file looks, for 40 minutes,
   exactly like the leftovers of one that died.

The fix for (2) is `AudioStore.pending_ids()`: `prepare_original_path`
reserves the ID, `register` releases it (it has a record now) and
`remove_files` releases it (its files are gone). `api/audio.py` already calls
`remove_files` on *every* failure path, including a bare `except
BaseException`, so no crash between the two leaks a permanent exemption — and
a process that dies mid-upload leaves a genuine orphan that the next process,
with an empty reservation set, prunes exactly as it should. The alternative
considered was an age heuristic (skip directories whose newest file was
touched recently), which is both fuzzier and wrong for the case that matters:
a slow upload of a large file is precisely when the directory is oldest.

### Why 058's route body moved into `jobs/removal.py`

The `terminal_jobs` class is a bulk `DELETE /jobs/{job_id}`, and its ordering
— pop the entry, unlink the record synchronously (re-seeding the entry if
that fails), then `rmtree` the rest in a thread — is subtle enough that a
second copy would eventually diverge. This is the same argument 058 itself
made for moving the exports path into `jobs/layout.py`: one function, so
nothing can disagree with it.

`detach_job` and `remove_job_directory` are split out of `remove_job` for one
reason: a bulk caller needs the *synchronous* half to run for every selected
job before any `await`. `DELETE` calls `remove_job` (both halves, one job);
prune loops `detach_job` over the batch with no suspension point in between,
then offloads all the tree removals together. That preserves 058's guarantee
at batch scale — by the time the removal thread starts, no concurrent request
can submit, cancel or delete any job in the batch, and no client can observe
the batch half-detached.

Per-job failures (`job_not_found` from a concurrent `DELETE`, `job_active`,
an unlinkable record) are recorded in `failures` and the prune continues; the
re-seed-on-record-unlink-failure semantics are 058's, unchanged, and now
apply per job.

### `JobManager.ids()` — 059's other handoff

Added, and `GET /system/disk-usage` switched to it. 059's route read
`[job.id for job in manager.list_jobs()]`, which deep-copies every `Job`
(configuration, result, stems and all) to read one string off each. `ids()`
is the manager-side twin of `AudioStore.ids()`, and returning IDs keeps the
copy-on-read contract intact for free — there is nothing mutable to hand out.
Prune itself still uses `list_jobs()`, because it needs `state` and
`finished_at`.

### `EXPORTS_DIRECTORY` is no longer duplicated

059 defined it locally in `system/disk_usage.py` with a docstring saying it
belonged in `jobs/layout.py` "once 058 lands". 058 has landed, so the local
copy is gone and the module imports the authority. `straticate.system` still
re-exports the name, so nothing downstream changed.

### `items_removed` counts files, deliberately

The same unit as `UsageBucket.count`, so a disk-usage report taken before a
prune and the prune's own report are directly comparable — which is exactly
what the ground-truth test does, subtracting one from the other. An "items"
count of top-level things removed would have been smaller and less useful,
and would have made "did it free what the report said was there?"
unanswerable without a second walk.

### `older_than_seconds` without `terminal_jobs` is a `422`

It is the only field that narrows a class rather than selecting one, and it
narrows exactly one. Accepting it alone would let a request that reads as
cautious ("only remove things older than a week") mean something quite
different from what it says — and paired with `orphans: true`, a caller could
reasonably believe the window applied there too. `extra="forbid"` is the same
argument for a different mistake: `{"exports": true}` would otherwise be a
successful prune that freed nothing, which reads exactly like "there was
nothing to reclaim".

### Removal reports what went, not what was planned

`shutil.rmtree(..., ignore_errors=True)` — 058's and 056's tolerance, for
058's reason (a file mid-download through `FileResponse` cannot be unlinked
on Windows, and one such file must not cost the caller the rest of the tree).
But tolerating it silently would make the *report* wrong, and this report's
whole value is that its numbers can be trusted against a disk-usage reading.
So a directory that survives its own removal is re-walked and the survivors
are subtracted: what went is counted, what stayed is not, and the difference
is reported as `partially_removed`. That check costs one `exists()` on the
happy path.

## Known limitations

- **An export build racing a prune can still lose.** This is 058's known
  limitation seen from the other side. `build_artifact` calls
  `artifact.parent.mkdir(parents=True, exist_ok=True)` before it writes, so a
  build that starts before an `export_caches` prune and publishes after it
  can recreate the `exports/` directory — and a build that is mid-flight when
  its `.part` file is swept as debris answers `export_failed` (500) instead
  of serving. Neither corrupts anything: the artifacts are caches, the
  stems are untouched, and a retry rebuilds. The narrowing that was
  considered and not done is having the route consult `BuildLocks`
  (`app.state.export_locks`) for in-flight builds under each job before
  planning its exports — cheap, but it reaches into `api/export.py`'s
  internals from a `system/` module, and the failure it prevents is one
  retried request. Worth a numbered feature if it shows up in practice.
- **`orphans` cannot sweep a running job's debris.** Debris inside a queued
  or running job's directory is deliberately out of reach (it is
  indistinguishable from that job's live `.part` output), so a disk-usage
  report taken while a job runs can show a non-zero `orphans` bucket that a
  prune will not clear until the job finishes. Running the prune again
  afterwards clears it.
- **No dry run.** `plan_prune` exists and returns exactly what would be
  removed, so a `dry_run: true` flag reporting the plan without executing it
  is a small addition — but it is a contract change, and nothing has asked
  for it yet.
- **Nothing prunes automatically.** No policy, no schedule, no background
  sweep, no size cap: a human (or a script) has to call this. Auto-prune is
  v0.4.0's, and this endpoint is deliberately the primitive it would be built
  on rather than a policy engine with a manual mode.
- **The report is not persisted.** What a prune removed exists only in its
  own response; there is no audit log a later request can read. 058 recorded
  the same gap for single deletes.
