# [029] Skeleton hardening (deferred review findings)

Branch: `029-skeleton-hardening`
Status: PLANNED
Dependencies: 004, 005

## Objective

Resolve code-review findings that were out of scope for the feature that
surfaced them. The PR #5 items were confirmed by an adversarial review pass on
2026-08-23; the PR #17 items on 2026-08-24.

## Scope

- **500 responses lack CORS headers.** The catch-all `Exception` handler runs
  in Starlette's outermost `ServerErrorMiddleware`, outside `CORSMiddleware`,
  so cross-origin callers cannot read the `internal_error` envelope. Restructure
  (e.g. envelope-producing middleware inside the CORS layer) so the error
  contract survives 500s.
- **Wire `Settings` for real.** `host`, `port`, and `data_dir` are currently
  unread; the config docstring advertises `STRATICATE_PORT` as a working
  override. Add a serve entry point that consumes host/port (or fix the docs),
  move the CORS origin allowlist into `Settings`, and have `data_dir` consumed
  once feature 006 lands.
- **Single-source the version.** `straticate/__init__.py` hand-duplicates
  `pyproject.toml`'s version; use hatchling dynamic versioning (or
  `importlib.metadata`) so `/api/v1/version`, package metadata, and the
  frontend badge cannot drift. Add a test that would catch drift.
- **Import-time side effects.** Module-level `app = create_app()` plus
  `logging.basicConfig(force=True)` inside the factory reset global logging on
  every import/instantiation (clobbering test `caplog`). Move logging setup to
  the server entry point and/or adopt `uvicorn --factory`.
- **CI FFmpeg placement.** The backend job installs FFmpeg though no backend
  test uses it yet, and the frontend job (which will run the FFmpeg-dependent
  E2E tier per DEVELOPMENT.md) doesn't. Align installs with actual consumers
  when 006/007 land; fix DEVELOPMENT.md's claim that both jobs install it.

### Deferred from PR #17 (feature 015, job REST endpoints)

- **An invalid catalog becomes a 500 at job-create time instead of failing at
  startup.** `separator_info_from_model` feeds `Model.stems` into
  `SeparatorInfo`, which enforces `^[a-z][a-z0-9_]*$` and uniqueness.
  `schemas.models.Model` enforces neither (only `min_length=2`), and
  `ModelCatalog` checks only cross-model stem *agreement*. A catalog entry with
  `"stems": ["Vocals", "Instrumental"]` (or a duplicate stem) therefore loads
  cleanly, serves `GET /models` and `GET /separation-modes` fine, and then
  raises an unhandled `ValueError` on the first `POST /api/v1/jobs` for that
  mode — a `500` whose real cause appears only in the server log. This
  contradicts feature 010's own stated principle that a malformed catalog fails
  loudly at startup rather than degrading. Fix by validating the stem pattern
  and uniqueness on `Model` (or in `ModelCatalog`), with a test asserting the
  failure happens at load time. Not fixed in 015 because
  `backend/src/straticate/schemas/` is a shared contract that feature was
  explicitly barred from touching.
- **`resolve_audio` is not pure — it creates a directory as a side effect.**
  `jobs/resolution.py` documents its three resolvers as pure, but
  `AudioStore.original_path` does `directory.mkdir(parents=True,
  exist_ok=True)`. On the documented "registered record whose file has
  disappeared" path, `resolve_audio` therefore recreates an empty
  `{data_dir}/audio/{audio_id}/` and *then* returns 404, leaving orphan
  directories behind on what is supposed to be a read-only lookup. Fix by
  giving `AudioStore` a non-creating path accessor and mkdir-ing only on the
  write path (feature 006's module, hence deferred).

### Deferred from PR #17 to feature 026 (not this feature)

Recorded here so they are not lost; they must be resolved **as part of 026**,
where they stop being theoretical:

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

### Deferred from PR #20 (feature 021, result + stem serving)

- **`Stem.name` is unconstrained, so `/result` can advertise a stem
  `/stems/{name}` denies.** `schemas.models`-side: `Stem.name`
  (`backend/src/straticate/schemas/jobs.py`) is a plain `str`, while
  `SeparatorInfo` validates its stem names against
  `^[a-z][a-z0-9_]*$`. A separator returning `"Vocals"` or `"drums-2"` would
  have it listed in the result, pass the membership check, then fail
  `stem_path()` and produce a self-contradictory 404 whose `detail` lists the
  very stem it says does not exist. **This is the same root cause as the PR #17
  finding above** — fixing stem-name validation once at the schema boundary
  resolves both.
- **TOCTOU between `path.is_file()` and `FileResponse`'s own `os.stat`.**
  Starlette re-stats the file and raises `RuntimeError` on `FileNotFoundError`,
  surfacing as a 500 where the module promises `stem_file_missing` (404). The
  window is real precisely because the module documents that a job directory
  "can be removed underneath a live job". Pass the single `os.stat` result
  through as `stat_result=`, and/or catch `OSError`/`RuntimeError` around the
  send.
- **CORS exposes no custom response headers.** `CORSMiddleware` is configured
  with explicit origins but no `expose_headers`, so `Content-Range`,
  `Accept-Ranges` and `ETag` — which the REST contract now documents as part of
  the stem response — are invisible to cross-origin JavaScript. Inert today
  because `frontend/vite.config.ts` proxies `/api` (the browser sees
  same-origin) and `<audio src>` handles ranges below the JS layer, but feature
  023's Web Audio path would hit it the moment it talked to `:8000` directly.
  Note this alongside the existing CORS item above.

## Out of scope

New endpoints or schema changes.

## Acceptance criteria

- [ ] Each item above fixed or explicitly re-dispositioned in this document
- [ ] All quality gates green

## Required tests

500-envelope CORS test; version-drift test; logging-isolation test (caplog
survives `create_app()`).
