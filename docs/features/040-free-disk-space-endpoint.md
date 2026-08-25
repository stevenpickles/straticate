# [040] Free-disk-space endpoint for installs

Branch: `040-free-disk-space-endpoint`
Status: PR OPEN
Dependencies: 025, 037
PR: #49

## Objective

The UI can now say "870 MB will be written to the machine running Straticate.
2.1 GB is free there." before somebody starts a download that cannot finish —
because the backend reports the free space beside `models_dir`, and both places
an install is offered compare the two.

## Why the frontend could not do this itself

Feature 037 investigated this and **reported it rather than improvising an
endpoint**; it was the one genuine backend gap its audit found. The weights are
written by the *backend*, to `Settings.models_dir`, on the machine running
Straticate. The one disk figure a browser can obtain —
`navigator.storage.estimate()` — describes the quota of the *page's own origin*
inside the *browser's* profile directory: a different number about a different
disk, and rendering it would be worse than silence because it would look like
an answer. So 037 stated the limitation honestly wherever an install is
offered. This feature replaces the admission with a fact, and keeps the
admission for the case it was written for: no figure.

## What was built

### Backend

- **`backend/src/straticate/schemas/storage.py`** — `StorageReport`:
  `free_bytes` and `total_bytes`, both `int | None`.
- **`backend/src/straticate/system/storage.py`** — `read_disk_usage` (the
  platform seam over `shutil.disk_usage`), `nearest_existing_dir`, and
  `storage_report`, which never raises.
- **`backend/src/straticate/api/system.py`** — `GET /system/storage`, beside
  018's `GET /system/devices` in the same router.
- `scripts/export_openapi.py` lists the new schema among its root models;
  `frontend/src/api/generated/api.d.ts` is regenerated and committed.

### Frontend

- **`frontend/src/api/system.ts`** — `getSystemStorage()`.
- **`frontend/src/state/diskSpace.tsx`** — `DiskSpaceProvider` /
  `useDiskSpace`: one reading for the whole page, read where an install is
  offered and again when the disk demonstrably changed. **No timer.**
- **`frontend/src/components/diskFit.ts`** — the pure comparison
  (`fits` / `tight` / `insufficient` / `unknown`) and its two thresholds.
- **`frontend/src/components/DiskCostNotice.tsx`** — 037's notice, now stating
  the comparison, and falling back to 037's own sentence when there is no
  figure. Because both install affordances already render this component,
  `ModelInstallPanel` (035) and `ModelCard` (037) needed **no change at all**.
- **`frontend/src/components/useModelInstallation.ts`** — one added effect and
  one exported pure rule (`installationChangedDisk`): when weights land, are
  removed, or a download ends without installing, the held figure is stale.
- **`frontend/src/App.tsx`** — mounts the provider once, above both trees.

## Out of scope

- Quotas, cleanup or retention for existing artifacts. Export artifacts and job
  outputs still accumulate unboundedly (recorded by 021, 022 and 025, and
  unclaimed); this feature only *mentions* that fact, in the argument for the
  512 MB headroom. **Still unclaimed.**
- Reporting free space anywhere other than where an install is offered.
- Any change to the installer's contract. `POST /models/{id}/install` behaves
  exactly as 025 built it — see the decision below.

## Decision: warn, never refuse

**The app never blocks an install it believes will not fit.** It states the
comparison plainly, styles a download that cannot fit as strongly as a failure,
says what will happen if it is started anyway — and leaves the button working.

Five reasons, in the order they decided it:

1. **The reading expires immediately.** Free space is a fact about one moment.
   Another process (or this application's own job outputs and exports, which
   nothing prunes) can consume gigabytes between the check and the write, and a
   user who frees space in another window would have to be told to reload. A
   refusal derived from it would be enforcing a number that was true a moment
   ago.
