# [057] Durable job records + interrupted recovery

Branch: `057-durable-job-records`
Status: PR OPEN
Dependencies: —
PR: #92

## Objective

Job records survive a backend restart. A completed job's record, result, stems
and exports are reachable through the API in the next process — before this,
`GET /jobs` came back empty and `GET /jobs/{id}/result` was a `404` while the
stems sat on disk with nothing able to name them, the most-cited defect in the
repository. A job that was queued or running when the server stopped comes back
`failed` with a new error code `job_interrupted`, and is never re-queued.

## Scope

- `{data_dir}/jobs/{job_id}/job.json` — one JSON sidecar per job, written
  atomically, holding the serialized `Job` (the wire shape, not a second
  schema).
- `JobStore` (`backend/src/straticate/jobs/store.py`): `save`, `load_all`,
  `recover`.
- `JobManager` gains an optional injected store, writes at submit and at every
  terminal transition, and gains `restore(jobs)` — which seeds records without
  queueing them and without emitting a single event.
- The application lifespan loads → normalizes → restores before the worker
  starts.
- Documentation: `docs/contracts/rest-api.md` (durability, the repair table,
  `job_interrupted`), the router docstrings that promised "empty after a
  restart", the README's known-limitation bullet.
- The restart harness (`backend/tests/restart_harness.py`) other M5 features
  will reuse.

## Out of scope

- `audio/storage.py` and `api/audio.py` — the durable upload registry is 056.
- Deletion and pruning endpoints (058/060), disk-usage reporting (059), model
  install failures (061).
- Any frontend change beyond the regenerated `api.d.ts`.
- Surfacing orphaned job directories (a directory with no `job.json`). They are
  ignored at startup; a later feature can list them.

## Expected modules/files

- `backend/src/straticate/jobs/layout.py` (new) — the job directory and record
  path, importing nothing but `pathlib`.
- `backend/src/straticate/jobs/store.py` (new) — `JobStore`,
  `JOB_INTERRUPTED_CODE`, `interrupted_record`.
- `backend/src/straticate/jobs/manager.py` — store hook, `restore`, optional
  `_JobEntry.executor`.
- `backend/src/straticate/jobs/__init__.py`, `backend/src/straticate/main.py`,
  `backend/src/straticate/api/jobs.py`,
  `backend/src/straticate/api/job_outputs.py`,
  `backend/src/straticate/inference/layout.py`.
- `backend/tests/restart_harness.py`, `backend/tests/test_jobs_persistence.py`.
- `docs/contracts/rest-api.md`, `README.md`, `ROADMAP.md`,
  `frontend/src/api/generated/api.d.ts` (regenerated).

## Acceptance criteria

- [x] A completed job survives a restart: `GET /jobs` lists it in ULID order,
      `GET /jobs/{id}` is byte-identical to the pre-restart record,
      `GET /jobs/{id}/result` is `200`, a stem streams `200`, an export builds.
- [x] A record found in a non-terminal state becomes `failed` with
      `error.code == "job_interrupted"`, is rewritten on disk, and is never
      enqueued.
- [x] Restoring emits **no** events.
- [x] `cancelled` and `failed` records restore verbatim.
- [x] A job directory with no `job.json`, an unparseable record, a record whose
      ID disagrees with its directory, and a leftover `*.tmp` are each skipped
      without failing startup.
- [x] The record exists on disk, in state `queued`, as soon as `POST /jobs`
      has answered `201`.
- [x] No API shape change: `JobState` is untouched and no schema moved. The
      regenerated `api.d.ts` differs only in two route descriptions.
- [x] Backend quartet green; 956 tests pass (935 before, 21 new).

## Required tests

`backend/tests/test_jobs_persistence.py` — the headline restart, ULID ordering
across a restart, both interrupted states, no-events-on-restore (twice: through
the app and directly on the manager), verbatim `cancelled`/`failed`/`completed`
restores, the four things startup ignores, persist-at-submit, terminal record
written before the terminal event, two lifespans of one application, and the
store's own behaviour (atomic publish, empty data directory, selective
normalization, a store that raises).

## Notes / decisions

### Why a sidecar per job, and why it is the wire shape

The record is `Job.model_dump_json()` — the same model the API serves. There is
no second schema to keep in step, and a record read back is validated by the
model that wrote it (AGENTS.md principle 2). It lives *inside* the job's own
directory, beside the stems and exports it describes, so a job directory is
self-describing: nothing can be half-deleted into a record naming stems that
are gone, and a single-file registry can never disagree with the directory
tree. A registry file would also have to be rewritten on every job; a sidecar
is written twice, ever, and only by the job it belongs to.

### `fsync` for completed records only — the trade, stated

`os.replace` is atomic for the directory entry, not for the file's data: after
a power loss a "written" record can come back empty or torn. What that costs
differs by state, so the cost paid differs by state.

