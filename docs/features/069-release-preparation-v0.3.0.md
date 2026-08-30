# [069] Release preparation for v0.3.0

Branch: `069-release-preparation-v0.3.0`
Status: MERGED
Dependencies: 056, 057, 058, 059, 060, 061, 062, 063, 064, 065, 066, 067, 068
PR: #105

## Objective

Everything that must be true before the `dev → main` release PR for
**v0.3.0**, mirroring feature 055's precedent for v0.2.0 (and 043's for
v0.1.0) exactly: `CHANGELOG.md` written from the ledger for users, version
bumped to `0.3.0`, `ROADMAP.md`'s M5 section and Current state updated, and
the whole workflow — including durability across a restart, deletion,
disk-usage reporting, prune, `mono_bass` and the timeline's job-scoped
session and reload persistence — verified from a clean clone.

## Scope

- **`CHANGELOG.md`** — a `## [0.3.0]` section, dated 2026-08-30, written for
  users from features 056–068's docs, covering three themes: durability &
  housekeeping (056 durable upload registry, 057 durable job records +
  `job_interrupted` recovery, 058 job deletion + exports authority, 059
  disk-usage endpoint, 060 prune endpoint, 061 persisted install failures),
  separation quality (062 `mono_bass` band-limited fold, measured on one
  track; 063 wide-stereo detection endpoint, its suggestion built and held
  disabled) and timeline polish (064 stem retry + hygiene, 065 job-scoped
  stem session, 066 view state survives reload, 067 lane height + fader
  a11y, 068 auto-follow suppressed inside a loop region). The measured
  caveats are stated plainly rather than implied away — see below.
- **Version bump.** `backend/pyproject.toml` `0.2.0` → `0.3.0`; `uv sync
  --extra torch` re-pinned the `straticate` entry in `backend/uv.lock` to
  match, the same generated-file mechanics 043 and 055 recorded.
  `frontend/package.json` stays at `0.0.0` — 043's and 055's reasoning is
  unchanged: the frontend still has no independent release identity, still
  ships as the bundle the backend serves from the same commit, and the one
  version a user can observe is still `GET /api/v1/version`.
- **Contract-change check.** Regenerated `backend/openapi.json` (gitignored)
  and `frontend/src/api/generated/api.d.ts` — no schema changed by this
  feature, and `info.version` came back `0.3.0`; `api.d.ts` came back
  byte-identical (empty diff), the same finding 055 made for the same
  reason: a version-only OpenAPI change has nothing for `openapi-typescript`
  to emit.