2. **A wrong reading is possible, and a false refusal is unrecoverable.**
   `shutil.disk_usage` answers for the filesystem, not for a per-user quota, a
   thin-provisioned volume, a network mount or a container layer; on Windows it
   reports the caller's quota-adjusted free space. On the one screen that
   exists to get weights onto the machine, refusing a download that would have
   worked leaves the user with no way forward at all — no override, no
   explanation they can act on. That is a worse product than a warning they can
   read and overrule.
3. **Failing is already cheap and safe.** Feature 025 streams to a `.part`
   sibling, verifies SHA-256, publishes with `os.replace`, and unlinks the
   partial file in a `finally` on every exit. A disk-full install therefore
   costs bandwidth and time, reports `download_failed` with
   `detail.reason: "filesystem_error"`, and leaves nothing behind — no
   corruption to clean up, and the retry button is already on screen.
4. **Refusing would change a shared contract for a check that cannot be
   trusted.** It would mean a new `409` from `POST /models/{id}/install` (and a
   pre-flight `statvfs` inside the installer), which every client would then
   have to handle — for a guard that is advisory by nature. The assignment's
   own instruction was to stop and report if this needed an installer-contract
   change; it does not, because warning is the better behaviour anyway.
5. **Unknown must not block, and consistency is worth having.** Unknown free
   space is the cautious case (below), but refusing on *ignorance* would be
   plainly wrong. Given that unknown cannot block, having "known and too small"
   block would mean the app is most permissive exactly when it knows least.
   Warning in every case is the coherent rule.

What the warning does instead is the part that actually prevents the mistake:
it is on screen **before** the click, next to the price, in the app's own units.

## Decision: unknown is `null`, and unknown is the cautious case

Feature 018 reports an unknown `memory_total_bytes` as `0` and documents it.
That is right there and wrong here: a machine with zero bytes of RAM is
impossible, so `0` cannot be mistaken for a reading — while **a disk with zero
bytes free is both possible and the single most important case to warn about**.
So this contract spells unknown as `null` and keeps `0` for a full disk, and
the frontend's `DiskSpaceStatus` distinguishes `known` from `unavailable` for
the same reason.

037's `DiskCostNotice` already treated an *unmeasured download size* as the
large case, because a size nobody has stated could be anything. The same
reasoning applies to an unmeasured disk, so:

- an unknown figure keeps the warning styling (`disk-cost-large`) whatever the
  download's size, and
- the sentence beside it is 037's own: *"Straticate cannot check that machine's
  free space right now, so make sure there is room before installing."*

Three quite different situations produce it — the host could not answer
(`null`), the request failed, or there is no provider above the component —
and all three leave the user in the same position, so they get the same advice.

## Decision: what happens when `models_dir` does not exist yet

**Report on its nearest existing ancestor.** On a fresh checkout nothing has
been installed and `{models_dir}/weights` does not exist; on a non-editable
install `models_dir` itself may not. Both platform primitives need a path that
exists, so `nearest_existing_dir` walks up until one does.

This is not an approximation: the missing directories are created by the very
install being priced, on that same filesystem, so the figures describe exactly
the disk the bytes land on. Reporting `unknown` instead would make the
commonest case — the first install a user ever does, which is the whole reason
this feature exists — the one case with no answer.

Only when *nothing* at or above the path can be examined (or the primitive
raises) is the answer unknown. A path component that exists but is a **file**
is skipped rather than measured: `{file}/weights` cannot be created, so the
file is not the filesystem the install would use.

## Decision: how a poll was avoided

AGENTS.md principle 3 forbids polling loops, and 025 and 035 both reasoned
carefully about the one timer that exists (a model re-read *while its own
download runs*, at 1 Hz, stopping on every terminal state). Nothing here
schedules anything. A free-space figure only matters at the moment somebody
decides whether to start a download, so it is read at exactly two moments:

1. **When an install is offered.** `DiskCostNotice` calls `ensureRead()` from
   its mount effect — and 037 renders that notice only where an install is
   actually on offer. A session that never installs anything never asks; a
   library with three uninstalled models asks **once**, because a read in
   flight is never duplicated and the figure is held above the cards rather
   than in them.
