# [031] Post-029 review findings (stem Range, startup logging, test integrity)

Branch: `031-post-029-findings`
Status: PLANNED
Dependencies: 029

## Objective

Resolve three defects introduced or left behind by feature 029, found by a
review pass on the merged squash commit (`735591c`) rather than on the PR.
Numbers are never reused and 029 is merged, so its follow-up gets its own.

## Scope

### 1. `streams_a_body` skips the TOCTOU pre-open for `Range` requests (medium)

`backend/src/straticate/api/results.py`. `streams_a_body()` returns `False`
whenever the server advertises the `http.response.pathsend` extension —
**regardless of the request's `Range` header**. But Starlette only uses
pathsend on the full-file path: `FileResponse._handle_simple` takes the
`send_pathsend` flag, while `_handle_single_range` and
`_handle_multiple_ranges` call `anyio.open_file(self.path)` unconditionally
(verified in the pinned starlette 1.6.0, `responses.py`).

So on any server offering the extension, a ranged `GET /jobs/{id}/stems/{stem}`
whose file vanishes in the TOCTOU window skips the pre-open, emits the `206`
status line, and then dies with `FileNotFoundError` mid-body. Because
`started` is already `True`, `StemFileResponse` re-raises — producing exactly
the `500`-instead-of-`stem_file_missing` that 029 item 8 exists to prevent.
029's claim that "Range requests keep the full 404 guarantee" is therefore
false; its test passes only because `httpx2.ASGITransport` sets no extensions.

Compounding it, the docstring's justification — "the `pathsend` arm is the one
that fires in practice (uvicorn offers the extension on some transports)" — is
**wrong for the pinned uvicorn 0.52.4**, which contains no pathsend support at
all (`grep -r pathsend` over the installed package returns nothing). The
narrowing therefore buys nothing on this project's own server while opening the
hole for anything else.

**Fix:** also return `True` when the request carries a `Range` header. Correct
the docstring's factual claim about uvicorn. Add a test that drives a ranged
request through a scope advertising `http.response.pathsend` — the case the
existing test cannot reach.

**Provenance note:** this narrowing was requested by the orchestrator as
finding 5 of the PR #29 review, on the reasoning that a pre-open per range
request wastes a dispatch into the scarce shared executor. That reasoning still
holds for `HEAD`; it was wrong for `Range`.

### 2. Import-time `create_app()` logs before logging is configured (low)

`backend/src/straticate/main.py`. 029 moved `configure_logging` out of
`create_app`, but module-level `app = create_app()` still runs at **import**,
i.e. before `serve()`'s `configure_logging` and before the lifespan's
`ensure_logging_configured`. `create_app` calls `device_detector.refresh()`,
which emits `logger.debug("PyTorch is not installed; …")` and
`logger.warning("Could not determine total system memory; …", exc_info=True)`
(`system/devices.py`).

On both documented entry paths those records now reach `logging.lastResort`:
the debug line is dropped even under `STRATICATE_LOG_LEVEL=DEBUG`, and the
warning prints bare — no timestamp, level or logger name. That is a regression
on precisely the class of records startup diagnostics most need, and it will
matter more once feature 026 makes "is torch present, is CUDA visible" a
question users actually ask.

### 3. `ErrorEnvelopeMiddleware` makes `raise_app_exceptions=True` inert (low)

`backend/src/straticate/errors.py`. The middleware catches `Exception` and
deliberately does not re-raise — correct for the CORS goal of 029 item 4, but
it also means an unexpected route exception no longer propagates to the test
client. `tests/conftest.py`'s `client` fixture uses
`httpx2.ASGITransport(app=app)` and `test_api_ws.py` / `test_api_jobs.py` use
`TestClient(app)`, all with the default `raise_app_exceptions=True`.

A route that starts raising `AttributeError` therefore no longer fails with a
traceback: it silently returns a `500` envelope, so any assertion that does not
check `status_code` still passes. This weakens the whole suite's ability to
catch route bugs — the failure mode is quiet, which is the worst kind.

**Fix:** re-raise after sending (Starlette's own `ServerErrorMiddleware`
contract does exactly this), or make the fixtures assert loudly. Prefer the
former; if the latter, document the trade-off where the fixtures are defined.

## Out of scope

Anything not listed above. In particular the `httpx2` adoption, the middleware
ordering, the stem-name schema constraint and the bounded FFmpeg timeouts were
all explicitly verified as correct by the same review and must not change.

## Acceptance criteria

- [ ] A ranged request under a pathsend-advertising scope yields
      `stem_file_missing` (404), never a `206` followed by a crash
- [ ] The uvicorn/pathsend claim in the docstring is factually correct
- [ ] Startup device-probe records are formatted and level-filtered on both
      documented entry paths
- [ ] An unexpected route exception fails a test loudly again
- [ ] All quality gates green, suite still warning-free

## Required tests

A ranged request through a scope carrying `http.response.pathsend`; a startup
log-capture test on both entry paths; a test proving an unexpected route
exception surfaces to the test client rather than being swallowed.
