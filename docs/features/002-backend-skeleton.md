# [002] Backend skeleton

Branch: `002-backend-skeleton`
Status: PR OPEN
Dependencies: 001
PR: #2

## Objective

A runnable FastAPI backend skeleton with configuration, logging, health/version
endpoints, a consistent error envelope, and the full quality toolchain — the
foundation every later backend feature builds on.

## Scope

- `backend/` project: `pyproject.toml` (hatchling src layout), committed
  `uv.lock`, `.python-version` (3.12), scoped `.gitignore`
- `src/straticate/`: `main.py` (`create_app()` factory + module-level `app`),
  `config.py` (pydantic-settings, `STRATICATE_` env prefix), `logging.py`,
  `errors.py` (`ApplicationError` + exception handlers), `api/system.py`
- Endpoints: `GET /api/v1/health`, `GET /api/v1/version`
- Error envelope for all failures:
  `{"error": {"code", "message", "detail"}}` — covers `ApplicationError`,
  `HTTPException` (404 → `not_found`), 422 → `validation_error`, and unhandled
  exceptions → 500 `internal_error` (traceback logged, never in the body)
- CORS for `http://localhost:5173`
- Tooling config: Ruff (py312, line 100, E/F/W/I/UP/B/SIM/RUF), Pyright strict,
  pytest with `asyncio_mode = "auto"`

## Out of scope

PyTorch/ML (deliberately not a dependency yet), audio handling, jobs,
WebSockets, model catalog, CI workflows.

## Acceptance criteria

- [x] `uv sync` succeeds; `uv run uvicorn straticate.main:app` serves both endpoints
- [x] Error envelope verified for 404/422/500/`ApplicationError`
- [x] `ruff format --check`, `ruff check`, `pyright` (strict, 0 errors), `pytest` (6 passed) all green
- [x] Public functions/classes documented

## Required tests

`backend/tests/` — httpx `AsyncClient` over `ASGITransport` against
`create_app()`: health body, version matches `__version__`, error envelope
shapes and codes.

## Notes / decisions

- PyTorch is intentionally deferred to the first feature that needs it
  (device detection / real inference) to keep the skeleton light.