2. **When the disk demonstrably changed.** `useModelInstallation` calls
   `noteDiskChanged()` when a watched model's state moves in a way that moved
   bytes: a download landing, weights being removed, or a download ending
   without installing (whose `.part` is unlinked). `installationChangedDisk` is
   a pure, tested rule, and a poll reporting the same state changes nothing and
   asks for nothing.

A held reading is reused for `STORAGE_MAX_AGE_MS` (30 s), which is a
*staleness bound on a read that only happens when an affordance mounts* — not a
schedule. `diskSpace.test.tsx` asserts `vi.getTimerCount() === 0` and that
advancing the clock by two minutes issues no request at all.

Why a context rather than a hook per component: free space is one fact about
the whole machine with a lifetime quite unlike an installation record's. A copy
per card would be N requests for one number and N answers that could disagree
with each other on screen at the same time.

## The thresholds

- `LARGE_DOWNLOAD_BYTES` (100 MB) is 037's, unchanged: above it the notice is
  styled as a warning rather than a footnote.
- `TIGHT_HEADROOM_BYTES` (512 MB) is new. A download that fits with nothing to
  spare technically fits and practically does not — the same filesystem holds
  this application's uploads, job outputs and export artifacts, none of which
  are ever pruned — so between `free - size = 0` and `512 MB` the notice says
  it "will fit with little to spare" and keeps the warning styling. It never
  blocks anything either.

## Expected modules/files

- `backend/src/straticate/schemas/storage.py` · `schemas/__init__.py`
- `backend/src/straticate/system/storage.py` · `system/__init__.py`
- `backend/src/straticate/api/system.py` · `scripts/export_openapi.py`
- `backend/tests/test_storage.py` · `backend/tests/test_system.py`
- `frontend/src/api/system.ts` · `system.test.ts` · `types.ts` ·
  `generated/api.d.ts`
- `frontend/src/state/diskSpace.tsx` · `diskSpace.test.tsx`
- `frontend/src/components/diskFit.ts` · `DiskCostNotice.tsx` · `.test.tsx` ·
  `.css`
- `frontend/src/components/useModelInstallation.ts` · `.test.ts`
- `frontend/src/components/ModelInstallPanel.test.tsx` · `ModelCard.test.tsx`
- `frontend/src/App.tsx` · `frontend/e2e/models.spec.ts`
- `docs/contracts/rest-api.md` · this file · `ROADMAP.md`

## Acceptance criteria

- [x] `GET /system/storage` reports free/total for the filesystem holding
      `models_dir`, and **degrades to a documented "unknown"** rather than
      raising — on a missing directory, a permissions failure, and an
      unsupported platform.
- [x] The install affordance (035) and the model library (037) show the
      comparison, and fall back to 037's honest wording when the figure is
      unavailable.
- [x] Unknown free space is the cautious case, consistent with 037's treatment
      of an unknown download size.
- [x] The refuse-or-warn decision is implemented (warn) and justified above.
- [x] No new polling loop; no timer at all in the new code.
- [x] `api.d.ts` regenerated; the contract is documented in
      `docs/contracts/rest-api.md`.
- [x] All backend and frontend gates green; the new backend tests are clean
      under `-W error`; the E2E suite passes (24 tests), with two new specs for
      the behaviour that visibly changed.

## Required tests

**Backend** (`tests/test_storage.py`, 14 tests; `tests/test_system.py`, +3) —
the platform primitive is stubbed at its seam, so **no test fills a disk**:

- the happy path, and that the reader is asked about the models directory;
- a full disk reported as `0` free and *not* as unknown;
- negative readings clamped;
- a missing `models_dir` measured at its nearest existing ancestor, a file in
  the path skipped, and a path with no examinable ancestor reported unknown
  with exactly one warning logged;
- each failure of the primitive — permissions, a vanished path, an unsupported
  platform, an exotic `OSError`, and a reading that makes no sense — degrading
  to `null`/`null` with one warning and no exception;