- **`ROADMAP.md`** — M5's requirements table converted to done-states with
  feature and PR numbers (mirroring M4's style), row 063 flipped from `PR
  OPEN` to `MERGED` (#104 — it had landed on `dev` before this feature
  branched, just with a stale ledger row), row 069 itself set to `PR OPEN`,
  the Current state section's header and opening updated for v0.3.0
  prepared with the v0.2.0-and-earlier history kept below it, and the
  fast-vocals (027) reopen-criterion note in "After v0.1.0" updated with
  this release's re-check date and result.
- **The fast-vocals licensing re-check**, per 027's reopen criterion and
  the ROADMAP's "re-checked at each release prep" note — see below.
- **Clean-clone verification**, the largest part of this feature's actual
  work — see below.

## Out of scope

- **Creating the release branch, opening the release PR, merging to `main`,
  or tagging.** Those belong to the project owner (`AGENTS.md`); 043 and 055
  stopped at exactly this line for v0.1.0 and v0.2.0, and so does this
  feature.
- Any behaviour change to the app, and any fix to the known limitations
  documented here or in 056–068 — this is release preparation, not feature
  work.
- Enabling the wide-stereo suggestion (063) — that needs the false-positive
  measurement on user-supplied tracks, which is the project owner's to run
  per 063's own protocol; this feature restates the handoff rather than
  discharging it.
- A second GitHub Actions release workflow change — 046/047 already cover
  the tag-triggered release; nothing here touches `.github/workflows/release.yml`.

## The version bump

One hand edit: `backend/pyproject.toml`, `0.2.0` → `0.3.0`. `uv sync --extra
torch` then rewrote the `straticate` entry in `backend/uv.lock` to match —
committed alongside, same reason 043/055 gave.

**Verified, not assumed**, both from this worktree and from the clean clone
described below:

| what | where | result |
| --- | --- | --- |
| `uv sync --extra torch` install line | this worktree and the clean clone | `+ straticate==0.3.0 (from file:///…/backend)` |
| `GET /api/v1/version` | clean clone, three separate server starts | `{"version":"0.3.0"}` every time |
| `tests/test_version.py` — 029's drift test, 3 tests | this worktree, inside the full suite; also run standalone in the clean clone | passed |

### Version-string drift, grepped rather than assumed

`grep -rn "0\.2\.0"` across the repository (excluding `node_modules`,
`.venv` and `package-lock.json`) turned up ten files. Three needed a change
and were fixed: `backend/pyproject.toml` (above), `backend/uv.lock`
(regenerated to match), and `README.md`, whose one occurrence was the
current-version status header (`**v0.2.0 — the timeline release.**`),
rewritten for v0.3.0 — on this head `README.md` contains no `0.2.0` string
at all. The remaining seven are **historical references, correctly left
alone**: `CHANGELOG.md`'s own `[0.2.0]` section (the previous release's
frozen record); `docs/features/046-release-workflow.md`,
`055-release-preparation-v0.2.0.md`, `067-lane-height-a11y.md` and
`068-auto-follow-loop.md` (merged feature docs, historical records per
064's own precedent — "Merged feature docs are historical records… left as
written"); `frontend/e2e/layout.spec.ts`'s one comment naming "the 17/18/20
px v0.2.0 clipping" it regression-tests against (describing when a bug
shipped, not a current version string); and `ROADMAP.md`, which carries the
v0.2.0 *history* deliberately below its now-updated v0.3.0 state, mirroring
exactly how 055 kept v0.1.0's history below its own update. `frontend/package.json` and `frontend/package-lock.json` were
checked and are unaffected either way — both are `0.0.0`, undisturbed by
this bump, per the standing decision below.

`frontend/package.json` stays at `0.0.0`. 043's and 055's reasoning is
unchanged by anything in 056–068: the frontend still has no independent
release identity, still ships as the same bundle the backend serves from
the same commit, and the single version a user can observe is still `GET
/api/v1/version`. Nothing in this feature's scope changed that picture, so
`npm install --package-lock-only` was not run — there is no version to
re-pin.

### The contract-change check

```
cd backend && uv run python -m straticate.scripts.export_openapi
cd frontend && npm run generate:api
```

No schema changed in this feature. `backend/openapi.json` (gitignored)
regenerated with `info.version: "0.3.0"`. `frontend/src/api/generated/api.d.ts`
**is** tracked, and `git diff --stat` against it after regeneration was
empty — the same finding 055 made for the same reason:
`openapi-typescript` never emits `info.version` anywhere in the generated
TypeScript, so a version-only OpenAPI change cannot appear in that file at
all. Nothing was committed for it because nothing changed.

## The fast-vocals licensing re-check (feature 027)

Feature 027's reopen criterion is unchanged since it closed `WONTFIX` on
2026-08-25: "A weights licence stated by a party with standing to grant
it." `ROADMAP.md`'s M5 planning note commits to re-checking this at every
release prep, so it was re-checked here, live, on **2026-08-30**, rather
than assumed carried-forward from 027's own text:

| what was checked | how | result |
| --- | --- | --- |
| `Anjok07/ultimatevocalremovergui` issue #2341 ("Question about bundling UVR models (Voc_FT) in a commercial app") | `gh issue view 2341 --repo Anjok07/ultimatevocalremovergui` | **Still open.** One comment total, from a second unrelated third party (`vadandra`, `authorAssociation: NONE`), dated 2026-08-22 — the same comment 027's own investigation recorded five days later; nothing has been added since. No maintainer reply. |
| That repository's `LICENSE` file on its default branch | `curl -o /dev/null -w '%{http_code}' https://raw.githubusercontent.com/Anjok07/ultimatevocalremovergui/master/LICENSE` and `gh api repos/…/license` | **Still 404s**, both ways. |
| Issue #1798 ("[BUG] License file not found") | `gh issue view 1798 --repo Anjok07/ultimatevocalremovergui` | **Still open.** Last activity 2025-08-01 ("2025/7 bump — Request updated!"), no maintainer response since. |

**Result: unchanged.** No party with standing has stated a licence for the
MDX-family weights `027` needs; the fast `vocals` tier stays blocked for
exactly the reason it was closed. `CHANGELOG.md`'s `[0.3.0]` *Licensing*
section and `ROADMAP.md`'s "After v0.1.0" note both carry this date and
result.

## The clean-clone verification

Same rule 043 and 055 used: type what `README.md` says, in a directory that
has never built this project, and drive the real workflow rather than
assert it should work — this time with a fourth thing to prove that 043 and
055 did not have: **that the application survives being killed.**

**Setup.** `git clone --branch 069-release-preparation-v0.3.0` of this
worktree's local repository into an empty scratch directory on Windows 11
(`C:\swt\069v`, short path for the same MAX_PATH reason this feature's own
worktree uses one), with the same toolchain 043/055 recorded: `uv 0.8.23`,
Node v24.11.1 / npm 11.6.2, FFmpeg 9.0.1-essentials and Git 2.55.0 on
`PATH`. Nothing was carried over: no `.venv`, no `node_modules`, no
`frontend/dist`, no `backend/data`, no installed weights — verified by
starting from a directory `git clone` had just created.

**Following `README.md` verbatim:**

| step | wall clock |
| --- | --- |
| `cd frontend && npm ci` | 6 s |
| `npm run build` | 6.4 s (415 ms of it `vite build`) |
| `cd backend && uv sync --extra torch` | 10.2 s |
| `uv run python -m straticate` | serving on `127.0.0.1:8000`; startup complete ~5 s after "Waiting for application startup" |

`GET /api/v1/health` → `{"status":"ok"}`, `GET /api/v1/version` →
`{"version":"0.3.0"}`.

**Model install, over the API** (as 043/055 did — the install button in the
UI was not clicked in this run): `POST
/api/v1/models/standard-stems-001/install`. The 84,141,911-byte weights
reported `installed` on the very first poll, a few seconds later.

**Two synthetic fixtures**, generated with FFmpeg into the scratch
directory (never committed, per `AGENTS.md`/`DEVELOPMENT.md`): a 20-second
hard-panned stereo file (110 Hz sine on the left channel only, 440 Hz sine
on the right channel only — deliberately wide, to give `mono_bass` and the
wide-stereo analysis endpoint something real to measure) and a 90-second
220 Hz mono-content stereo file, used only to give a job enough runtime to
kill the server mid-separation.

**The real workflow, driven directly against the running server with
`curl`** rather than through the browser this time — the previous two
release-prep features already proved the browser path (048–054's UI, in
055) end to end, and this feature's new surface is entirely backend
endpoints plus session-and-persistence behaviour 065/066 already cover with
their own Playwright stages (`separation.spec.ts`), not a new browser
workflow. What follows is what was actually run, not what should have
worked:

1. Uploaded the wide-stereo fixture (`POST /audio`). The Select step's own
   metadata came back correctly: `0:20 · WAV · Stereo · 44,100 Hz · 16-bit`.
2. `GET /audio/{id}/analysis` → `{"l_r_correlation": 0.0, "wide_stereo":
   true}` — the analysis endpoint (063) measuring a real hard-panned file
   and correctly flagging it, end to end.
3. Installed Standard Stems, started a job with `stereo_handling:
   "mono_bass"` against the wide fixture. **Real inference, not a stub**:
   `processing_seconds` **10.813**, `realtime_factor` **1.850** for 20.0 s
   of audio on CPU — consistent with 062's own measured range and with
   043's/055's CPU figures for this model on this class of hardware. All
   four stems came back **2-channel** (`channels: 2` in the result record),
   proving `mono_bass` keeps the stereo image — a full `mono` fold would
   have returned 1-channel stems here, which is exactly the distinction
   062's wiring tests exist to pin.
4. Built an export (`GET /jobs/{id}/export?format=flac&stems=…`) — `200`,
   1,508,081 bytes — to give the disk-usage and prune steps below something
   real to classify and reclaim.
5. **Interrupted a real job.** Uploaded the 90-second fixture, started a
   second job with no special options, confirmed it was genuinely mid-flight
   (`GET /jobs/{id}` → `"state":"separating","progress":0.25`), then
   **force-killed the server process** (`taskkill /F`, not a graceful
   shutdown — the crash 057's `job_interrupted` path exists for). Restarted
   the process from the same `backend/` directory.
   - `GET /api/v1/health` and `GET /api/v1/version` answered normally
     within the same ~5 s startup window as the first boot.
   - The interrupted job came back exactly as 057 documents:
     `"state":"failed"`, `"error":{"code":"job_interrupted","message":"The
     server stopped while this job was queued or running."}` — never
     re-queued, never silently resumed.
   - The **completed** `mono_bass` job from step 3 was untouched: `GET
     /jobs/{id}` byte-for-byte the same state, `GET /jobs/{id}/result` →
     `200` with the identical result record, `GET
     /jobs/{id}/stems/vocals` → `200`. `GET /jobs` listed both jobs. `GET
     /audio/{id}` for the original upload still answered `200` — the
     upload registry (056) surviving the same crash as the job record
     (057).
   - No `.venv`, no cached process state, no in-memory anything: this was a
     second, independent Python process reading only what `056`/`057`'s
     sidecars had written to disk before the kill.
6. **Exercised deletion.** `DELETE` on an unknown job ID → `404
   job_not_found`. Started a fresh job, tried to delete it while
   `"state":"separating"` → `409 job_active` with `detail.state ==
   "separating"`, exactly as 058 documents. Cancelled it, waited for the
   terminal event, deleted it → `204`; a repeat `GET` on the same ID → `404`.
7. **Disk usage, seeded and checked.** `GET /system/disk-usage` reported
   real, non-zero `uploads`/`job_stems`/`job_exports` buckets and
   `"complete": true`. A hand-made orphan directory
   (`data/audio/orphan-test-id/`, containing a file `AudioStore` never
   registered) was picked up as `"orphans": {"count": 1, "bytes": 22}` on
   the very next call — 059's classifier working against real, uncontrolled
   disk state, not a fixture built for it.
8. **Pruned in three passes**, one class per call, checking the effect of
   each before moving to the next:
   - `{"export_caches": true}` → freed the one export built in step 4
     (`items_removed: 1, bytes_freed: 1508081`). Re-requesting the same
     export afterward rebuilt it — `200`, 1,508,080 bytes (1 byte off the
     original; zip archives embed a build timestamp, so byte-identical was
     never the right bar — the *content* being reconstructible from
     surviving stems is, and it was).
   - `{"orphans": true}` → freed the hand-made orphan
     (`items_removed: 1, bytes_freed: 22`); the next `GET
     /system/disk-usage` read `"orphans": {"count": 0, "bytes": 0}`.
   - `{"terminal_jobs": true}` → freed both remaining terminal jobs
     (`items_removed: 7, bytes_freed: 15622159` — the completed job's
     record + 4 stems + rebuilt export, plus the interrupted job's record);
     `GET /jobs` afterward listed `0`.
   - A fourth, identical call requesting all three classes at once
     afterward froze at `items_removed: 0, bytes_freed: 0, failures: []` —
     idempotence, measured rather than assumed.
9. **A third restart**, after the prune, confirmed the pruned jobs stay
   gone: `GET /jobs` on the freshly restarted process still listed `0`.
10. **No warnings or errors in any of the three server logs** across the
    whole run — each log's only non-"INFO" text was the string "error"
    appearing inside the `uvicorn.error` *logger name* on ordinary startup
    lines, not an actual error entry.

**What this run does not cover, stated plainly:** it drives the backend
directly rather than through a browser, so it does not re-verify the
*frontend* halves of 065's session hoist or 066's reload persistence
(playhead/loop/zoom surviving an unmount or a `page.reload()`) — those are
already covered end to end by `frontend/e2e/separation.spec.ts`'s and
`resync.spec.ts`'s own stages (066) and by `stemSession.test.tsx` and
`StemPlayer.test.tsx` (065) inside the full frontend gate suite below, not
re-driven a second time here. It also does not click the in-app
model-install button (see step 3) or listen to any audio by ear — the same
limitations 043 and 055 recorded, for the same reasons.

### Gate results — this worktree

**Backend** (`cd backend`, all against `--extra torch`):

| check | result |
| --- | --- |
| `ruff format --check .` | 124 files already formatted |
| `ruff check .` | all checks passed |
| `pyright` | 0 errors, 0 warnings, 0 informations |
| `pytest` | **1095 passed**, 12 deselected (the opt-in `integration` tier), 226.85 s |

**Frontend** (`cd frontend`):

| check | result |
| --- | --- |
| `format:check` | all matched files use Prettier code style |
| `lint` | clean |
| `typecheck` | clean |
| `test` | **1102 passed**, 44 test files |
| `build` | `tsc -b && vite build`, 290.56 kB JS / 26.96 kB CSS |

**Targeted evidence runs**, all inside the 1095/1102 totals above, run
standalone as well to isolate them:

| files | result |
| --- | --- |
| `test_jobs_persistence.py`, `test_audio_durability.py` (restart survival) | 33 passed |
| `test_api_job_deletion.py`, `test_disk_usage.py`, `test_prune.py` (delete/disk-usage/prune) | 60 passed |
| `test_stereo_handling.py`, `test_stereo_analysis.py` (`mono_bass` + analysis) | 72 passed, 1 deselected |
| `test_model_installer.py` (persisted install failures) | 49 passed |

### Gate results — the clean clone

Run independently, from a second `.venv`/`node_modules` this worktree never
touched:

| check | result |
| --- | --- |
| backend `ruff format --check .` | 124 files already formatted |
| backend `ruff check .` | all checks passed |
| backend `pyright` | 0 errors, 0 warnings, 0 informations |
| backend `pytest` | **1095 passed**, 12 deselected, 211.12 s — identical counts to this worktree |
| frontend `format:check` | all matched files use Prettier code style |
| frontend `lint` | clean |
| frontend `typecheck` | clean |
| frontend `test` | **1102 passed**, 44 test files, 47.22 s — identical counts to this worktree |
| frontend `build` | `tsc -b && vite build`, 290.56 kB JS / 26.96 kB CSS, 415 ms |

## Acceptance criteria

- [x] `CHANGELOG.md` has a user-facing `[0.3.0]` entry, written from the
      056–068 feature docs, stating `mono_bass`'s one-track measurement and
      the wide-stereo suggestion's hold plainly
- [x] `backend/pyproject.toml` at `0.3.0`; `uv.lock` regenerated to match;
      `GET /api/v1/version` reports `0.3.0` on every one of three
      independent server starts in the clean clone; 029's drift test
      (3 tests) passes
- [x] Contract-change check performed: `openapi.json` regenerated
      (`info.version: 0.3.0`), `api.d.ts` regenerated and diffed — empty
- [x] `ROADMAP.md`'s M5 table converted to done-states with feature/PR
      numbers, row 063 flipped to `MERGED`, own row (069) set to `PR OPEN`,
      Current state updated, fast-vocals reopen criterion re-checked live
      and dated
- [x] The fast-vocals (027) licence reopen criterion re-checked against
      live sources, not assumed carried-forward; result recorded with date
- [x] The workflow verified from a clean clone, including: restart
      survival of a completed job, the upload registry, and a genuinely
      interrupted (force-killed) job recovering as `job_interrupted`;
      deletion (`404`/`409`/`204`); disk-usage reporting against real and
      hand-made orphan state; prune across all three classes plus
      idempotence; `mono_bass` producing real 2-channel stems from a real
      separation; the wide-stereo analysis endpoint measuring a real
      hard-panned fixture
- [x] Known limitations stated where a user will meet them, including what
      056–068 ship with (interrupted jobs do not resume, prune is manual
      only, `mono_bass` and the wide-stereo threshold are one-track
      measurements, the suggestion itself is held)
- [x] All five frontend gates green in both this worktree and the clean
      clone; backend's full quality bar green in both

## Notes / decisions

1. **The clean-clone workflow was driven with `curl` against the real
   server, not through a browser.** 043 and 055 already proved the
   browser-driven workflow for the parts of the application a browser
   changes (upload picker, job creation, the Inspect timeline's UI); M5's
   new surface is entirely REST endpoints (deletion, disk-usage, prune,
   analysis) plus session/persistence behaviour that 065 and 066 already
   drive through Playwright in their own PRs. Re-deriving a browser
   automation script for endpoints that have no UI yet (058–060, 063's
   endpoint) would have tested nothing a `curl` call does not test more
   directly, and would have left the one thing worth proving new — that
   the *process* itself survives being killed — no better proven either
   way.
2. **The interruption test used a real `taskkill /F`, not a simulated one.**
   057's own test suite proves `job_interrupted` recovery through a
   restart-harness abstraction (`backend/tests/restart_harness.py`) that
   never actually kills a process — by design, so the unit suite stays
   fast and deterministic. This feature's job was to prove the same
   behaviour survives an actual OS-level process kill, on the actual
   `python -m straticate` entry point a user runs, which the harness
   cannot exercise by construction.
3. **The re-exported FLAC zip was one byte different from the first
   export, not byte-identical.** Investigated rather than waved away: both
   zips extract to the same four FLAC files from the same surviving stems
   (058's and 060's own claim is that stems and record survive an
   `export_caches` prune, not that the *archive bytes* are pinned), and the
   one-byte difference is consistent with a zip container's embedded
   build timestamp. Nothing in 058/060's acceptance criteria claims
   byte-identical zips; the claim checked here is the one actually made:
   the artifact is reconstructible.
4. **Model weights were installed fresh in the clean clone, not carried
   over from this worktree**, matching 043's and 055's rule even though
   this worktree already had both models downloaded from earlier release
   preps on this machine. The 80.2 MB Standard Stems download completed
   and verified within the first poll after `POST …/install`.
5. **The `Anjok07/ultimatevocalremovergui` licensing check used `gh` and
   `curl` against the live repository, not a cached summary.** Both issues
   (#2341, #1798) were re-fetched directly; the `LICENSE` 404 was checked
   both via a raw-content request and via GitHub's own License API, which
   agreed (`404` both ways).
6. **The `045` ledger-row precedent stands.** This feature touched only its
   own row (069) and 063's (which was genuinely stale — 063 had already
   landed on `dev` before this branch was cut) in `ROADMAP.md`'s ledger
   table, per `AGENTS.md`'s rule against rewriting other features' rows.

## Known limitations

Everything 056–068 ship with, from their own Known Limitations sections,
carried into `CHANGELOG.md`'s `[0.3.0]` "What it cannot do" for users —
repeated here for the ledger's own record, at the same rigor 043 and 055
used:

- **An interrupted job does not resume**, and is never re-queued or
  reported `cancelled` — it is `failed`/`job_interrupted`, which this
  feature's own clean-clone run reproduced against a real, force-killed
  process rather than only the unit-level harness (057).
- **Nothing prunes automatically.** `POST /system/prune` is manual and
  opt-in per class; no policy, schedule or background sweep exists yet
  (060, deferred to v0.4.0). A file held open by an in-flight download can
  survive a delete or prune as debris on Windows until whatever holds it
  closes (058, 060).
- **`mono_bass`'s crossover (500 Hz, Linkwitz-Riley 4th-order) is measured
  on one track** — the same wide-stereo mix used to measure 0.1.0's
  `mono` fold — and is a fixed constant, not user-adjustable (062). It
  recovers the `bass` stem; it does not cleanly separate it — 19.4% of the
  source's low-frequency energy lands there, `other` still holds 37.5%.
- **The wide-stereo suggestion is measured but not shown to anyone.**
  `GET /audio/{id}/analysis` ships and was exercised end to end against a
  real hard-panned fixture in this feature's own clean-clone run, but the
  in-app note is disabled behind `WIDE_STEREO_SUGGESTION_ENABLED = false`
  until the false-positive rate is measured on 10–20 user-supplied
  ordinary tracks per 063's protocol — **still outstanding, and it is the
  project owner's to run**, not this feature's; the material cannot live
  in this repository (063, *The false-positive measurement protocol*).
- **Playhead, loop region and zoom state now survive leaving Inspect and a
  page reload** (065, 066) — the one 0.2.0 caveat this release resolves.
  What still does not survive a reload is the *decoded audio itself*
  (C12/v0.4.0): the engine rebuilds and every stem re-downloads.
- **The rest of 0.1.0's and 0.2.0's pre-existing caveats, untouched by
  056–068, still stand**: `vocals` mode has no fast tier (~0.3× real time
  on CPU; the licence gate is unchanged — re-checked above); the Demucs
  weights are research-use-only; a 24-bit/32-bit-float export adds no
  detail; one job at a time with no history; model downloads are not
  resumable and installed weights are not re-verified after install;
  exports are still buffered in the browser tab with no progress indicator
  and cannot be cancelled; cancelling a running separation still takes
  effect at the next chunk boundary; there is still no model *update*
  path.
- **One clean-clone run**, on Windows 11, with synthetic fixtures and the
  small Standard Stems model — the same scope limitation 043 and 055
  recorded, for the same reasons. Not tested on Linux or macOS by hand; CI
  covers Ubuntu on every PR.
- **`CHANGELOG.md`'s `[0.3.0]` link points at a tag that does not exist
  yet.** It resolves the moment the project owner pushes `v0.3.0` — the
  same situation 043 and 055 recorded and the same step this feature does
  not take.

## What remains — the project owner's, per `CONTRIBUTING.md`

Quoting [CONTRIBUTING.md](../../CONTRIBUTING.md#release-process) rather than
inventing a process:

1. "When `dev` reaches a release milestone, prepare the release (version
   bumps, changelog, docs) — directly on `dev` via a numbered feature, or on
   a `release/vX.Y.Z` branch cut from `dev` if stabilization needs
   isolation." — this feature is that preparation, done directly on a
   numbered branch off `dev` (`069-release-preparation-v0.3.0`), matching
   how 043 and 055 did it.
2. "Open a release PR `dev` (or `release/vX.Y.Z`) → `main`." — not opened by
   this feature.
3. "**Rebase-merge it.** Not a merge commit, and not a squash" — the
   project owner's action, once the PR above exists.
4. "Create an **annotated** tag on `main`'s new tip and push it":
   ```sh
   git switch main && git pull
   git tag -a v0.3.0 -m "Straticate v0.3.0"
   git push origin v0.3.0
   ```
5. Pushing the tag runs `.github/workflows/release.yml` (046/047), which
   publishes the GitHub Release once it confirms the tag is annotated, the
   tagged commit is reachable from `main`, and the tag, `pyproject.toml` and
   the `## [0.3.0]` heading in `CHANGELOG.md` all agree — which they now do,
   verified in this feature (`0.3.0` in all three places).
6. "Any release-branch fixes are reconciled back into `dev`."
7. **The false-positive measurement for the wide-stereo suggestion (063)**
   is not a release-blocking step, but it is the one open item this feature
   hands to the project owner directly: 10–20 user-supplied ordinary
   tracks, run through the protocol in `docs/features/063-wide-stereo-detection.md`,
   before `WIDE_STEREO_SUGGESTION_ENABLED` is flipped in a future feature.
