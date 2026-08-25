# Developing Straticate

## Prerequisites

- **Python 3.12** (managed via `uv` — you do not need it preinstalled;
  `uv` will fetch it)
- **[uv](https://docs.astral.sh/uv/)** (Python package/environment manager)
- **Node.js ≥ 20** and npm
- **FFmpeg** (with `ffprobe`) on `PATH`
- Git
- For the E2E tier: Playwright's Chromium, installed with
  `cd frontend && npm run e2e:browsers` (it lives in a user cache, never in
  the repository)
- Optional: NVIDIA GPU + CUDA drivers (never required for development or
  normal tests — the fake separator covers everything, PyTorch is an optional
  extra since feature 034, and when installed it is the CPU build; see *PyTorch
  and CUDA* below). Since feature 032 the
  fake separator is hidden from a normally started server; see
  *Separating audio without downloading weights* below for the one-variable way
  back.

## Backend setup

```bash
cd backend
uv sync                # .venv with Python 3.12 and every default dependency
uv sync --extra torch  # …plus PyTorch, for work on the real separator
```

`torch` is an **optional** extra (feature 034): the application imports, starts
and serves fake-separator jobs without it, which is what keeps the E2E job and
every torch-free checkout from pulling ~183 MiB they never execute. Everything
below assumes the plain `uv sync` unless it says otherwise; *PyTorch and CUDA*
covers what the extra changes, including two ways `uv run` can undo it.

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

Those four commands are right for the default environment. If you are working on
the **real** separator, `uv run` needs care in two different ways — see
*What `uv run` does to a PyTorch environment* below; on a machine with the CUDA
wheel installed the short answer is to add `--no-sync` to every one of them.

## PyTorch and CUDA

**`torch` is an optional extra (feature 034).** A plain `uv sync` does not
install it, and the application starts and runs fake-separator jobs without it —
which is what keeps CI and the E2E tier from downloading hundreds of megabytes
they never use. To work on real separation, ask for it:

```bash
cd backend
uv sync --extra torch
```

When it *is* installed, it is installed as **the CPU build**, deliberately.
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
uv sync --extra torch
uv pip install --reinstall-package torch --index-url https://download.pytorch.org/whl/cu130 torch
```

Two notes on why it is a second command rather than a flag on the first.
Redirecting the *named* index (`uv sync --index pytorch-cpu=…/cu130`) loses its
`explicit = true`, so unrelated packages start resolving from PyTorch's index
and the lock fails; and `uv sync` re-pins `torch` from the lock file, so **a
later `uv sync` puts the CPU build back** and this command has to be repeated.
That is a fair trade for a default that keeps CI lean, and it is one line in a
setup script. Read the rest of this section before running anything else: that
"later `uv sync`" is closer than it looks, and the obvious way to check the
result is the thing that undoes it.

### Choosing the `cuNNN` index

`nvidia-smi` reports the highest CUDA version the driver supports; take the
highest PyTorch index at or below it. **But a `cuNNN` index is a directory of
files, not a translation layer** — it carries a wheel for a given torch version
and platform, or it does not, and one that does not is reported as `uv` simply
being unable to find `torch`. The fix for that is a different `cuNNN`, never a
different flag. For the `torch 2.13.0` this project pins, checked on 2026-08-25
with `curl -s https://download.pytorch.org/whl/cuNNN/torch/`:

| index | `torch 2.13.0` |
| --- | --- |
| `cu130` | Linux **and Windows** (`cp312-cp312-win_amd64` present) |
| `cu129` | Linux only — no Windows wheel at any Python version |
| `cu126` | Linux **and Windows** (`cp312-cp312-win_amd64` present) |
| `cu128` | none — that index stops at `torch 2.11.0` |
| `cu124` | none — stops at `2.6.0` |
| `cu121` | none — stops at `2.5.1` |

So on Windows there are exactly two choices for this torch: `cu130` (current)
and `cu126` (fallback for an older driver). `cu121` and `cu124`, which this
section used to offer as examples, do not exist for `torch 2.13.0` on any
platform, and `cu128` does not either. Re-check the table above when the pinned
torch version moves — the answer is version-specific, and only the index knows
it.

### What `uv run` does to a PyTorch environment

**`uv run` syncs the environment before it runs anything**, to whichever extras
*that invocation* names — not to the ones you synced with earlier. Nothing
remembers `--extra torch`, and there is no `UV_EXTRA` variable and no
`[tool.uv] default-extras` to make it stick. Combined with `torch` being both
optional (034) and pinned to the CPU index, that gives `uv run` two separate
ways to quietly undo the environment you just built. Measured with uv 0.8.23,
starting each row from a `.venv` holding `torch 2.13.0+cu130`:

| command | what happens to `torch` |
| --- | --- |
| `uv sync` | **removed entirely** — it is not a default dependency |
| `uv sync --extra torch` | reinstalled as the **CPU** wheel |
| `uv run pytest` | left alone — `uv run` corrects *required* packages but does not prune extraneous ones, and without the extra `torch` is not required |
| `uv run --extra torch pytest` | reinstalled as the **CPU** wheel |
| `uv run --no-sync pytest` | left alone |
| `.venv/Scripts/python.exe -m pytest` | left alone |

Two things in that table surprise people, and they pull in opposite directions:

1. **On a CPU host**, `uv run pytest` after `uv sync --extra torch` is fine
   today only because the sync left `torch` behind — but `uv sync` (say, after a
   dependency change) takes it away again, and then the real-separator tests
   have nothing to import. The habit that always works is
   `uv run --extra torch <ruff|pyright|pytest|…>`.
2. **On a CUDA host, `--extra torch` is the flag that breaks it.** Adding it to
   `uv run` is exactly what re-pins `torch` to the locked CPU wheel. The habit
   that always works there is `--no-sync`.

So: **`--extra torch` when you need torch installed, `--no-sync` when you need
the CUDA build to survive.** Do not rely on `uv run`'s not-pruning: it is a
property of `uv run` rather than of your project, and `uv sync` prunes for real
— it removes the CUDA wheel *and* anything else installed into `.venv` by hand,
such as the optional NVML binding below:

```text
$ uv pip list | grep -i "nvidia\|^torch "
nvidia-ml-py           13.610.43
torch                  2.13.0+cu130
$ uv sync
Uninstalled 2 packages in 2.72s
Installed 1 package in 21.73s
 - nvidia-ml-py==13.610.43
 - torch==2.13.0+cu130
 + torch==2.13.0+cpu
```

A reversion announces itself only as two lines that are very easy to read
straight past:

```text
Uninstalled 1 package in 6.35s
Installed 1 package in 17.48s
```

There are two ways to run something without re-syncing at all. Add `--no-sync`:

```bash
cd backend
uv run --no-sync pytest -m integration
uv run --no-sync uvicorn straticate.main:app --port 8000
uv run --no-sync python -m straticate.scripts.export_openapi
```

…or call the interpreter inside `.venv` directly, which cannot re-sync:

```bash
cd backend
.venv/Scripts/python.exe -c "…"   # Windows
.venv/bin/python -c "…"           # Linux and macOS
```

`uv pip install`, `uv pip uninstall` and `uv pip list` do **not** re-sync, which
is why the swap command above is safe to run.

### Confirming which build is installed

```bash
cd backend
.venv/Scripts/python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# 2.13.0+cu130 True
```

**Do not verify with `uv run --extra torch`.** It reinstalls the CPU wheel
first and then reports, perfectly truthfully, on the environment it has just
changed — the check destroys what it is checking and presents its own damage as
the original state. Observed verbatim in this repository:

```text
$ uv run --extra torch python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
Uninstalled 2 packages in 2.00s
Installed 2 packages in 18.13s
2.13.0+cpu False
```

Dropping the `--extra torch` happens to be harmless *today* — `uv run` leaves
the already-installed CUDA wheel alone — but that is a property of `uv run`, not
a guarantee about your environment, and it does not hold for `uv sync`. Verify
with the interpreter, which cannot be wrong about itself.

The end-to-end check is the application's own device list — and the server has
to be started the same careful way. Started as `uv run --extra torch uvicorn …`
it reverts the wheel on the way up and then correctly reports a host with no
CUDA device:

```bash
cd backend
uv run --no-sync uvicorn straticate.main:app --port 8000 &
curl localhost:8000/api/v1/system/devices
# [{"id":"cuda:0","backend":"cuda","name":"NVIDIA GeForce RTX 4060 Laptop GPU",…},
#  {"id":"cpu","backend":"cpu",…}]
```

CUDA first in that list is the whole point: feature 018's detector found the
device, and feature 026's resolver will send a job that pinned no device to it.

`pytest` matters for the same reason and fails more quietly. On a CPU host you
need `--extra torch` or the real-separator tests have nothing to import — but on
a **GPU** host that same flag is what breaks the run, and it breaks it silently:

```text
$ uv run --extra torch pytest -m integration -q -rs
Uninstalled 1 package in 1.96s
Installed 1 package in 17.62s
SKIPPED [1] tests\test_roformer_integration.py:193: no CUDA device is available
3 passed, 1 skipped, 725 deselected in 56.21s
```

That is a green run, on a machine with a working GPU, in which
`test_cuda_runtime_stats_report_real_memory` skipped itself, everything else ran
on the CPU, and every timing figure printed is a CPU figure. Immediately before
it, the same tier under `uv run --no-sync pytest -m integration` was **4 passed
on `cuda:0`**. Use `--no-sync` here.

### Optional: NVML, for GPU utilization and temperature

GPU telemetry works without NVML: memory allocated, peak and total come from
torch's own CUDA APIs, and `utilization` / `temperature_celsius` are simply
`null` (ARCHITECTURE.md §12 — basic operation must never require NVML, and
nothing here makes it a dependency). If you want those two fields filled in
locally:

```bash
cd backend
uv pip install nvidia-ml-py        # NOT pynvml — see below
```

**Install `nvidia-ml-py`, never `pynvml`.** They are not alternatives with the
same result. `nvidia-ml-py` is NVIDIA's binding and provides the module named
`pynvml`, which is what `inference/torch_device.py` imports and what torch
imports. The PyPI package *called* `pynvml` is a deprecated shim: it installs
`nvidia-ml-py` plus a `_pynvml_redirector` import hook that raises a
`FutureWarning` the first time anything imports `pynvml`.

That would be a nuisance anywhere. Here it breaks the test suite, because
`torch/cuda/__init__.py` imports `pynvml` **at torch import time** and the suite
treats a warning as a finding:

```text
$ uv pip install pynvml
$ python -W error -c "import torch"
  File "…/torch/__init__.py", line 2189, in <module>
    _C._initExtension(_manager_path())
  File "…/torch/cuda/__init__.py", line 64, in <module>
    import pynvml
  File "…/_pynvml_redirector.py", line 29, in find_spec
    warnings.warn(PYNVML_MSG, FutureWarning, stacklevel=2)
FutureWarning: The pynvml package is deprecated. Please install nvidia-ml-py
instead. If you did not install pynvml directly, please report this to the
maintainers of the package that installed pynvml for you.
```

Every test that imports torch fails, at import, with a message about a package
you installed for telemetry. If you have already done it,
`uv pip uninstall pynvml` removes the shim and leaves `nvidia-ml-py` — which it
pulled in as a dependency — in place and working.

Verified on 2026-08-25 with `nvidia-ml-py 13.610.43`, driver 610.47: the suite
is clean under `-W error`, and a real separation reported `utilization: 1.0` and
`temperature_celsius: 62.0`.

**Do not add it to `backend/pyproject.toml`.** NVML stays optional and outside
the lock file, which also means the next `uv sync` prunes it (see the table
above) and you reinstall it the same way.

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

CI additionally runs the end-to-end suite (`npm run e2e`) on every PR; see
*End-to-end tests* below for running it locally.

## Running both (development)

Two terminals: backend on `:8000`, frontend on `:5173`. The Vite dev server
proxies `/api` (REST and WebSocket) to the backend, so the browser only talks
to `:5173`.

The proxy target is `STRATICATE_BACKEND_URL` when that variable is set
(defaulting to `http://localhost:8000`), which is how the E2E tier points the
dev server at the backend it started for itself.

This is the arrangement to develop in — hot module reload, source maps, an
un-minified bundle — and it is unchanged by the production path below.

## Running it as one process (production build)

Since feature 042 the backend serves the built frontend itself, so *using*
Straticate is one command on one port. Build the bundle, then start the backend
as usual:

```bash
cd frontend && npm run build     # writes frontend/dist/
cd ../backend && uv run python -m straticate
```

`http://127.0.0.1:8000` is then the whole application: `/api/v1/**` is the API
exactly as before, the WebSocket is exactly where it was, and every other path
is the app (a deep link or a refresh returns `index.html`, which is what makes
client-side routes survive a reload). `uv run uvicorn straticate.main:app
--port 8000` serves it identically — the two entry points differ only in who
owns the bind address, as above.

Both commands go through `uv run`, which **re-syncs the environment to the
extras that invocation names** — so on a machine carrying a CUDA build of
PyTorch, run them as `uv run --no-sync …` like every other command here (see
*What `uv run` does to a PyTorch environment*). Nothing about serving the
frontend depends on torch either way.

Three things worth knowing:

- **The bundle is located once, when the application is built**, so building the
  frontend while the server is running means restarting the server.
- **The bundle path is a setting**, `STRATICATE_FRONTEND_DIST_DIR`, defaulting to
  this repository's `frontend/dist` resolved from the *package's* location — not
  from the working directory. `python -m straticate` serves the same app from
  `backend/`, from the repository root, or from `C:\`.
- **A checkout with no `frontend/dist` is a normal state.** The API starts and
  behaves exactly as it always has (which is how the backend suite and the
  Playwright tier run); only the root URL differs — it explains how to build the
  bundle instead of returning a 404.

The end-to-end tier below still drives the *development* arrangement, on its own
ports, and is unaffected by whether `frontend/dist` exists.

## End-to-end tests (Playwright)

The E2E tier drives the whole workflow — upload, configure, separate, progress,
telemetry, cancel, inspect, playback, export, reconnect — in a real browser
against the **fake separator**. It needs no GPU, no weights and no download.

```bash
cd frontend
npm ci
npm run e2e:browsers   # once: downloads Chromium (~110 MB, outside the repo)
npm run e2e            # the suite
npm run e2e:ui         # the same suite in Playwright's UI mode
```

`npm run e2e` starts **both servers itself** — you do not need either running,
and a backend you already have on `:8000` is left alone:

- the backend on `127.0.0.1:8123` with `STRATICATE_DATA_DIR` pointing into a
  temporary directory, so uploads, job outputs and the export cache never land
  in `backend/data/`;
- the Vite dev server on `127.0.0.1:5123`, told where that backend is.

Both ports, and the temporary directory, are overridable —
`STRATICATE_E2E_BACKEND_PORT`, `STRATICATE_E2E_FRONTEND_PORT`,
`STRATICATE_E2E_DIR` — and everything the run creates is deleted when it ends.
The audio fixtures are generated with FFmpeg at setup time; **audio is never
committed**.

Useful invocations: `npx playwright test e2e/cancel.spec.ts` for one file,
`--headed` to watch it, `--debug` to step through it, and
`npx playwright show-trace test-results/…/trace.zip` for the trace a failure
leaves behind. The whole suite is about **35 seconds** on a developer machine
(~7 s of that is starting the two servers).

`npm test` (Vitest) and `npm run e2e` (Playwright) do not overlap: Vitest is
scoped to `src/`, Playwright to `e2e/`, each with its own config.

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
| E2E (fake separator) | `frontend/e2e/` (Playwright, Chromium) | always | FFmpeg, Chromium, both servers |
| GPU/model integration | `backend/tests/test_roformer_integration.py` (`-m integration`) | never (opt-in) | installed weights, and a GPU for the `gpu` tests |

Principles:

- Normal CI never needs CUDA, a GPU, or model downloads — jobs run against the
  fake separator, and the real separator's plumbing is tested against a
  synthetic ~20 000-parameter checkpoint built at test time
  (`backend/tests/roformer_fixtures.py`).
- **The real-model tier is opt-in.** `backend/pyproject.toml` sets
  `addopts = "-m 'not integration'"`, so a plain `pytest` deselects it; run it
  with `uv run --extra torch pytest -m integration` once the weights are
  installed — or, **on a GPU host, `uv run --no-sync pytest -m integration`**,
  because there `--extra torch` re-pins `torch` to the CPU wheel and the `gpu`
  test then skips itself on a machine that has a GPU. Both traps are in
  *PyTorch and CUDA*. Its tests skip with an explanatory message when their
  prerequisites are missing.
- Audio fixtures are generated (sine sweeps, noise bursts) and seconds long;
  never commit copyrighted or large audio. The E2E tier generates its own with
  FFmpeg at setup time, into a temporary directory it deletes afterwards.
- **The E2E tier waits on conditions, never on the clock.** It contains no
  fixed sleeps: it waits for elements, for polling `expect`s, for responses
  that have actually arrived and for rendered frames. A sleep added there is a
  bug report waiting to happen.
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
The backend, frontend and e2e jobs run in parallel; each is skipped-proof (no
path filtering — all three always run, keeping required checks simple).

Backend job (Ubuntu):

```text
uv sync --extra torch → ruff format --check → ruff check → pyright → pytest
```

Every step there carries `--extra torch` (feature 034), because `uv run`
re-syncs to the extras *it* is given and omitting the flag on one step would
uninstall torch for the next. This is the job that runs the real separator's
unit tests and type-checks the module that imports torch, so it is the one place
the extra is genuinely exercised.

Frontend job (Ubuntu):

```text
npm ci → format:check → lint → typecheck → vitest run → vite build
```

E2E job (Ubuntu), added by feature 030:

```text
apt ffmpeg → uv sync (backend) → npm ci (frontend)
→ cached Chromium → playwright test
→ vite build → production-mount smoke check
```

Those last two steps are feature 042's, and their placement was a decision
rather than a convenience. The mount and its fallback are covered hermetically
by `backend/tests/test_frontend_mount.py`, against a synthetic `index.html` in a
temporary directory — fast, and the right place for routing. What that cannot
see is the one thing only a **real** Vite build decides: whether the asset URLs
in the built `index.html` are root-relative and therefore resolve against the
mount. Setting `base` in `vite.config.ts` would leave every hermetic test green
and serve an app that loads nothing. So CI builds the frontend once and asserts,
against a running `python -m straticate`, that the root URL and a deep link are
the built app, that the asset the built page asks for is served, and that an
unknown `/api/v1/**` path is still the JSON envelope.

It costs about 15 s — a `vite build` and a server start — because it runs in the
job that **already has both toolchains installed**. A job of its own would have
spent a minute installing Python and Node to do the same thing, and adding Node
to the `backend` job would have slowed the pipeline's critical path. It is
deliberately *not* a Playwright spec: `webServer` is global to the config, so a
production spec would make every local `npm run e2e` build the frontend and
start a third server before running anything.

**FFmpeg is installed by the backend job and by the e2e job; the frontend job
does not install it** — and that matches who actually uses it. The backend
suite runs FFmpeg and ffprobe for real (generated audio fixtures, upload
probing, export transcoding, and ffprobe verification of what a transcode
produced); the e2e job generates its audio fixtures with FFmpeg and drives a
backend that transcodes real exports; the frontend job is Vitest against mocked
API responses and touches no media tooling, so an install there would be a
minute of CI spent on nothing.

The E2E tier is its **own job** rather than extra steps on `frontend`, because
it needs the backend installed as well as the frontend: bolting it onto the
lint/unit job would slow the pipeline's fastest feedback for every PR, while a
separate job runs alongside the other two and costs wall-clock only if it is
the slowest — and it is not. Measured on its first run (`ubuntu-latest`, cold
Playwright cache): **1 min 36 s** total — 24 s of apt FFmpeg, 6 s of `uv sync`
(PyTorch's CPU wheel is ~183 MiB, but `setup-uv`'s cache is keyed on
`backend/uv.lock` and shared with the backend job), 9 s of Node and `npm ci`,
17 s installing Chromium and its system libraries, and **34 s of tests**. The
browser is cached by `actions/cache` (~110 MB, keyed on `package-lock.json`),
so it is downloaded again only when `@playwright/test` moves — the next run,
restoring it from the cache, took **1 min 13 s**. Only Chromium is
installed — the tier tests correctness, not browser compatibility. It needs
**no GPU, no weights and no download**: every job it runs goes to the fake
separator. On failure it uploads the HTML report and the traces.

The e2e job used to install the whole backend including `torch`, which it never
uses, because `straticate.main` imported the RoFormer builder at import time
(`inference/registry.py`). **Feature 034 fixed that**: the builder imports
lazily and `torch` moved to an optional extra, so the e2e job syncs *without*
`--extra torch` and drops ~183 MiB. That omission is deliberate and load-bearing
— the tier drives the fake separator exclusively, so a plain `uv sync` there is
what proves on every PR that the application still imports, starts and serves
with PyTorch absent. If that step ever needs the extra, the lazy-import property
has regressed and that is what to fix.

A later `integration-gpu` workflow (manual `workflow_dispatch`, self-hosted or
skipped by default) covers real-model validation: it would install the weights
through the model manager and run `pytest -m integration`. CI must not download
models, and the CPU-wheel pin above is what keeps the backend job's
`uv sync --extra torch` from growing by gigabytes whenever `torch` is installed.

## Conventions

- Line endings are LF in the repository (`.gitattributes` enforces this);
  editors on Windows should respect `.editorconfig`.
- Python: Ruff is the formatter and linter; Pyright runs in strict mode for
  `src/straticate`.
- TypeScript: strict mode; ESLint + Prettier.
- Public interfaces (API routes, schemas, exported functions/components) carry
  docstrings/TSDoc.