- A lost **completed** record strands real work — stems that took GPU-minutes,
  in a directory nothing can name — and it is unrecoverable, because nothing
  re-derives a `SeparationResult` from a stems directory. So a completed record
  is flushed and `fsync`-ed before the rename, and its directory synced after
  it where the platform has a handle, exactly as `models/installer.py` does for
  weights.
- A lost **failed** or **cancelled** record costs nothing: the next boot finds
  the older, non-terminal record and normalizes it to `job_interrupted`, which
  is a *true* statement about a job whose ending was never durably recorded.

This is the same argument `models/installer.py` documents, decided per state
instead of once. The `fsync` runs on the event loop, once per completed job,
for the same reason the installer's does — moving it to a thread would buy a
sub-millisecond saving and cost the atomicity of publish.

### Two writes per job; progress is never one of them

Written at `submit` (so a job answered `201` can never vanish) and at the
terminal transition. Intermediate stages and 4 Hz progress are deliberately not
persisted: **any** non-terminal record becomes `job_interrupted` at startup
regardless of which state it names, so persisting `separating` would buy
nothing and turn a progress report into a disk write. A consequence worth
knowing when reading a record by hand: a running job's record still says
`queued`.

### No events on restore, ever

`restore()` seeds `_entries` and does nothing else. The event stream is a
**live** channel — `job_completed` means "this job just completed" — and
replaying history through it would tell the first browser to connect that a job
from last week had just finished, and would restart the telemetry sampler for
it. There is also no client attached at startup to receive them. REST reads are
how a client learns about the past; that is what `GET /jobs` is for. The tests
prove this both through the application (a manager subclass that attaches a
listener in its own constructor, before `restore` runs, with a subsequent real
job as an ordering barrier) and directly on the manager.

### Never re-queued, never `cancelled`

An interrupted job is not restarted. The executor closure that would run it —
a built separator, a resolved device, a decoded input path — cannot be
reconstructed from a record, and silently re-running heavy inference at startup
would be the application acting unasked (feature 032's rule). It is not
reported `cancelled` either: nobody cancelled it, and a client can only tell
"the server went away" from "I pressed cancel" if the two say different things.
`failed` + an error code is how every other failure reason is already
expressed, so no new `JobState` member was needed — which is what kept this
feature out of the shared schema entirely.

An orderly shutdown is different and is tested: the manager already cancels a
running job at `aclose`, that transition is persisted, and the job comes back
`cancelled`. `job_interrupted` is what a crash, a kill, or a power loss leaves.

### A failing store never fails a job

`JobStore.save` raises honestly; `JobManager._persist` catches, logs with a
traceback, and carries on. The manager's whole posture is that no single job
can stall the queue or die half-way through a terminal transition, and a full
disk is exactly when that matters: raising would turn a separation that really
did complete into `separation_failed`, and raising from `submit` would turn a
resolvable request into a `500`. Startup takes the same line — an unreadable
record is logged and skipped, and one bad file never stops a server booting.

### Where the layout lives (and the import cycle that decided it)

`job_record_path` is in a **new** `jobs/layout.py` rather than in
`inference/layout.py`, and that was forced rather than chosen. `inference`
already imports `jobs` (`inference/base.py` needs `CancellationToken`,
`inference/executor.py` needs `JobContext`), so a store that imported
`inference.layout` closed the loop through a half-initialised
`straticate.inference.base` — verified by an `ImportError` before the move.
`jobs/layout.py` imports nothing but `pathlib`, `inference/layout.py`
re-exports `job_output_dir` and `JOBS_DIRECTORY` from it, and every existing
import site is unchanged. The dependency now runs one way.

### The restart harness, for 058-060

`backend/tests/restart_harness.py` is the shared piece: `running_app(app)` runs
one application's lifespan and yields a client; `write_job_record` /
`read_job_record` are the crash simulator. Its docstring records the three
rules — build the second application from scratch (re-entering one app object's
lifespan proves less, and is tested separately as its own case), let the first
lifespan exit before the second starts, and no sleeps. Deletion, pruning and
disk-usage features should reuse it rather than re-deriving it.

## Known limitations

- **Orphan job directories are invisible.** A directory with no `job.json` —
  everything produced before this feature, or a job whose record never landed —
  is ignored at startup and never listed. Nothing deletes it either. Surfacing
  or pruning them belongs to 058/060.
- **Uploaded-audio records are still in memory** (feature 056's subject), so a
  restored job's `audio_id` may name an upload the running server no longer
  knows about. Nothing in the result, stem or export path consults the audio
  registry, so this affects only a client that tries to re-run the job.
- **The `queued` record of a running job is stale by design.** Anyone reading
  `job.json` out of band sees `queued` for a job that is mid-separation; the
  live state is `GET /jobs/{id}`.
- **Nothing bounds the number of records.** Startup reads every job directory,
  so a data directory with tens of thousands of jobs pays a linear scan. The
  retention policy that would bound it does not exist yet (058/060).
