# [055] Release preparation for v0.2.0

Branch: `055-release-preparation-v0.2.0`
Status: PR OPEN
Dependencies: 048, 049, 050, 051, 052, 053, 054
PR: (not opened — see Out of scope)

## Objective

Everything that must be true before the `dev → main` release PR for
**v0.2.0**, mirroring feature 043's precedent for v0.1.0 exactly:
`CHANGELOG.md` written from the ledger for users, version bumped to
`0.2.0`, `ROADMAP.md`'s M4 section and Current state updated, and the whole
workflow — including the new waveform-timeline surface — verified from a
clean clone.

## Scope

- **`CHANGELOG.md`** — a `## [0.2.0]` section, dated 2026-08-29, written for
  users from features 048–054's docs: the waveform timeline, zoom/pan,
  audible scrub preview, loop/A-B regions, per-stem faders, the 048
  result-fetch-retry fix, and an honest "what it cannot do" carrying forward
  what still applies from 0.1.0 plus what the timeline ships with (state that
  does not survive leaving Inspect, the lane-header/fader sizing limitation
  at larger root fonts, the auto-follow double page-flip, and that retry
  still covers only the result fetch, not a failed stem download).
- **Version bump.** `backend/pyproject.toml` `0.1.0` → `0.2.0`; `uv sync
  --extra torch` re-pinned the `straticate` entry in `backend/uv.lock` to
  match, the same generated-file mechanics 043 recorded.
  `frontend/package.json` stays at `0.0.0` — 043's reasoning is unchanged: the
  frontend still has no independent release identity (`"private": true`,
  never published, served from the same backend commit), the one version a
  user can observe is still `GET /api/v1/version`, and nothing checks a
  second copy for drift.
- **Contract-change check.** Regenerated `backend/openapi.json` (gitignored)
  and `frontend/src/api/generated/api.d.ts` — no schema changed by this
  feature, and the generated TypeScript does not embed the version string
  anywhere, so `api.d.ts` came back byte-identical and nothing was committed
  for it. See "The version-drift check" below.
