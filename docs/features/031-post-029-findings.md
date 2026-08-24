# [031] Post-029 review findings (stem Range, startup logging, test integrity)

Branch: `031-post-029-findings`
Status: PR OPEN
Dependencies: 029
PR: #TBD

## Objective

Three defects that a review pass on the **merged** 029 squash commit
(`735591c`) found, fixed. Numbers are never reused and 029 is merged, so its
follow-up got its own.

## What was done

Three items, each its own commit, in the order below.

### 1. A `Range` request keeps the stem TOCTOU pre-open

`backend/src/straticate/api/results.py`. `streams_a_body()` returned `False`
whenever the scope advertised the `http.response.pathsend` extension —
regardless of the request's `Range` header. Starlette uses pathsend on the
full-file arm **only**: `FileResponse._handle_simple` is the single method that
takes the `send_pathsend` flag, while `_handle_single_range` and
`_handle_multiple_ranges` call `anyio.open_file(self.path)` unconditionally
(confirmed in the pinned starlette 1.6.0, `responses.py`).

So on a server offering the extension, a ranged `GET /jobs/{id}/stems/{stem}`
whose file vanished in the TOCTOU window skipped the pre-open, emitted the
`206` status line, and then died with `FileNotFoundError` mid-body — and with
`started` already `True`, `StemFileResponse` can only re-raise. That is exactly
the `500`-instead-of-`stem_file_missing` that 029 item 8 exists to prevent.

**`Range` now wins over `pathsend`**: a request carrying the header always
pre-opens. The check reads the raw ASGI header list (`has_range_header`) rather
than constructing `starlette.datastructures.Headers`, which requires `headers`
in the scope and rewrites it in place. The `HEAD` shortcut is unchanged and
still comes first — a `HEAD` reads nothing on any arm.

The docstring's factual claim was corrected too. It justified the narrowing
with "the `pathsend` arm is the one that fires in practice (uvicorn offers the
extension on some transports)"; the pinned **uvicorn 0.52.4 contains no
pathsend support at all** (`grep -rn pathsend` over the installed package
returns nothing, while the same grep over starlette returns the six sites that
implement it). Neither shortcut fires on this project's own server, and the
docstring now says so — both are honoured because they are `FileResponse`'s
contract rather than ours.

**Why the existing test could not catch it:** `httpx2.ASGITransport` builds a
scope with **no `extensions` key**, so the pathsend branch is never taken
through it and `test_a_range_request_still_gets_the_404_guarantee` pre-opened
for the wrong reason. The raw-ASGI helper in `tests/test_api_results.py` (which
already existed, for unnormalized URL paths) now takes `headers` and
`extensions`, and two tests use it: a ranged request under a
pathsend-advertising scope, asserting the `stem_file_missing` envelope; and a
whole-file request under the same scope, asserting `http.response.start` +
`http.response.pathsend` and no body — so the narrowing is still pinned where
it is correct, and "always pre-open" would not pass silently. Both fail against
the old code, the first with the `FileNotFoundError`-after-`206` escaping the
application.

**Provenance note:** the narrowing was requested by the orchestrator as finding
5 of the PR #29 review, on the reasoning that a pre-open per range request
wastes a dispatch into the scarce shared executor. That reasoning still holds
for `HEAD` and for the full-file pathsend case; it was wrong for `Range`.

### 2. Compute devices are probed at startup, not at import

`backend/src/straticate/main.py`. 029 moved `configure_logging` out of
`create_app`, but the module-level `app = create_app()` still runs at
**import** — before `serve()`'s `configure_logging` and before the lifespan's
`ensure_logging_configured` — and `create_app` called `device_detector
.refresh()`. Its records therefore reached `logging.lastResort`: the
`logger.debug("PyTorch is not installed; …")` line dropped even under
`STRATICATE_LOG_LEVEL=DEBUG`, and `logger.warning("Could not determine total
system memory; …", exc_info=True)` printed bare, with no timestamp, level or
logger name.

**How this was fixed without reintroducing what 029 removed:** by moving the
*probe*, not the *logging configuration*. `create_app` still constructs the
`DeviceDetector` — so `app.state.device_detector` stays substitutable before
startup, which every API test relies on — and the lifespan warms it,
immediately after `ensure_logging_configured`. `create_app` therefore gains no
global logging call at all: `logging.basicConfig(force=True)` stays confined to
`serve()`, importing the module still reconfigures nothing for a host
application, and pytest's `caplog` handler is still safe (the 029 tests that
guard both are untouched and still pass).

The alternative — calling `ensure_logging_configured` from `create_app` — was
rejected: `basicConfig` without `force` is non-destructive but it is still a
process-global act on import, and 029's own test asserts that building an
application configures nothing. Deferring detection is also simply more
correct: nothing needs the device list before the application is running, and
the lifespan is the one point that runs once per *running* application on both
entry paths. Devices still cannot change during a run, and a failing probe
still only logs a warning, so startup semantics are unchanged.