- two tests against the *real* `shutil.disk_usage` for a directory that exists;
- over HTTP: the real application's report, the shape of the payload, the
  degraded `200` with nulls, and that the figures describe
  `Settings.models_dir` rather than the working directory.

**Frontend** (34 new tests: 4 + 13 + 17 in the three new suites, plus 4, 3 and 7
added to the three existing ones — 116 across the six files):

- `api/system.test.ts` — the request URL, the documented unknown parsed as a
  value rather than a failure, a full disk parsed as `0`, and a typed
  `ApiError` for a failed request.
- `state/diskSpace.test.tsx` — nothing read until an install is offered; one
  request for three simultaneous consumers; unknown and a failed request both
  landing as `unavailable`; a full disk as a known `0`; a re-read when the disk
  changed; the held figure surviving a re-read; a change arriving mid-request
  taking one more reading afterwards; a fresh reading reused by the next
  affordance; a stale one re-read by it; **no timer**; and the no-provider
  default asking for nothing.
- `components/DiskCostNotice.test.tsx` — `diskFit`'s four verdicts and its
  boundaries; the comparison rendered; "will not fit" with no control taken
  away; a full disk; the tight case; an unpublished size with a known disk;
  037's wording for an unavailable figure, a failed request, and no provider;
  and a small download that is a footnote when there is room but the cautious
  case when the space is unknown.
- `components/ModelInstallPanel.test.tsx` — the comparison in the configure
  step, the "will not fit" warning with Install still enabled, the honest
  fallback, and **no request while a download is running** (no install is
  offered then).
- `components/ModelCard.test.tsx` — the same three, in the library.
- `components/useModelInstallation.test.ts` — `installationChangedDisk`'s
  rules, including that a first reading is never a change; no request from
  merely watching a model; a fresh reading when a download settles; none while
  it merely progresses; and one when the weights stop being installed.
- `e2e/models.spec.ts` — two new specs, scripted through `page.route` with
  nothing downloaded and no fixed sleeps: the comparison rendered from a
  scripted `/system/storage`, the same card falling back to "cannot check" when
  the host answers `null`, and a download that cannot fit being installed
  anyway (asserting the UI refused nothing). The existing install spec now
  scripts the endpoint and asserts the comparison in place of 037's sentence.

## Notes / decisions

### No path is on the wire

`StorageReport` carries two numbers and nothing else. The UI has no use for the
server's directory layout, and returning it would publish the running user's
home directory to any browser that can reach the API. The same instinct as
025's, which keeps the artifact's download URL off the wire entirely.

### The endpoint is not cached, and the detector is not reused

018 probes devices once at startup because devices cannot change during a run,
and caches them behind `DeviceDetector`. Neither applies here: free space
changes constantly, so a cached figure would be a lie, and there is no state to
hold between requests. `storage_report` is therefore a plain function reading
`Settings.models_dir` through the existing `SettingsDep`, with no object on
`app.state` and nothing to warm at startup.

### Reported out of scope

- **Nothing prunes what accumulates.** Export artifacts and job outputs grow
  without bound under `data_dir` (021, 022, 025 and now this feature all record
  it). It is the reason a free-space figure can go down without anybody
  installing anything, and the reason for the 512 MB headroom — but retention
  and cleanup remain unclaimed and untouched here.
- **`data_dir` is not reported.** This endpoint answers for `models_dir`,
  because that is where an *install* writes. A separation writes stems and
  exports under `data_dir`, which may be a different filesystem entirely, and
  nothing warns about running out of room mid-job. That is a real gap and a
  different feature (it wants a pre-flight estimate from the audio's duration,
  not a bare figure).
- **A download's `.part` is not counted against the figure.** While an install
  runs, free space is falling; the UI does not show that, because it does not
  offer an install for a model that is already downloading. Cheap to add if a
  reason appears; a reason to poll, if one is not careful.
