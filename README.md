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

MIT — see [LICENSE](LICENSE).

Note: pretrained model *weights* are downloaded separately and carry their own
licenses, which are tracked per-model in the model catalog. A model's
source-code license does not automatically apply to its weights.