`tests/test_main.py` covers **both documented entry paths** —
`uvicorn straticate.main:app` and `python -m straticate` (i.e. `serve()`) —
against real captured stderr. Each reproduces the uvicorn situation with the
new `bare_root_logger()` context manager (a root logger nobody has configured;
a context manager rather than a fixture because pytest attaches its own root
handler for the *call* phase, after fixture setup), patches
`main.DeviceDetector` **before** `create_app` runs, and asserts each record
arrives project-formatted: `<timestamp> DEBUG    straticate.system.devices -
…`, the WARNING with its `exc_info` traceback, and nothing at all written while
the application is merely being built. The probe logs at DEBUG and then
raises, so the WARNING comes from `DeviceDetector._probe_safely` itself rather
than from the test. That "building the app writes no record" assertion is the
one that fails against the old code.

This matters more than its severity suggests: feature 026 lands torch, which
makes "is PyTorch installed, is CUDA visible" the most-asked startup question —
and it was exactly the record being dropped.

### 3. `ErrorEnvelopeMiddleware` re-raises, so `raise_app_exceptions` works again

`backend/src/straticate/errors.py`. The middleware caught `Exception` and did
not re-raise — correct for the CORS goal of 029 item 4, but it made
`raise_app_exceptions=True` inert for every client in the suite:
`tests/conftest.py`'s `client` fixture (`httpx2.ASGITransport`) and the
`TestClient` in `test_api_ws.py` / `test_api_jobs.py` all use the default. A
route that started raising `AttributeError` silently returned a 500 envelope,
and any assertion not checking `status_code` still passed.

**Re-raising worked**, and is what was done — it is Starlette's own
`ServerErrorMiddleware` contract ("we always continue to raise the exception …
allows test clients to optionally raise the error within the test case"). The
CORS fix is untouched: the envelope has already been sent through
`CORSMiddleware`, so `ServerErrorMiddleware`'s `response_started` flag is set
and it emits no second response. Verified directly rather than assumed — a test
drives the raw ASGI application and asserts **exactly one**
`http.response.start`, carrying both the envelope body and
`access-control-allow-origin`, with the original `RuntimeError` still escaping.

One consequence needed handling: `ServerErrorMiddleware` invokes the registered
`Exception` handler even when the response has started, so logging from the
middleware as well would report every unexpected exception twice. The
middleware now builds its response with the new, deliberately silent
`internal_error_response()`, and `_handle_unexpected_error` — which every
re-raised exception still reaches — remains the single place the traceback is
logged. (At the process edge a real server logs it as well, exactly as it does
for any unhandled ASGI exception; that is standard, and was the behaviour
before 029.)

Three regression tests: the exception surfacing through `httpx2.ASGITransport`,
the same through Starlette's `TestClient`, and the single-response/CORS check
above. All three fail against the old code. The `client` fixture's docstring
now records that its `raise_app_exceptions` default is load-bearing and why a
test that wants to inspect a 500 body opts out explicitly.

## Out of scope (unchanged)

The `httpx2` adoption, the middleware **ordering**, the stem-name schema
constraint, the bounded FFmpeg timeouts and the `AudioStore` path split were
all explicitly verified correct by the same review and were not touched. No
schema changed, so `frontend/src/api/generated/api.d.ts` cannot have drifted
and was not regenerated. No frontend file was touched.

## Acceptance criteria

- [x] A ranged request under a pathsend-advertising scope yields
      `stem_file_missing` (404), never a `206` followed by a crash
- [x] The uvicorn/pathsend claim in the docstring is factually correct
- [x] Startup device-probe records are level-filtered and project-formatted on
      both documented entry paths, without `create_app` regaining a destructive
      global logging call
- [x] An unexpected route exception fails a test loudly again, and a
      cross-origin 500 still carries both its envelope and its CORS header
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest` all
      green (604 tests), and the suite still passes under `-W error`

## Tests

`backend/tests/test_api_results.py`

- `test_a_range_request_keeps_the_404_guarantee_under_pathsend`
- `test_a_whole_file_request_under_pathsend_serves_the_path`
- `test_only_body_carrying_requests_pre_open_the_stem` (extended: `Range` beats
  `pathsend`; `HEAD` still reads nothing)

`backend/tests/test_main.py`

- `test_the_uvicorn_entry_path_formats_startup_device_records`
- `test_the_serve_entry_path_formats_startup_device_records`

`backend/tests/test_errors.py`

- `test_an_unexpected_route_exception_reaches_the_client`
- `test_an_unexpected_route_exception_reaches_the_test_client`
- `test_a_re_raised_500_still_sends_exactly_one_response`

Every one of them was verified to fail against the pre-fix code by reverting
the fix, running it, and restoring.

## Known limitations

- The pathsend tests construct the scope by hand because no server in this
  project implements the extension; they pin Starlette's contract, not a
  deployment we run today.
- A `Range` request whose stem file survives the pre-open and disappears
  microseconds later, mid-body, is still an aborted connection rather than a
  `404`. That window is irreducible — the status line is already on the wire —
  and is documented as such on `StemFileResponse`.
