# AGENTS.md — Rules for coding agents

You are working on **Straticate**, a locally hosted music source separation web
app. Read this file completely before making any change.

## Architectural principles (non-negotiable)

1. **The ML model is a replaceable inference backend.** Never couple
   application code to a specific architecture (RoFormer/MDX/MDXC/Demucs).
   Work in terms of model IDs, capabilities, separation modes, stems, jobs,
   compute devices, and results. Model internals stay behind the `Separator`
   interface. See [ARCHITECTURE.md](ARCHITECTURE.md).
2. **Contracts are generated, not duplicated.** Pydantic schemas + OpenAPI are
   authoritative; frontend types are generated from them. Never hand-write a
   duplicate schema on the other side of the boundary.
3. **Progress is real work** (`completed_chunks / total_chunks`), pushed over
   WebSocket. No polling loops, no fake timers once real inference exists.
4. **Async jobs.** Never run inference inside a request handler; never keep the
   initiating HTTP request open during processing.
5. **Local-first, small-scale.** No Redis, Celery, Kubernetes, microservices,
   cloud services, or auth unless a documented requirement emerges.
6. **Frontend renders capabilities served by the backend.** Do not hardcode the
   UI around exactly two or four stems.

## Repository layout

See §3 of [ARCHITECTURE.md](ARCHITECTURE.md). Backend code lives in
`backend/src/straticate/` (packages: `api`, `schemas`, `audio`, `jobs`,
`models`, `inference`, `telemetry`, `system`); frontend in `frontend/src/`
(`api`, `ws`, `state`, `components`, `audio`).

## Git rules (violations are never acceptable)

- Permanent branches: `main` (releases only) and `dev` (integration).
- **Never push directly to `main` or `dev`.** Never force-push or delete them.
- **Every feature PR targets `dev`.** Feature branches never target `main`.
- **Exception: a release branch.** During a release, `release/vX.Y.Z` exists
  and work aimed at that release branches from it and PRs back into it, still
  as a numbered feature. It is not a licence to push directly — the same rules
  apply, only the base changes. Unless your assignment names a release branch,
  your base is `dev`.
- Work happens on numbered feature branches: `NNN-short-description`, branched
  from up-to-date `dev`. Numbers are zero-padded, sequential, never reused.
- PR titles: `[NNN] Feature Name`. Squash merge. The one PR that does **not**
  squash is the release PR `release/vX.Y.Z` → `main`, which rebase-merges; it
  is the project owner's to open and merge, never an agent's. See
  [CONTRIBUTING.md](CONTRIBUTING.md#release-process).

## Your assignment

You work on **exactly one** numbered feature branch. Your assignment specifies:

```text
Feature number · Feature title · Branch name · Objective · Dependencies
Scope · Out-of-scope work · Expected modules/files · Acceptance criteria
Required tests
```

If any of these are missing or ambiguous, stop and ask — do not improvise
scope.

### Scope discipline

- Implement only what the assignment covers. If you notice an unrelated bug,
  missing feature, or tempting refactor: **do not touch it.** Note it in your
  PR's "Known Limitations" (or report it) so a new numbered feature can be
  opened.
- Do not modify shared contracts (`backend/src/straticate/schemas/`, WebSocket
  event definitions, `models/schemas/`) unless your assignment explicitly says
  the contract is yours to change. Two branches must never redefine the same
  contract.
- Do not renumber, reorder, or rewrite ROADMAP entries other than your own row.

### Finding dependencies

Check your feature's row in [ROADMAP.md](ROADMAP.md). Dependencies must be
`MERGED` into `dev` before you rely on their code. A dependency marked `*` is
contract-only: build against the documented contract
([docs/contracts/](docs/contracts/)), generated types, and mocks — not against
unmerged implementation.

## Quality bar (Definition of Done)

Before opening your PR, from the relevant directory:

- Backend: `uv run --extra torch ruff format --check .` ·
  `uv run --extra torch ruff check .` · `uv run --extra torch pyright` ·
  `uv run --extra torch pytest` — all green.
  Since feature 034 PyTorch is an **optional extra**, so a plain `uv sync`
  leaves it out — and the suite then fails at *collection* in the six modules
  that import it (it covers the real separator; nothing is guarded with
  `pytest.importorskip`, deliberately). `--extra torch` is what puts it back,
  and the `backend` CI job passes it on every step for the same reason. The
  application itself runs fine without it — that is the point of 034; it is the
  *test suite* that needs it.
  **Running a CUDA build of torch? Read DEVELOPMENT.md ("PyTorch and CUDA")
  before using these commands.** `--extra torch` makes torch a required package
  again, so uv reconciles it against the lock — which pins the CPU wheel — and
  a GPU run can end up silently on CPU. DEVELOPMENT.md has the measured
  behaviour of both cases; do not second-guess it from here.
- Frontend: `npm run format:check` · `npm run lint` · `npm run typecheck` ·
  `npm test` · `npm run build` — all green.
- Tests exist for new behavior (see the test strategy in
  [DEVELOPMENT.md](DEVELOPMENT.md)); every acceptance criterion is verifiably
  met; public interfaces are documented.
- Update `docs/features/NNN-*.md` (create from the template in
  `docs/features/README.md`) and your ROADMAP ledger row in the same PR.

Then open a PR to `dev` using the body template in
[CONTRIBUTING.md](CONTRIBUTING.md). Working code alone is not done.
