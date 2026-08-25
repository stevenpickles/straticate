# Developing Straticate

## Prerequisites

- **Python 3.12** (managed via `uv` — you do not need it preinstalled;
  `uv` will fetch it)
- **[uv](https://docs.astral.sh/uv/)** (Python package/environment manager)
- **Node.js ≥ 20** and npm
- **FFmpeg** (with `ffprobe`) on `PATH`
- Git
- Optional: NVIDIA GPU + CUDA drivers (never required for development or
  normal tests — the fake separator covers everything, and PyTorch installs as
  its CPU build by default; see *PyTorch and CUDA* below). Since feature 032 the
  fake separator is hidden from a normally started server; see
  *Separating audio without downloading weights* below for the one-variable way
  back.

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

Or run it from its own settings, with no uvicorn flags to keep in sync:

```bash
cd backend
uv run python -m straticate                       # 127.0.0.1:8000
STRATICATE_PORT=9000 uv run python -m straticate  # 127.0.0.1:9000
```

The two differ only in who owns the bind address and the log configuration.
`uvicorn straticate.main:app` takes both from its command line (and is what
`--reload` needs); `python -m straticate` takes `STRATICATE_HOST`,
`STRATICATE_PORT` and `STRATICATE_LOG_LEVEL` from
`backend/src/straticate/config.py`, where every setting is documented.

### Separating audio without downloading weights

A backend started as above serves the **user-facing** catalog: development
fixtures are hidden (feature 032), so the only separation mode is `vocals` at
`high_quality`, backed by `vocals-hq-001` — a 913 MB download you must install
first with `POST /api/v1/models/vocals-hq-001/install`. A job started before
that is refused with `model_weights_missing` (409).

To work on anything *other* than real inference, put the fake separator back:

```bash
cd backend
STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1 uv run uvicorn straticate.main:app --reload --port 8000
```

That restores the pre-032 catalog — `vocals` gains a `balanced` tier and
`standard_stems` reappears, both backed by `FakeSeparator` — so the whole
upload → job → progress → telemetry → stems → export loop runs with no weights,
no CUDA and no network. **This is how the backend test suite and the Playwright
E2E tier run**, which is why neither needs a GPU or a download.

Quality checks (all must pass before a PR):

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

Auto-format: `uv run ruff format .`

## PyTorch and CUDA

`uv sync` installs **the CPU build of PyTorch**, deliberately.
`backend/pyproject.toml` pins `torch` to PyTorch's own CPU wheel index:

```toml
[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cpu" }]
```

The default PyPI `torch` wheel for Linux bundles the CUDA runtime — several
gigabytes, downloaded on every CI run, for a job that has no GPU. The CPU wheel
is a fraction of that and is the right default three times over: for CI, for
development on a CPU-only machine, and for macOS/Windows, where the PyPI wheel
is CPU-only anyway. `explicit = true` means *only* `torch` comes from that index;
everything else still resolves from PyPI.

**To run on an NVIDIA GPU**, swap that one wheel for a CUDA build after syncing.
Nothing else changes — no code, no settings, no API, no schema. Detection
(feature 018) starts reporting CUDA devices and jobs resolve to them
automatically:

```bash
cd backend
uv sync
uv pip install --reinstall-package torch   --index-url https://download.pytorch.org/whl/cu126 torch
```

Substitute the CUDA version your driver supports (`cu121`, `cu124`, `cu126`, …);
`nvidia-smi` reports the maximum.

Two notes on why it is a second command rather than a flag on the first.
Redirecting the *named* index (`uv sync --index pytorch-cpu=…/cu126`) loses its
`explicit = true`, so unrelated packages start resolving from PyTorch's index
and the lock fails; and `uv sync` re-pins `torch` from the lock file, so **a
later `uv sync` puts the CPU build back** and this command has to be repeated.
That is a fair trade for a default that keeps CI lean, and it is one line in a
setup script.

Confirm which build is installed:

```bash
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
curl localhost:8000/api/v1/system/devices
```

### Model weights

Weights are never committed (ARCHITECTURE.md §9) and never downloaded by tests.
Install them through the running application:

```bash
curl -X POST localhost:8000/api/v1/models/vocals-hq-001/install
curl localhost:8000/api/v1/models/vocals-hq-001    # installation.state / .progress
```

The artifact is verified against the SHA-256 pinned in `models/catalog.json`
before it is published into `models/weights/{model_id}/weights.bin`. The
high-quality vocals model is ~913 MB.

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
| Backend API | pytest + httpx2 `ASGITransport` against the app | always | nothing external |
| Audio tests | pytest, tiny generated fixtures in `testdata/` | always | FFmpeg |
| Frontend unit/component | `frontend/` (Vitest + Testing Library) | always | nothing external |
| E2E (fake separator) | Playwright — feature 030, not yet written | *(will be)* always | FFmpeg |
| GPU/model integration | `backend/tests/test_roformer_integration.py` (`-m integration`) | never (opt-in) | installed weights, and a GPU for the `gpu` tests |

Principles:

- Normal CI never needs CUDA, a GPU, or model downloads — jobs run against the
  fake separator, and the real separator's plumbing is tested against a
  synthetic ~20 000-parameter checkpoint built at test time
  (`backend/tests/roformer_fixtures.py`).
- **The real-model tier is opt-in.** `backend/pyproject.toml` sets
  `addopts = "-m 'not integration'"`, so a plain `pytest` deselects it; run it
  with `uv run pytest -m integration` once the weights are installed. Its tests
  skip with an explanatory message when their prerequisites are missing.
- Audio fixtures are generated (sine sweeps, noise bursts) and seconds long;
  never commit copyrighted or large audio.
- WebSocket flows are tested with an in-process client against the real event
  hub; frontend tests consume recorded/mocked typed events.
- Every job-state transition and cancellation path has a test.
- **The backend suite runs clean.** A warning in the output is a finding, not
  background noise: it is either fixed or filtered with a written reason. The
  HTTP client is `httpx2` (Pydantic's successor to `httpx`) because
  `starlette.testclient` deprecates the `httpx` shim; the two have the same
  API, so the swap is an import rename.

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

**The backend job installs FFmpeg via apt; the frontend job does not** — and
that matches who actually uses it. The backend suite runs FFmpeg and ffprobe
for real (generated audio fixtures, upload probing, export transcoding, and
ffprobe verification of what a transcode produced), while the frontend suite is
Vitest against mocked API responses and touches no media tooling. The frontend
job needs FFmpeg only once the Playwright E2E tier exists (feature 030), which
is when the install should be added — not before, where it would be a minute of
CI spent on nothing.

A later `integration-gpu` workflow (manual `workflow_dispatch`, self-hosted or
skipped by default) covers real-model validation: it would install the weights
through the model manager and run `pytest -m integration`. CI must not download
models, and the CPU-wheel pin above is what keeps the backend job's `uv sync`
from growing by gigabytes now that `torch` is a runtime dependency.

## Conventions

- Line endings are LF in the repository (`.gitattributes` enforces this);
  editors on Windows should respect `.editorconfig`.
- Python: Ruff is the formatter and linter; Pyright runs in strict mode for
  `src/straticate`.
- TypeScript: strict mode; ESLint + Prettier.
- Public interfaces (API routes, schemas, exported functions/components) carry
  docstrings/TSDoc.
