# [029] Skeleton hardening (deferred review findings)

Branch: `029-skeleton-hardening`
Status: PR OPEN
Dependencies: 004, 005
PR: #…

## Objective

Every code-review finding that five earlier PRs (#5, #8, #17, #20, #25)
recorded as "out of scope here" is now fixed, or explicitly re-dispositioned to
the feature that will do it. M1's accumulated debt stops being carried forward
into M2.

## What was done

Eleven items, each its own commit, in the order they appear below.

### A. Configuration and startup

1. **The version has one source.** `straticate/__init__.py` hardcoded
   `0.1.0.dev0`, duplicating `pyproject.toml`. It now resolves at import time
   via `importlib.metadata.version("straticate")`, with a recognisably unreal
   `0.0.0+unknown` fallback for the case where no distribution is installed at
   all. `tests/test_version.py` reads the version out of `pyproject.toml` with
   `tomllib` and asserts it matches `__version__`, so editing one without the
   other fails the suite; a second test rejects the fallback, so the comparison
   cannot pass by accident, and a third asserts `GET /api/v1/version` serves
   the same string.

2. **`Settings` is wired for real.** `serve()` (and `python -m straticate`)
   reads `host`, `port` and `log_level` and hands them to uvicorn, so
   `STRATICATE_PORT=9000 uv run python -m straticate` does what the config
   docstring always promised. The CORS origin allowlist moved out of the
   hardcoded list in `create_app` into `Settings.cors_origins` (a JSON array in
   the environment). `ffmpeg_timeout_seconds` was added here for item 9.
   `data_dir` **was already consumed** — by `AudioStore` (006), the job output
   layout (014) and the export cache (022) — so nothing was needed beyond
   saying so; the `Settings` class docstring now names the consumer of every
   field, because a setting nothing reads is a promise the application does not
   keep. DEVELOPMENT.md documents both ways to start the server and how they
   differ.

3. **Building an application configures nothing process-global.**
   `configure_logging()` calls `logging.basicConfig(force=True)`, which
   replaces the root logger's handlers for the whole interpreter; calling it
   from `create_app` meant every instantiation — one per test using the `app`
   fixture — tore down pytest's `caplog` handler. Only `serve()` configures
   logging now, and it passes `log_config=None` so uvicorn's own dictConfig
   does not replace it either.
   **The module-level `app = create_app()` stays**, deliberately and now
   documented as such: `uvicorn straticate.main:app` is what DEVELOPMENT.md,
   CI and hand-testing use, and with the global side effect gone there is
   nothing left for `--factory` to fix. `tests/test_main.py` proves `caplog`
   still captures after `create_app()`, and that `create_app` never calls
   `configure_logging` while `serve()` does.

### B. The error contract

4. **A 500 carries CORS headers.** The catch-all `Exception` handler runs in
   Starlette's outermost `ServerErrorMiddleware`, outside `CORSMiddleware`, so
   a cross-origin caller got an opaque network failure for the one error it
   most needs to report. `ErrorEnvelopeMiddleware` (a pure ASGI middleware in
   `errors.py`) now produces the `internal_error` envelope one layer lower, and
   is added *before* CORS so that CORS ends up outermost — `add_middleware`
   prepends, so the last added is the outermost layer, and a comment at the
   call site says so. The registered `Exception` handler is kept as a fallback
   for anything raised outside the middleware.
   `tests/test_errors.py` sends an `Origin` at a route that raises and asserts
   **both** the envelope body and the `access-control-allow-origin` response
   header; removing the middleware makes it fail (verified).

5. **The documented response headers are readable cross-origin.**
   `Accept-Ranges`, `Content-Disposition`, `Content-Range`, `ETag` and
   `Last-Modified` are all part of the stem/export contract and none was in
   `Access-Control-Expose-Headers`, so browser JavaScript could receive a byte
   range and not see which range it got. All five are exposed now, asserted on
   a real `206` stem response carrying an `Origin`, and written into
   `docs/contracts/rest-api.md`.

### C. Stem names — one fix, two findings

6. **Stem names are constrained at the schema boundary.** The pattern
   `^[a-z][a-z0-9_]*$` and uniqueness were enforced only by `SeparatorInfo`.
   `schemas.jobs.Stem.name` and each entry of `schemas.models.Model.stems` now
   carry the pattern, and `Model.stems` rejects duplicates. That single fix
   closes both findings: a catalog with `"stems": ["Vocals", …]` (or a repeat)
   can no longer load cleanly and then 500 on the first job, and a result can
   no longer advertise a stem `/stems/{name}` would deny.
   The pattern has exactly one definition, in the new
   `straticate/schemas/stems.py`, imported by `schemas/` and re-exported by
   `inference/base.py` — that module imports nothing from the application, so
   both sides of the seam share it with no circular import.
   `ModelCatalog.from_file` fails at **load time** with `ModelCatalogError`
   naming the file and `models.N.stems`, tested for four bad-name shapes and
   for a duplicate, plus a test that `create_app` itself refuses to start.
   `frontend/src/api/generated/api.d.ts` was regenerated and committed; the
   wire shape is unchanged (descriptions only), so no other frontend file
   moved.

### D. Filesystem robustness

7. **A read-only audio lookup creates nothing.** `AudioStore.original_path`
   did `mkdir(parents=True, exist_ok=True)`, so `resolve_audio` — documented as
   pure — recreated an empty `{data_dir}/audio/{audio_id}/` on the "registered
   record whose file has disappeared" path and *then* returned 404, leaving an
   orphan directory per failed probe. `original_path` is now pure;
   `prepare_original_path` creates the directory and is called only from the
   upload write path. Tested by snapshotting the tree around two failing
   lookups.

8. **The TOCTOU window on stem serving is closed.** `stem_source()` returns the
   `os.stat_result` it used, which is passed to the response as `stat_result=`
   so nothing re-`stat`s. `FileResponse` still sends its headers *before*
   opening the file, so `StemFileResponse` opens it first — while a 404 is
   still choosable — and maps `OSError`/`RuntimeError` onto the documented
   `stem_file_missing`. Tested deterministically by patching the lookup to
   delete the file exactly in the window: no timing, no `sleep`.
   Two cases remain and are documented rather than papered over: the
   microseconds between our open and Starlette's, and a file lost mid-stream,
   when the status line is already on the wire.

### E. Subprocess safety

9. **Every FFmpeg invocation is bounded.** `subprocess.run` had no `timeout` in
   `api/export.py`, `audio/probe.py` **or** `inference/pcm.py`; all three
   dispatch onto asyncio's shared default `ThreadPoolExecutor`, so a wedged
   subprocess is a thread held forever and enough of them starve probing and
   separation, not just exports. There is now one runner —
   `straticate.audio.ffmpeg.run_ffmpeg` — which always passes
   `Settings.ffmpeg_timeout_seconds` (default 600) and raises `FFmpegTimeout`
   on expiry, after `subprocess.run` has killed the process.
   Each call site maps it onto **its own** code, because a timeout is not "not
   decodable" and the three surfaces are not interchangeable:

   | surface | code | status |
   | --- | --- | --- |
   | `POST /audio` (ffprobe) | `audio_probe_timed_out` | 504 |
   | a job's decode (FFmpeg) | `audio_decode_timed_out` | job `error.code` |
   | `GET /jobs/{id}/export` (FFmpeg) | `export_timed_out` | 504 |

   All three are documented in `docs/contracts/rest-api.md` (including a
   "Timeouts" section explaining the shared-executor reason). Every timeout
   path is tested with a stubbed runner — never by waiting.

### F. Tooling

10. **The CI FFmpeg claim was wrong; the workflow was right.** Re-checked
    before changing anything: nine backend test modules use FFmpeg or ffprobe
    for real (generated fixtures, upload probing, export transcoding, and
    ffprobe verification of transcoded output), so the backend job's install is
    correct. The frontend suite is Vitest against mocked responses and needs
    nothing until the Playwright tier exists (feature 030). So `ci.yml` is
    **unchanged** and DEVELOPMENT.md's "Both jobs install FFmpeg via apt" was
    corrected to say which job installs it, why, and when the frontend job
    should gain it.

11. **`httpx2` adopted; the suite has no warnings.** `httpx2` is real,
    published (Pydantic's successor to `httpx`, 2.12.0) and API-compatible with
    everything the suite uses — `AsyncClient`, `ASGITransport`,
    `raise_app_exceptions` — so the deprecation was fixed rather than filtered:
    `httpx` was replaced outright in the dev dependencies and the test imports
    renamed, keeping one HTTP client rather than two. The backend suite now
    runs with **zero** warnings and no `filterwarnings` entry, and
    DEVELOPMENT.md records that a warning in the output is a finding, not
    background noise.

## Re-dispositioned, not fixed

- **The Playwright E2E tier → feature 030.** This is a whole new test tier, not
  a deferred fix, so it is tracked separately as feature **030 — Playwright E2E
  tier (fake separator)** (ledger row added in this PR, depends on 024).
  DEVELOPMENT.md's test-strategy table and its CI section both now point at
  030 instead of "around M1".

## Still deferred to feature 026 (unchanged)

Recorded here so they are not lost; they must be resolved **as part of 026**,
where they stop being theoretical. Neither was touched by this feature.

- **Separator construction runs on the event loop inside the request handler.**
  `SeparatorRegistry.get()` builds the separator on a cache miss, inside the
  `async def create_job` handler. That is free for `FakeSeparator`, but a real
  backend loads weights there — blocking the event loop, and with it the job
  worker, the event dispatcher, every other HTTP request and all WebSocket
  delivery. 026 must offload it (`asyncio.to_thread`) or construct the
  separator inside the executor.
- **`Model.capabilities` is never consulted when resolving a device.** A
  CUDA-only catalogued model on a CPU-only host is accepted with `201` and only
  dies later as a generic `separation_failed` event, instead of being rejected
  at create time. Nothing reads `Model.capabilities` anywhere in the codebase
  yet; today both fake models declare `cuda` and `cpu`, so the gap is
  unreachable. 026 introduces the first model for which it is not.

## Out of scope

New endpoints and new response shapes. (Item 6 adds *validation* to existing
fields — a constraint, not a new shape. Item 9 adds three error **codes**,
which the item explicitly required and which are documented in the REST
contract.) No behaviour change to the job manager, event hub, separators,
telemetry sampler, or the model catalog beyond item 6's load-time validation.
No frontend source other than the regenerated `api/generated/api.d.ts`. No
Playwright work (030). No PyTorch, real models or downloads (025/026).

## Modules changed

- `backend/src/straticate/`: `__init__.py`, `__main__.py` (new), `main.py`,
  `config.py`, `errors.py`, `logging.py`, `api/{audio,export,job_outputs,results}.py`,
  `audio/{__init__,ffmpeg (new),probe,storage}.py`, `inference/{base,fake,pcm}.py`,
  `jobs/resolution.py`, `schemas/{__init__,jobs,models,stems (new)}.py`
- `backend/pyproject.toml`, `backend/uv.lock`
- `backend/tests/`: `test_version.py`, `test_main.py`, `test_ffmpeg_runner.py`
  (all new) plus `conftest.py`, `test_api_export.py`, `test_api_jobs.py`,
  `test_api_results.py`, `test_audio.py`, `test_errors.py`,
  `test_inference_fake.py`, `test_jobs_resolution.py`, `test_model_catalog.py`,
  `test_models_api.py`, `test_schemas.py`, `test_system.py`,
  `test_telemetry_sampler.py`
- `frontend/src/api/generated/api.d.ts` (regenerated)
- `DEVELOPMENT.md`, `docs/contracts/rest-api.md`, `ROADMAP.md`
- `.github/workflows/ci.yml` — **deliberately unchanged** (item 10)

## Acceptance criteria

- [x] Every item 1–11 fixed; the Playwright bullet re-dispositioned to 030 with
      the ledger row added
- [x] A 500 carries CORS headers, proven by asserting the
      `access-control-allow-origin` header, not just the body
- [x] An invalid catalog (bad stem name, or a duplicate) fails at **startup**
      with `ModelCatalogError`
- [x] A read-only audio lookup creates no directories
- [x] A stem file deleted between check and send yields 404
      `stem_file_missing`, never a 500
- [x] All three FFmpeg call sites have a bounded timeout mapping to a
      documented per-surface error code
- [x] `__version__` and `pyproject.toml` cannot drift without a test failing
- [x] `caplog` works after `create_app()`
- [x] The backend suite runs with no warnings
- [x] `frontend/src/api/generated/api.d.ts` regenerated and committed
- [x] All quality gates green

## Required tests

Added alongside each item: the version-drift test; `caplog`-after-`create_app`;
`serve()` binding host/port from the environment; the 500-with-`Origin` CORS
test and the same for a handled `ApplicationError`; `expose_headers` on a real
`206`; invalid-catalog-at-startup (bad pattern **and** duplicate, plus
`create_app` refusing to start); `Stem.name` rejecting bad names and `Model`
rejecting duplicates; a read-only lookup creating no directory and
`original_path`/`prepare_original_path` differing; the deterministic TOCTOU
404; and a timeout path for each of the three FFmpeg call sites plus the runner
itself. Conventions unchanged: `ASGITransport`/`TestClient`,
`asyncio.Event`-gated coordination, generated audio fixtures, and **no
`sleep()` as synchronization** — every timeout test stubs the runner rather
than waiting.

## Notes / decisions

- **`python -m straticate` passes the application object, not the
  `"straticate.main:app"` import string.** The string form makes uvicorn
  re-import the module in the worker, building a second application (a second
  catalog load, a second device probe) and discarding the first. Reload-style
  supervision is therefore not offered by `serve()`; that is `uvicorn --reload`
  on the command line, which DEVELOPMENT.md documents.
- **Middleware order is load-bearing.** `add_middleware` prepends, so the last
  one added is the outermost layer. `ErrorEnvelopeMiddleware` is added first
  and `CORSMiddleware` second, precisely so the envelope travels back out
  through CORS. Swapping the two lines silently restores the bug; the
  `Origin`-header test is what catches it.
- **Three timeout codes, not one.** A single generic code would have leaked
  across three unrelated API surfaces, and reusing `audio_not_decodable` would
  have told users their file was broken on the strength of a subprocess that
  never reached a verdict.
- **`export_timed_out` is a code, not an `export_failed` reason.**
  `export_failed` means the encode was attempted and failed — a statement about
  the audio or the disk. A timeout is a statement about the server, with a
  different remedy.