- **`ROADMAP.md`** — M4's requirements table converted to done-states with
  feature and PR numbers (mirroring M3's style), a closing statement that
  every M4 requirement is met and what remains is the project owner's; the
  Current state section's header and opening updated for v0.2.0 prepared,
  with the v0.1.0 history kept below it exactly as the file already does;
  own ledger row (055) set to `PR OPEN`.
- **Clean-clone verification**, the heart of 043's precedent and the largest
  part of this feature's actual work — see below.

## Out of scope

- **Creating the release branch, opening the release PR, merging to `main`,
  or tagging.** Those belong to the project owner (`AGENTS.md`; 043 stopped
  at exactly this line for v0.1.0, and so does this feature). Prepared
  everything and left `dev` ready.
- Any behaviour change to the app, and any fix to the known limitations
  documented here or in 048–054 — this is release preparation, not feature
  work. Verification found no defect that needed one (see below).
- A second GitHub Actions release workflow change — 046/047 already cover the
  tag-triggered release; nothing here touches `.github/workflows/release.yml`.

## The version bump

One hand edit, same shape as 043's: `backend/pyproject.toml`, `0.1.0` →
`0.2.0`. `uv sync --extra torch` then rewrote the `straticate` entry in
`backend/uv.lock` to match — committed alongside the `pyproject.toml` edit for
the same reason 043 gave: it is what keeps CI's `uv sync` from finding the
lock out of date.

**Verified, not assumed**, both from this worktree and from the clean clone
described below:

| what | where | result |
| --- | --- | --- |
| `uv sync --extra torch` install line | this worktree and the clean clone | `+ straticate==0.2.0 (from file:///…/backend)` |
| `GET /api/v1/version` | clean clone | `{"version":"0.2.0"}` |
| `tests/test_version.py` — 029's drift test, 3 tests | this worktree, inside the 935 | passed |

### The version-drift check

The assignment asked to regenerate and diff rather than assume the OpenAPI
contract is unchanged. Both artifacts are generated, neither is hand-edited:

```
cd backend && uv run python -m straticate.scripts.export_openapi
cd frontend && npm run generate:api
```

`backend/openapi.json` is gitignored (`.gitignore:16`), so there is nothing to
diff there directly, but its `info.version` field did read `"0.2.0"` after the
regeneration — the single-sourced version (029) doing exactly what it is
supposed to. `frontend/src/api/generated/api.d.ts` **is** tracked, and `git
diff --stat` against it after regeneration was empty: `openapi-typescript`
does not emit `info.version` anywhere in the generated TypeScript (it only
turns `paths`/`components` into types), so a version-only change to the
OpenAPI document cannot appear in that file at all. Nothing was committed for
`api.d.ts` because nothing changed. No schema changed in this feature, so
this is a version bump and its own check, not a schema diff — the mechanism
was still exercised end to end rather than assumed to be a no-op.

## The clean-clone verification

Same rule 043 used: type what `README.md` says, in a directory that has never
built this project, and drive the real workflow rather than assert it should
work.

**Setup.** `git clone --branch 055-release-preparation-v0.2.0` of this
worktree's local repository into an empty scratch directory on Windows 11,
with the same toolchain 043 recorded: `uv 0.8.23`, Node v24.11.1 / npm
11.6.2, FFmpeg 9.0.1 and Git 2.55.0 on `PATH`. Nothing was carried over: no
`.venv`, no `node_modules`, no `frontend/dist`, no `backend/data`, no
installed weights.

**Following `README.md` verbatim:**

| step | wall clock |
| --- | --- |
| `cd frontend && npm ci` | 6.3 s |
| `npm run build` | 5.6 s |
| `cd backend && uv sync --extra torch` | 20.1 s |
| `uv run python -m straticate` | serving on `127.0.0.1:8000`; startup complete ~6 s after "Waiting for application startup" |

`GET /api/v1/health` → `{"status":"ok"}`, `GET /api/v1/version` →
`{"version":"0.2.0"}`.

**Model install, over the API** (as 043 did — the app's own install-button
path was not clicked in this run, so what is verified is that the app
correctly reports and separates against weights installed however they got
there, not the install button's own UI path): `POST
/api/v1/models/standard-stems-001/install` against the clean clone. The
84,141,911-byte weights downloaded and were reported `installed` within a
few seconds.

**A 20-second synthetic stereo file**, generated with FFmpeg into the
scratch directory (never committed, per `AGENTS.md`/`DEVELOPMENT.md`): a
110 Hz sine, a 440 Hz sine put through a tremolo, and pink noise
high-passed at 2 kHz, mixed to one 44.1 kHz/16-bit stereo WAV.

**The real workflow, driven by Playwright against the real, already-running
server** (not the repository's own `e2e` tier, which always drives the fake
separator — read `frontend/playwright.config.ts`'s own comment on that; a
one-off config pointed a real Chromium at `http://127.0.0.1:8000` instead of
letting Playwright start its own fake-separator backend). One test, one real
Standard Stems separation, exercising the acceptance criteria of 048 and
050–054 in sequence:

1. `GET /api/v1/version` confirmed `0.2.0` before anything else.
2. Uploaded the fixture through the file picker; the Select step's summary
   showed `0:20` among the rest of the metadata.
3. Chose Standard Stems (the catalog's four-stem mode, read from `GET
   /separation-modes` rather than hardcoded) and started the job.
4. **Polled the real job to completion** — not the fake separator's timer.
   The backend's own job list afterwards showed **`processing_seconds`
   8.484, `realtime_factor` 2.357** for 20.0 s of audio on CPU (a second,
   otherwise-identical run in the same clean clone measured 9.812 s /
   2.038×; both are consistent with 038's and 043's CPU figures for this
   model on this class of hardware — real inference, not a stub).
5. **Exercised the 048 retry path for real**, not merely observed it: a
   Playwright route handler aborted the very first `GET
   /jobs/{id}/result` request client-side (`route.abort('failed')`) the
   instant "View results" was clicked, then let every subsequent request
   through. The Inspect step showed `.stem-player-error` with the "Try
   again" button; clicking it issued a second `GET .../result`, which the
   backend answered `200` (visible once, correctly, in the server log — the
   aborted request never reached the server at all, confirming the abort
   was client-side and the retry mechanism, not a lucky race, is what
   recovered it).
6. **Waveform lanes**: after recovery, four `.stem-timeline-lane` elements
   rendered, each containing a `<canvas>` — one per catalog stem, from the
   real decoded audio.
7. **Click and keyboard seek**: clicking the timeline a quarter of the way
   along a 20 s file moved the readout to `0:05`; a subsequent `ArrowRight`
   on the focused seek control moved it to `0:06`.
8. **Zoom**: the strip's `data-zoom` was `1` (fit) before any interaction;
   two "Zoom in" clicks raised it; "Zoom out" lowered it again; "Zoom to
   fit" returned it to `1`.
9. **Loop / A-B region**: a drag across the ruler from 10% to 40% of its
   width produced a `Loop m:ss – m:ss` badge and enabled "Clear loop";
   clicking "Clear loop" removed the badge and disabled the button again.
10. **Per-stem fader**: the `vocals` lane's fader (`role="slider"`, name
    `"vocals level"`) was found, set to `0.5`, and read back `0.5`.
11. **Audible scrub preview** (052) was driven exactly as the repository's
    own e2e stage does: pressed Play, dragged the seek surface from 20% to
    60% of the strip through four intermediate pointer moves, and confirmed
    the transport was still in the `Pause`-labelled (i.e. playing) state
    once released — the automatable half of 052's acceptance criteria (one
    real transport move per gesture, audible while dragging). **What is not
    automatable, and is recorded here plainly rather than skipped past: the
    grains themselves are audio, and confirming they are audible — as
    opposed to confirming the transport state and playhead position around
    them — is a listening check. It was not performed by ear in this run
    and remains for the project owner before tagging.**
12. **No unexpected console errors.** The only console line the run
    produced was Chromium's own "Failed to load resource: net::ERR_FAILED"
    for the request this same test deliberately aborted in step 5 — expected
    and excluded from the assertion by name, not by broadening it to accept
    anything.
13. `GET /jobs/{id}/export?format=flac&stems=vocals,drums,bass,other`,
    called directly (a browser-initiated download is not observable from
    here, the same limitation 043 recorded), returned a 200 and a
    5,401,508-byte zip.

**Total spec wall-clock: 12.5 s** for the whole sequence above (upload,
real separation, retry exercise, and every timeline interaction), on the
second of two runs — the first run's failure was in the verification script
itself (see below), not the application.

**No warnings or errors in the server log** across either run (both
attached in full to this feature's working notes; reproduced findings in
summary above). Every request the browser made succeeded exactly once,
including the deliberately-aborted one, which the server never saw at all.

### What the clean run actually turned up

**Nothing new.** No application defect. One thing about the verification
script itself, corrected before the passing run: the first attempt asserted
zero console errors unconditionally and failed on Chromium's own log line
for the request the test itself aborts to exercise the retry path (step 5
above) — a false positive in the check, not a finding about the app. The
assertion was narrowed to exclude exactly that expected message by name,
and the second, otherwise-identical run passed clean. Recorded here rather
than silently fixed, per the same "report what happened" rule 043 used.

Two smaller notes, neither a defect:

- **A background-process bookkeeping mistake during this verification**, not
  a repository issue: an earlier attempt to background the clean-clone
  server with `&` in the same shell call that changed directories lost track
  of its own PID due to how this agent's shell tooling resets working
  directory between calls, and a second start attempt then failed to bind
  port 8000 (already held by the first, still-running instance). The first
  instance was in fact healthy and serving `0.2.0` correctly throughout —
  confirmed by `GET /api/v1/version` and by its own access log — so
  verification proceeded against it once identified; the stray second
  attempt never bound anything and left nothing running. No code or
  documentation issue.
- **First real Standard Stems separation of this session downloaded weights
  in a few seconds** (small model, fast connection) rather than reusing a
  cached install — consistent with "nothing was carried over" above.

## Acceptance criteria

- [x] `CHANGELOG.md` has a user-facing `[0.2.0]` entry, written from the
      048–054 feature docs
- [x] `backend/pyproject.toml` at `0.2.0`; `uv.lock` regenerated to match;
      `GET /api/v1/version` reports `0.2.0`; 029's drift test (3 tests)
      passes
- [x] Contract-change check performed: `openapi.json` regenerated
      (`info.version: 0.2.0`), `api.d.ts` regenerated and diffed — empty,
      because the generated TS never embeds a version string; nothing
      committed for it, and that is stated rather than assumed
- [x] `ROADMAP.md`'s M4 table converted to done-states with feature/PR
      numbers, a closing statement on what remains for the owner, the
      Current state section updated, own ledger row set to `PR OPEN`
- [x] The workflow verified from a clean clone, including the M4 surface —
      waveform lanes, click/drag/keyboard seek, zoom, loop region + badge,
      a fader, and the retry path — with timings and outcomes reported
      above, not assumed
- [x] Known limitations stated where a user will meet them, including what
      the new surface ships with (state that does not survive leaving
      Inspect, the lane-header sizing limit, the auto-follow double
      page-flip, retry's narrower-than-it-sounds scope)
- [x] All five frontend gates green; backend's full quality bar green (the
      version bump touches backend, so its full suite was run per
      `AGENTS.md`, not skipped as "just a version string")

## Gate results

**Backend** (`cd backend`, all against `--extra torch`, this worktree):

| check | result |
| --- | --- |
| `ruff format --check .` | 110 files already formatted |
| `ruff check .` | all checks passed |
| `pyright` | 0 errors, 0 warnings, 0 informations |
| `pytest` | **935 passed**, 11 deselected (the opt-in `integration` tier), 192.18 s |

**Frontend** (`cd frontend`, this worktree):

| check | result |
| --- | --- |
| `format:check` | all matched files use Prettier code style |
| `lint` | clean |
| `typecheck` | clean |
| `test` | **1004 passed**, 41 test files, 49.30 s |
| `build` | `tsc -b && vite build`, 284.42 kB JS / 26.76 kB CSS, built in 441 ms |

## Notes / decisions

1. **The clean-clone verification used a purpose-built Playwright script,
   not the repository's own `e2e` tier.** `frontend/playwright.config.ts`
   documents, load-bearingly, that the tier always drives the fake
   separator — its `webServer` block sets
   `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1` precisely so it never needs
   real weights or a real model. That is the right design for the tier
   (fast, no download, no GPU) and the wrong tool for this feature's job
   (prove the *real* release workflow on the *real* Standard Stems model
   from a clean checkout, exactly as 043 did for v0.1.0). A one-off config
   pointed a real Chromium at the already-running clean-clone server
   instead of asking Playwright to start its own backend, and the spec file
   reused `e2e/app.ts`'s `Workflow` page object and helpers (`fourStemMode`,
   `fetchJob`, `dragRuler`, `dragSeek`, `window()`) rather than
   reinventing selectors — the same vocabulary the shipped tier uses, aimed
   at a different server. Neither file is part of this feature's delivered
   scope; both lived only in the scratch clone and were discarded with it.
2. **The retry path (048) was exercised as a real network failure, not
   merely read about.** A Playwright `page.route` handler aborted the
   client's *first* request to `GET /jobs/{id}/result` and let every
   subsequent one through — the same shape of failure 048's own test suite
   proves against with a stubbed `fetch`, reproduced here against a real
   browser talking to a real server. The server's access log confirms the
   aborted request never arrived (only one `GET .../result` appears per
   job), which is what proves the recovery came from the "Try again" button
   rather than from a request that happened to succeed anyway.
3. **Both jobs' measured real-time factors (2.36× and 2.04×) read faster
   than 043's 1.69× and 038's 1.63–1.64×** for the same model on 20–30 s
   clips. Nothing in 048–055 touches the inference path, so this is read as
   host variance (a quieter machine on this run) rather than a regression
   worth investigating — consistent with 043's own note that its 1.69× was
   "the same number on a different clip, on a build nothing in this branch
   touched." Recorded rather than chased, for the same reason.
4. **`frontend/package.json` stays at `0.0.0`.** 043's reasoning is
   unchanged by anything in 048–054: the frontend still has no independent
   release identity, still ships as the same bundle the backend serves from
   the same commit, and the single version a user can observe is still
   `GET /api/v1/version`. Nothing in this feature's scope changed that
   picture.
5. **The `045` ledger-row note 043 left stands as its own precedent, not
   repeated here.** This feature touched only its own row (055) in the
   ledger table, per `AGENTS.md`'s rule against rewriting other rows, and
   the M4 table/Current-state prose per its explicit scope.

## Known limitations

Everything the timeline ships with, from 048–054's own Known Limitations
sections, carried into `CHANGELOG.md`'s `[0.2.0]` "What it cannot do" for
users — repeated here for the ledger's own record, at the same rigor 043
used for v0.1.0:

- **Playhead, loop region and zoom state do not survive leaving the Inspect
  step.** Leaving `inspect` and returning rebuilds the audio engine at
  0:00 with the stems re-fetched and no region set (050 note 10, carried
  through 052 note 12 and 053 note 12, unaddressed by any of 048–055).
- **Lane headers are tight above the default root font, measured rather
  than estimated.** ~0.9 px of slack at 16 px; 1–7 px of clipping at
  17–20 px root fonts, with the fader remaining clickable throughout
  (`elementFromPoint`-verified) and its own pointer target about 11 px tall
  — under WCAG 2.2 SC 2.5.8's 24 px guideline, keyboard operation
  unaffected (054, Known Limitations). The real fix is a taller or
  font-relative `LANE_HEIGHT_PX`, owned by whichever future feature revisits
  `TimelineLane.tsx` — 051 did not absorb it and it was not this feature's
  to fix.
- **Auto-follow can page-flip twice per loop pass** when the visible window
  is zoomed narrower than the loop region (053 note 13) — working as
  051's follow design intends, applied to a case that reads as busy on
  screen; no test pins the interaction and no fix was proposed here.
- **Retry (048) covers the result fetch only.** A failed *stem audio*
  download after the result has already loaded still renders the same
  unhelpful text with no working control (048's own Known Limitations).
  This feature's clean-clone run exercised the result-fetch path exactly
  because it is the one that has a fix; the stem-download path was not
  exercised because there is nothing yet to exercise.
- **The audible scrub preview (052) was not verified by ear.** The
  clean-clone run above confirms the mechanism automatically — one real
  transport move per gesture, the playing state maintained through the
  drag, the playhead landing in range — but whether the grains are actually
  audible, and audible correctly (respecting mute/solo/level, no click at
  the edges), is a listening check. It remains for the project owner
  before tagging `v0.2.0`, exactly as this document's Scope section says.
- **The pre-existing v0.1.0 caveats not touched by 048–054 still stand**:
  `vocals` mode has no fast tier (~0.3× real time on CPU); Demucs loses the
  `bass` stem on wide-separation stereo mixes, with mono fold-down as the
  verified workaround; the Demucs weights are research-use-only; job
  records live in memory and do not survive a restart; nothing prunes
  uploads, job outputs or exports; a 24-bit/32-bit-float export adds no
  detail; model downloads are not resumable; exports are still buffered in
  the browser tab with no progress indicator and cannot be cancelled.
- **One clean run**, on Windows 11, with a synthetic file and the small
  Standard Stems model — the same scope limitation 043 recorded for
  v0.1.0's verification, for the same reasons (a reasonable download, a CPU
  path faster than real time, and no committed audio fixtures). Not tested
  on Linux or macOS by hand; CI covers Ubuntu on every PR.
- **`CHANGELOG.md`'s `[0.2.0]` link points at a tag that does not exist
  yet.** It resolves the moment the project owner pushes `v0.2.0` — the
  same situation 043 recorded and the same step this feature does not take.

## What remains — the project owner's, per `CONTRIBUTING.md`

Quoting [CONTRIBUTING.md](../../CONTRIBUTING.md#release-process) rather than
inventing a process:

1. "When `dev` reaches a release milestone, prepare the release (version
   bumps, changelog, docs) — directly on `dev` via a numbered feature, or on
   a `release/vX.Y.Z` branch cut from `dev` if stabilization needs
   isolation." — this feature is that preparation, done directly on a
   numbered branch off `dev` (`055-release-preparation-v0.2.0`), matching
   how 043 did it for v0.1.0.
2. "Open a release PR `dev` (or `release/vX.Y.Z`) → `main`." — not opened by
   this feature.
3. "**Rebase-merge it.** Not a merge commit, and not a squash" — the
   project owner's action, once the PR above exists.
4. "Create an **annotated** tag on `main`'s new tip and push it":
   ```sh
   git switch main && git pull
   git tag -a v0.2.0 -m "Straticate v0.2.0"
   git push origin v0.2.0
   ```
5. Pushing the tag runs `.github/workflows/release.yml` (046/047), which
   publishes the GitHub Release once it confirms the tag is annotated, the
   tagged commit is reachable from `main`, and the tag, `pyproject.toml` and
   the `## [0.2.0]` heading in `CHANGELOG.md` all agree — which they now do,
   verified in this feature (`0.2.0` in all three places).
6. "Any release-branch fixes are reconciled back into `dev`."
