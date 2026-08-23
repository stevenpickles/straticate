# Developing Straticate

## Prerequisites

- **Python 3.12** (managed via `uv` — you do not need it preinstalled;
  `uv` will fetch it)
- **[uv](https://docs.astral.sh/uv/)** (Python package/environment manager)
- **Node.js ≥ 20** and npm
- **FFmpeg** (with `ffprobe`) on `PATH`
- Git
- Optional: NVIDIA GPU + CUDA drivers (never required for development or
  normal tests — the fake separator covers everything)

## Backend setup

```bash
cd backend
uv sync                # creates .venv with Python 3.12 and all deps
```

Run the backend (dev mode, auto-reload):

```bash
cd backend
uv run uvicorn straticate.main:app --reload --port 8000
```

Quality checks (all must pass before a PR):

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Auto-format: `uv run ruff format .`

## Frontend setup

```bash
cd frontend
npm ci
```

Run the frontend dev server (proxies `/api` to the backend on port 8000):

```bash
cd frontend
npm run dev            # http://localhost:5173
```

Quality checks (all must pass before a PR):

```bash
cd frontend
npm run format:check
npm run lint
npm run typecheck
npm test               # Vitest (run mode, not watch)
npm run build          # production build must succeed
```

Auto-format: `npm run format`

## Running both

Two terminals: backend on `:8000`, frontend on `:5173`. The Vite dev server
proxies `/api` (REST and WebSocket) to the backend, so the browser only talks
to `:5173`.

## API types

Frontend types are generated from the backend's OpenAPI document — never
hand-written. After changing backend schemas:

```bash
cd backend && uv run python -m straticate.scripts.export_openapi   # writes backend/openapi.json
cd frontend && npm run generate:api                                # openapi-typescript
```

`backend/openapi.json` is gitignored (regenerate on demand). The generated
`frontend/src/api/generated/api.d.ts` **is committed** so frontend CI and
frontend-only development never need the backend — regenerate and commit it in
the same PR whenever backend schemas change. App code imports contract types
from `frontend/src/api/types.ts` (friendly aliases), never from the generated
file directly.

## Test strategy

| Tier | Where | Runs in CI | Requires |
| --- | --- | --- | --- |
| Backend unit | `backend/tests/` (pytest, pytest-asyncio) | always | nothing external |
| Backend API | pytest + httpx `ASGITransport` against the app | always | nothing external |
| Audio tests | pytest, tiny generated fixtures in `testdata/` | always | FFmpeg |
| Frontend unit/component | `frontend/` (Vitest + Testing Library) | always | nothing external |
| E2E (fake separator) | Playwright, added around M1 | always | FFmpeg |
| GPU/model integration | separate suite, manually triggered | on demand | CUDA GPU, model downloads |

Principles:

- Normal CI never needs CUDA, a GPU, or model downloads — jobs run against the
  fake separator.
- Audio fixtures are generated (sine sweeps, noise bursts) and seconds long;
  never commit copyrighted or large audio.
- WebSocket flows are tested with an in-process client against the real event
  hub; frontend tests consume recorded/mocked typed events.
- Every job-state transition and cancellation path has a test.

## CI plan

GitHub Actions, workflow `ci.yml`, triggered on PRs and pushes to `dev`/`main`.
Backend and frontend jobs run in parallel; each is skipped-proof (no path
filtering initially — both always run, keeping required checks simple).

Backend job (Ubuntu):

```text
uv sync → ruff format --check → ruff check → pyright → pytest
```

Frontend job (Ubuntu):

```text
npm ci → format:check → lint → typecheck → vitest run → vite build
```

Both jobs install FFmpeg via apt. A later `integration-gpu` workflow (manual
`workflow_dispatch`, self-hosted or skipped by default) covers real-model
validation. CI must not download ML models.

## Conventions

- Line endings are LF in the repository (`.gitattributes` enforces this);
  editors on Windows should respect `.editorconfig`.
- Python: Ruff is the formatter and linter; Pyright runs in strict mode for
  `src/straticate`.
- TypeScript: strict mode; ESLint + Prettier.
- Public interfaces (API routes, schemas, exported functions/components) carry
  docstrings/TSDoc.
