# [001] Repository bootstrap

Branch: `001-repository-bootstrap`
Status: PR OPEN
Dependencies: —

## Objective

Establish Straticate as a structured, agent-friendly project: core
documentation, architecture, contracts, model-manifest schema, feature ledger,
and development process — before substantial implementation begins.

## Scope

- Root documentation: README, LICENSE (MIT), ARCHITECTURE, ROADMAP,
  DEVELOPMENT, CONTRIBUTING, AGENTS
- Contract proposals: `docs/contracts/rest-api.md`,
  `docs/contracts/websocket-events.md`
- Model manifest JSON Schema (`models/schemas/model-manifest.schema.json`) and
  seed catalog (`models/catalog.json`)
- Feature documentation structure and template (`docs/features/`)
- Repo hygiene: `.gitignore`, `.gitattributes`, `.editorconfig`
- The initial planning pass: job state machine, abstractions, first 28
  numbered features, dependency graph, parallel tracks, agent assignments,
  CI plan, test strategy, M1 definition

## Out of scope

- Any backend or frontend code (features 002/003)
- CI workflows (feature 004)

## Acceptance criteria

- [x] All documents listed above exist and are internally consistent
- [x] ROADMAP contains the ledger, dependency graph, milestones, and phases
- [x] AGENTS.md fully specifies agent rules and the assignment template
- [x] Model manifest schema validates the example catalog entry
- [x] PR targets `dev` with title `[001] Repository bootstrap`

## Required tests

None (documentation-only; no toolchain exists yet).

## Notes / decisions

- Results/preview/export are scheduled **before** real inference so milestone
  M1 proves the whole application against the fake separator.
- Frontend types will be generated from OpenAPI (`openapi-typescript`);
  decision recorded in ARCHITECTURE.md §4.
- Ledger `*` marker introduced for contract-only dependencies to enable
  parallel frontend/backend work.
