# Straticate

**Straticate** is a locally hosted, open-source web application that separates mixed
music files into their constituent stems using pretrained neural-network music
source separation models.

The name is a portmanteau of **strata** and **extricate**: the application
extricates the individual musical layers from a finished mix.

## Workflow

```text
Select → Configure → Separate → Inspect → Export
```

1. Open Straticate in a browser.
2. Drag and drop a music file (or use the file picker).
3. Review information about the uploaded audio.
4. Choose how granular the separation should be (e.g. Vocals/Instrumental, or Vocals/Drums/Bass/Other).
5. Start the separation job.
6. Watch real processing progress plus model, GPU, VRAM, and inference statistics.
7. Preview the resulting stems with solo/mute.
8. Export individual stems or all of them.

Straticate is deliberately focused on this workflow. It is an inspection and
extraction tool, not a DAW, and it will not evolve into one.

## Run it

**One process, one URL.** Build the frontend once; after that, starting
Straticate is a single command.

You need [uv](https://docs.astral.sh/uv/), Node.js ≥ 20 and FFmpeg (with
`ffprobe`) on `PATH`. A GPU is optional.

```bash
# once, to build the app
cd frontend && npm ci && npm run build

# every time, to run it
cd backend && uv sync --extra torch && uv run python -m straticate
```

Then open **<http://127.0.0.1:8000>**. The same process serves the interface and
the API; nothing else needs to be running.

`--extra torch` installs PyTorch, which the real separation models need. It is
the CPU build; installing a CUDA build is one documented command and changes
nothing else (see [DEVELOPMENT.md](DEVELOPMENT.md), *PyTorch and CUDA*). Model
weights are not bundled either: the app shows you each model's licence terms and
downloads only the one you choose, the first time you ask for a separation.

`STRATICATE_HOST`, `STRATICATE_PORT` and the rest of the settings are documented
in `backend/src/straticate/config.py`. Straticate binds to loopback and has no
authentication: it is a local-first tool, not a service to expose.

Haven't built the frontend? The server still starts and the whole API still
works — the root URL just tells you what to build.

## Develop it

Development is a different arrangement, and deliberately so: the Vite dev server
on `:5173` with hot reload, proxying `/api` to the backend on `:8000`. That is
two terminals, and it is what you want while editing the frontend — but it is
not how the application is meant to be *used*. See
[DEVELOPMENT.md](DEVELOPMENT.md) for setup, both run modes, the test tiers and
the quality gates.

## Architecture at a glance

```text
Browser (React + TypeScript)
        │  REST + WebSocket
FastAPI backend (jobs, audio, models, telemetry)
        │
Separation Engine (replaceable model backends: RoFormer, MDX, MDXC, Demucs, …)
        │
PyTorch → CUDA / CPU / future accelerators
```

The core architectural principle: **the ML model is a replaceable inference
backend**. The application works in terms of model IDs, capabilities,
separation modes, stems, jobs, compute devices, and results — never in terms of
a specific network architecture. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

**Pre-alpha — under active initial development.**

The project is being built in small, numbered feature branches merged into the
`dev` integration branch. The first major milestone is a complete
browser → backend → job → WebSocket → results workflow using a *fake* separator,
proving the architecture before any real ML model is integrated. See
[ROADMAP.md](ROADMAP.md) for the feature ledger and current status.

## Repository layout

```text
backend/    FastAPI + PyTorch backend (Python 3.12, uv)
frontend/   React + TypeScript + Vite frontend
models/     Model catalog and manifest schemas
docs/       Contracts, feature documentation, planning
scripts/    Development and automation scripts
testdata/   Small audio fixtures for tests
```

## Documentation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System components, boundaries, job model, abstractions |
| [ROADMAP.md](ROADMAP.md) | Phases, numbered feature ledger, dependency graph |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Environment setup, running, testing, quality checks |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branching model, PR process, release workflow |
| [AGENTS.md](AGENTS.md) | Rules for coding agents working on this repository |
| [docs/contracts/](docs/contracts/) | REST API and WebSocket event contracts |

## License

Straticate itself is **MIT** — see [LICENSE](LICENSE).

**Model weights are a separate question, and not all of them are permissive.**
Weights are never bundled or redistributed here: the catalog pins a download URL
and a SHA-256, and you install them yourself. A model's source-code license does
not automatically apply to its weights, and in practice usually does not.

What ships in the catalog today:

| Model | Code | Weights | Commercial use |
| --- | --- | --- | --- |
| `vocals-hq-001` — Mel-Band RoFormer (Kim Vocal 2) | MIT | **MIT** | Permitted |
| `standard-stems-001` — Demucs v4 (htdemucs) | MIT | **Research use only** — upstream states the weights are "not covered by the MIT license, and are provided only for scientific purposes"; no formal license was designated | **Not permitted** |

Every model's terms — code license, weights license, commercial-use and
redistribution permissions, and required attribution — are recorded in
`models/catalog.json` and **shown in the app before you install anything**, which
is the only moment they can still change your decision. Terms stated in prose
rather than as a named license are rendered verbatim rather than summarised, so
a paragraph of conditions is never presented as if it were an identifier.

If you intend to use Straticate commercially, check each model's weights license
before installing it. The application being MIT does not make the models so.
