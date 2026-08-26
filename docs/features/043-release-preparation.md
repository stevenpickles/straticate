# [043] Release preparation for v0.1.0

Branch: `043-release-preparation`
Status: PR OPEN
Dependencies: 038, 042
PR: #62

## Objective

Everything that must be true before the `dev → main` release PR for **v0.1.0**.

Deliberately **not** a CI workflow. The project owner chose the smallest thing
that makes a real release, and `CONTRIBUTING.md` already documents the manual
`dev → main` → annotated-tag flow. Release automation would be new CI surface to
maintain for a first release; it can come later if the manual flow proves
annoying.

## Scope

- **`CHANGELOG.md`**, which the release process in `CONTRIBUTING.md` already
  assumes exists and which does not. Write the 0.1.0 entry from the feature
  ledger — unusually good source material, since every feature has a numbered
  doc with acceptance criteria. Write it for **users**, not as a commit log:
  what the application can now do, what it needs, and what it cannot do.
- **Version bump.** `backend/pyproject.toml` `0.1.0.dev0` → `0.1.0`. Feature 029
  single-sourced `__version__` from package metadata, so that is one edit and
  `/api/v1/version` follows automatically — **verify** that rather than assuming
  it, and note that 029 added a drift test which must still pass.
  `frontend/package.json` is `0.0.0`; decide whether the frontend carries the
  same version, and say why.
