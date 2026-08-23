# [029] Skeleton hardening (deferred review findings)

Branch: `029-skeleton-hardening`
Status: PLANNED
Dependencies: 004, 005

## Objective

Resolve the code-review findings from PR #5 that were out of scope for the
contracts feature. Each item below was confirmed by an adversarial review pass
on 2026-08-23.

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

## Out of scope

New endpoints or schema changes.

## Acceptance criteria

- [ ] Each item above fixed or explicitly re-dispositioned in this document
- [ ] All quality gates green

## Required tests

500-envelope CORS test; version-drift test; logging-isolation test (caplog
survives `create_app()`).