- **Honest limitations, in the release notes.** This project has recorded them
  carefully throughout; the release is where a user meets them. At minimum: the
  `vocals` mode has no fast tier (~0.3× real time on CPU); Demucs loses bass on
  wide-separation stereo mixes, with mono fold-down as the verified workaround
  (028's known limitations, feature 041); model weights carry their own licences
  and one shipped model is **research-use-only**; job records are in memory and
  do not survive a restart; nothing prunes job outputs or export artifacts.
- **Verify the release works from a clean checkout.** Clone or `git clean` into
  a fresh directory, follow the README exactly as written, and separate
  something. Report what you did, not what should have happened. This is the
  step that catches documentation which only works on the machine it was written
  on.
- Update the ROADMAP: M3's table, and "Current state" to describe a released
  project rather than one in flight.

## Out of scope

- **Creating the release PR, merging to `main`, or tagging.** Those belong to the
  project owner (`AGENTS.md`: never push to `main`). Prepare everything and hand
  over a `dev` that is ready.
- A GitHub Actions release workflow — explicitly deferred.
- Publishing to PyPI or npm; building distributable artifacts.
- Any feature work. If the clean-checkout run finds a bug, **report it** — do not
  fix it inside release preparation.

## Acceptance criteria

- [x] `CHANGELOG.md` exists with a user-facing 0.1.0 entry
- [x] Versions bumped; `/api/v1/version` reports `0.1.0`; 029's drift test passes
- [x] Known limitations stated where a user will meet them
- [x] A clean checkout was followed end to end and what happened is reported
- [x] ROADMAP reflects a project at release rather than in flight
- [x] All gates green

## The version bump

One hand edit: `backend/pyproject.toml`, `0.1.0.dev0` → `0.1.0`. `uv` then
rewrote the `straticate` entry in `backend/uv.lock` to match on the next sync —
that is a generated file, and committing it is what keeps CI's `uv sync` from
finding the lock out of date, but it is worth knowing the string appears there
too.

**Verified rather than assumed**, and verified from the clean clone rather than
from this worktree:

| what | result |
| --- | --- |
| `GET /api/v1/version` | `{"version":"0.1.0"}` |
| the header in the browser | `backend v0.1.0` |
| `tests/test_version.py` (029's drift test) | 3 passed, inside the 935 |

029's design is what made it one edit. `straticate.__version__` resolves from
installed distribution metadata; `api/system.py` and `main.py` both read it, so
the endpoint, the OpenAPI `info.version` and the wheel metadata cannot disagree
with `pyproject.toml`. `test_version.py` reads the **file** rather than the
metadata, so an edit to one without the other fails — and it also asserts the
value is not `UNKNOWN_VERSION`, which is what stops a stale editable install
from making the comparison pass by accident.

One nicety fell out for free: `docs/contracts/rest-api.md` has documented
`GET /version` → `{ "version": "0.1.0" }` since feature 005. It was aspirational
for forty features. It is now true.

### `frontend/package.json` stays at `0.0.0`, deliberately

The brief asked for a decision and a reason. The reason is that bumping it would
create a second version with nothing to keep it honest.

- **The frontend has no independent release identity.** It is `"private": true`,
  never published to npm, and since 042 it is not even served independently —
  the backend serves the bundle built from the same commit. There is no artifact
  that carries this number anywhere a user can see it.
- **There is exactly one version a user can observe**, and it is the backend's:
  `GET /api/v1/version` and the `backend v0.1.0` indicator in the header. The
  repository's release identity is the annotated tag on `main`, which covers
  both directories at once.
- **029 spent a whole feature removing the second source of truth on the Python
  side and added a test to keep it removed.** Putting `0.1.0` in
  `package.json` reintroduces exactly that shape on the other side of the
  boundary, with no equivalent test: nothing in `npm run lint`, `typecheck`,
  `test` or `build` compares the two, so the first release that bumps only one
  of them drifts silently. A `0.0.0` that is obviously inert is better than a
  `0.1.0` that is *sometimes* right.

What would change the decision: publishing the frontend as a package, or serving
it from somewhere other than this backend. Either would give it a version
someone can read, and then it needs one — with a drift test, not without.

## The clean-checkout verification

The point of this step is to catch documentation that only works on the machine
it was written on, so the rule was: type what `README.md` says, in a directory
that has never built this project, and report what happened rather than what
should have.

**Setup.** `git clone` of this branch into an empty scratch directory on
Windows 11, with `uv 0.8.23`, Node v24.11.1 / npm 11.6.2, FFmpeg 9.0.1 and
Git 2.55.0 already on `PATH`. Nothing was carried over: no `.venv`, no
`node_modules`, no `frontend/dist`, no `backend/data`, no installed weights.

**Following `README.md` verbatim, both commands worked first time.**

| step | wall clock |
| --- | --- |
| `cd frontend && npm ci` | 8.9 s |
| `npm run build` | 10.5 s (643 ms of it `vite build`) |
| `cd backend && uv sync --extra torch` | 23.5 s |
| `uv run python -m straticate` | serving on `127.0.0.1:8000` |

The server logged that it found the bundle, and `127.0.0.1:8000` served the
application. `GET /api/v1/health` → `{"status":"ok"}`,
`GET /api/v1/version` → `{"version":"0.1.0"}`.

**Then it separated something, in a browser**, which is the part the README
actually describes. A 20-second stereo 44.1 kHz MP3, generated with FFmpeg
(a 110 Hz bass tone, a tremolo'd 440 Hz lead and a high-passed pink-noise
percussion bed — synthetic, because the repository commits no audio):

1. Selected the file through the Select step's file input (the picker path, not
   a real drag-and-drop — browser automation cannot synthesise a drop). Upload
   succeeded and the workspace showed
   `0:20 · MP3 · Stereo · 44.1 kHz · 192 kbps · 470.2 KB`.
2. The Configure step offered both modes from the catalog. Vocal Isolation
   showed "Needs a 870.8 MB download", its licence panel (code MIT, weights
   MIT, commercial use Permitted), an install button reading "870.8 MB will be
   written to the machine running Straticate. 2 TB is free there.", and a
   **disabled** "Start separation" with the reason "This quality tier needs its
   model weights installed first."
3. Chose Standard Stems, whose 84,141,911-byte weights had been installed a few
   minutes earlier through `POST /api/v1/models/standard-stems-001/install`
   (downloaded and SHA-256 verified in a few seconds; the **install button in
   the UI was not clicked** in this run, so what is verified here is that the
   app reports installed weights correctly — "Model weights installed
   (80.2 MB)." — not the install button's own path). Its licence panel came up
   headed **"Restricted use"**, with the weights terms rendered verbatim,
   "Commercial use: Not permitted", "Redistribution: Not permitted", and a note
   that the terms are stated in words rather than as a named licence. That is
   the research-use-only fact reaching a user at the moment it can still change
   their decision, which is the claim the README makes for it.
4. Started the job. Live WebSocket progress with real chunk counts (`1 / 4`,
   `3 / 4`, …), a Cancel button (present, not exercised — cancelling was not
   part of this run), and a telemetry panel showing the model, `htdemucs`,
   `htdemucs-v4`, the stage and a real-time factor of 1.9×. The Device group was
   absent, which is correct on CPU.
5. Completed, reading "Separation complete — 4 stems are ready." The Inspect
   step listed all four stems with Mute and Solo, and Play started playback
   (the transport switched to Pause; no audio was listened to).
6. Export offered WAV 24-bit / WAV 32-bit float / FLAC, said "You will get a
   .zip with 4 audio files and separation.json", and carried the note that a
   24-bit export is re-encoded from 16-bit stems and adds no detail. The zip
   itself was taken over HTTP rather than through the button, because a
   browser-initiated download is not observable from here.

**Two jobs were run against the clean checkout**, on the same file: one driven
over HTTP with `curl` (so the numbers below could be read out of the result
record exactly) and one driven through the browser as described above. Both
completed with four stems.

**Measured on the HTTP run:** `processing_seconds` **11.844**,
`realtime_factor` **1.6886**, for 20.0 s of audio on CPU; the result record
reported all four stems at 20.0 s, 44,100 Hz, 2 channels. The browser run
reported **1.9×** in its telemetry panel, which is the same figure rounded and
measured from a different clock. Feature 028 published 1.63–1.64 on a 30 s clip
on this host; this is the same number on a different clip, on a build nothing in
this branch touched.

`GET /jobs/{id}/export?format=flac&stems=vocals,drums,bass,other` returned a
2,399,590-byte zip containing `vocals.flac`, `drums.flac`, `bass.flac`,
`other.flac` and a 936-byte `separation.json`.

**No console errors in the browser. No warnings or errors in the server log.**

### What the clean run actually turned up

Three things. **None of them is a code defect**, and none was fixed here.

1. **`README.md`, `ARCHITECTURE.md` and `DEVELOPMENT.md` all describe
   directories that do not exist.** `README.md`'s repository layout lists
   `scripts/    Development and automation scripts` and
   `testdata/   Small audio fixtures for tests`; `ARCHITECTURE.md` §3 has
   `testdata/` in its tree; `DEVELOPMENT.md`'s test-strategy table says audio
   tests use "tiny generated fixtures in `testdata/`". `git ls-files` has
   neither, and neither is gitignored — the fixtures are generated into
   temporary directories at test time and always have been. This is exactly the
   class of thing this step exists to find. Not fixed: it spans three documents
   this feature does not own, and it is now the last bullet of the ROADMAP's
   *After v0.1.0* list.
2. **The README's path gives you a CPU build even on a machine with a GPU**, and
   says so, but it is worth knowing that the clean run bore it out concretely:
   this host has an RTX 4060 Laptop GPU, and `GET /system/devices` from the
   clean checkout returned **`cpu` only**. That is correct and documented
   (`--extra torch` pins PyTorch's CPU wheel; DEVELOPMENT.md's *PyTorch and
   CUDA* has the one command that swaps it), but it means a first-time user with
   a GPU gets the slow path unless they read further — and for Vocal Isolation
   that is the difference between 40 s and 10 minutes on a three-minute track.
   Not a defect; a documentation ordering question for whoever revisits the
   README.
3. **First startup takes about 9 seconds** between "Waiting for application
   startup" and "Application startup complete" — cold device detection importing
   torch. Subsequent starts are quick. Recorded because it is long enough that a
   first-time user may think it has hung, and nothing on the terminal says what
   it is doing.

## Known limitations

- **Nothing here was tested on Linux or macOS.** The clean run is one clone on
  one Windows 11 host. The README's commands are platform-neutral and CI builds
  and tests both toolchains on Ubuntu on every PR, but "followed the README from
  a clean checkout" is a claim about **Windows** in this document, and it should
  not be read as more.
- **One clean run, and it separated with Standard Stems, not Vocal Isolation.**
  The 84 MB model was chosen over the 913 MB one to keep the verification to a
  reasonable download and a CPU path that is faster than real time. So the
  release's *headline* mode was not exercised end to end from the clean
  checkout — its numbers in `CHANGELOG.md` come from 026's and 038's
  measurements, which were made on this host but not in this branch.
- **The audio was synthetic.** A generated tone-plus-noise mix proves the
  pipeline, not the separation quality. Quality figures in the changelog are
  026's and 028's ground-truth correlations, cited as theirs.
- **`CHANGELOG.md`'s `[0.1.0]` link points at a tag that does not exist yet.**
  It resolves the moment the project owner pushes `v0.1.0`, which is the step
  this feature is explicitly forbidden to take.
- **The `045` ledger row still reads `PR OPEN`** although `1b8baec` is in `dev`.
  That is another feature's row and was left untouched (`AGENTS.md`: do not
  rewrite ROADMAP entries other than your own); the pending ledger PR is the
  place for it.
- **No test asserts that `CHANGELOG.md` exists or mentions the current
  version.** It could go stale at the next release with nothing to catch it.
  The equivalent guard on the Python side (029's drift test) is what makes the
  absence of one here noticeable.
