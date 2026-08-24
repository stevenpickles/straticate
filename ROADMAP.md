# Straticate Roadmap

This is the feature ledger and dependency graph for Straticate. It must be kept
current: every feature PR updates its own ledger row (status, branch, PR).

Feature numbers are zero-padded, sequential, and **never reused**. The number
appears in the branch name (`NNN-short-description`), the PR title
(`[NNN] Title`), and the feature document (`docs/features/NNN-*.md`).

Statuses: `PLANNED → READY → IN PROGRESS → PR OPEN → MERGED` (plus `BLOCKED`).

## Milestones

### M1 — Fake-separator end-to-end (first major technical milestone)

The complete workflow, with **no real ML model**:

```text
Open Straticate → drag/drop audio → upload succeeds → metadata appears
→ select separation mode → start job → fake inference begins
→ real-time WebSocket progress → model/GPU-style telemetry → cancel works
→ job completes → placeholder stems appear → preview (solo/mute/seek) works
→ export works
```

**Formal acceptance:** a person can run backend + frontend locally per
DEVELOPMENT.md and perform every step above against the fake separator, with
all normal CI green, on a machine with no GPU. Requires features 001–024.

### M2 — First real separation

HQ vocal separation (RoFormer-family) on CUDA with CPU fallback, verified
model download, real chunk progress, real telemetry. Features 025–026.

### M3 — v0.1.0 release

Fast + HQ vocal models, 4-stem model, capability-driven mode selection,
production build, release automation. Release PR `dev → main`, tag `v0.1.0`.

## Current state (2026-08-24)

Phases 0–6 (partly) are merged into `dev`: features 001–021 except 022–024.
**381 backend tests and 319 frontend tests**, all CI-enforced on every PR.

**The application works end to end in a browser.** Verified manually on a
GPU-free machine against a real backend and Vite dev server, not just the test
harness: a 3-minute MP3 was dropped in, ffprobe metadata rendered (with the
`bit_depth` row correctly absent for a lossy source), the four-stem mode was
chosen from catalog-derived options, and the job ran to `completed` at **13.7×
real time** with live chunk-grained progress and a populated telemetry panel —
model, device and processing, including real-time factor. A second 10-minute
job was cancelled mid-run: the UI settled on `cancelled` reporting the stage,
and **no partial stem was left on disk**. `GET /jobs/{id}/stems/{stem}` serves
`206 Partial Content` with `accept-ranges: bytes`.

The device shown is the fake separator's own honestly-labelled
`Straticate Fake Accelerator` / `backend: "fake"` / `fake:0`. That is
deliberate: feature 019 publishes the separator's `DeviceStats` verbatim
(feature 014's design) rather than deriving GPU identity from the compute
device, which is what lets the whole telemetry path be demonstrated on a
machine with no GPU. This **supersedes the telemetry sketch in
`docs/features/018-device-detection.md`**, which predates 014 — see
`docs/features/019-telemetry-sampler.md`.

Working today:

- **Backend** — the shared contract layer (Pydantic → OpenAPI → generated
  TypeScript), audio upload/probe/delete, the model catalog serving
  capability-derived separation modes, compute-device detection, the
  asynchronous job manager, the WebSocket event hub, the `Separator`
  abstraction with `FakeSeparator`, the job REST resource with the
  architecture-keyed `SeparatorRegistry`, the runtime telemetry sampler, and
  result + stem serving with byte-range support.
- **Frontend** — app shell, drag-drop upload, metadata panel, catalog-driven
  mode/quality selection that starts jobs, live progress with cancel and
  terminal-state handling, the session-wide job event socket with REST resync
  on reconnect, and the runtime telemetry panel.

**Remaining for M1 — features 022, 023, 024.** Stem export (022) is backend and
independent; the stem player (023) and export UI (024) complete the loop.
`023` and `022` may run in parallel; `024` follows both. Nothing else blocks
M1: every dependency of 022–024 is merged.

Deferred review findings from PRs #5, #8, #17 and #20 are tracked as feature
**029**; two of them (separator construction on the event loop, and
`Model.capabilities` never being consulted) are recorded there as work feature
**026** must carry out.

## Feature ledger

| #   | Feature                                      | Status  | Depends on | Branch | PR |
|-----|----------------------------------------------|---------|------------|--------|----|
| 001 | Repository bootstrap (docs, contracts, plan) | MERGED  | —          | `001-repository-bootstrap` | #1 |
| 002 | Backend skeleton (FastAPI, tooling, health)  | MERGED  | 001        | `002-backend-skeleton` | #2 |
| 003 | Frontend skeleton (Vite/React/TS, shell)     | MERGED  | 001        | `003-frontend-skeleton` | #3 |
| 004 | CI pipeline (backend + frontend checks)      | MERGED  | 002, 003   | `004-ci-pipeline` | #4 |
| 005 | API contracts v1 (schemas, OpenAPI → TS)     | MERGED  | 002        | `005-api-contracts` | #5 |
| 006 | Audio upload + validation + temp storage     | MERGED  | 005        | `006-audio-upload` | #6 |
| 007 | Audio metadata extraction (ffprobe)          | MERGED  | 006        | folded into 006 | #6 |
| 008 | Drag-drop + file picker + upload state UI    | MERGED  | 003, 005   | `008-drag-drop-ui` | #7 |
| 009 | Metadata display UI                          | MERGED  | 008        | `009-metadata-display` | #10 |
| 010 | Model catalog + capabilities backend         | MERGED  | 005        | `010-model-catalog` | #11 |
| 011 | Separation mode + quality selection UI       | MERGED  | 009, 010*, 015, 016 | `011-mode-selection-ui` | #19 |
| 012 | Job manager (queue, states, cancellation)    | MERGED  | 005        | `012-job-manager` | #8 |
| 013 | WebSocket event hub + typed events           | MERGED  | 012        | `013-websocket-hub` | #13 |
| 014 | Separator interface + FakeSeparator          | MERGED  | 012        | `014-fake-separator` | #15 |
| 015 | Job REST endpoints (create/get/cancel/list)  | MERGED  | 012, 014   | `015-job-endpoints` | #17 |
| 016 | Frontend job + WebSocket clients             | MERGED  | 003, 005*  | `016-job-ws-clients` | #14 |
| 017 | Progress UI + cancel + error handling        | MERGED  | 011, 015, 016 | `017-progress-cancel-ui` | #23 |
| 018 | Compute device detection + devices API       | MERGED  | 005        | `018-device-detection` | #12 |
| 019 | Runtime telemetry sampler + metrics events   | MERGED  | 013, 018   | `019-telemetry-sampler` | #21 |
| 020 | Telemetry panel UI (model/GPU/processing)    | MERGED  | 011, 016, 019* | `020-telemetry-panel` | #22 |
| 021 | Result management + stem serving             | MERGED  | 014, 015   | `021-result-serving` | #20 |
| 022 | Stem export (WAV24/float32/FLAC)             | PR OPEN | 021        | `022-stem-export` | #25 |
| 023 | Stem player UI (sync playback, solo, mute)   | PR OPEN | 017, 021   | `023-stem-player` | #26 |
| 024 | Export UI                                    | PLANNED | 022*, 023  | | |
| 025 | Model download manager (SHA-256, atomic)     | PLANNED | 010        | | |
| 026 | Real separator: HQ vocals (RoFormer-family)  | PLANNED | 014, 018, 025 | | |
| 027 | Real separator: fast vocals (MDX-family)     | PLANNED | 026        | | |
| 028 | 4-stem model + capability-driven modes       | PLANNED | 026        | | |
| 029 | Skeleton hardening (deferred #5 review finds)| PLANNED | 004, 005   | | |

`*` = depends only on that feature's *contract* (schemas/mocks), not its
implementation — the frontend feature may proceed against documented contracts,
generated types, and mock responses/events.

## Dependency graph

```mermaid
graph LR
  001 --> 002 & 003
  002 & 003 --> 004
  002 --> 005
  005 --> 006 --> 007
  003 & 005 --> 008 --> 009
  005 --> 010
  009 & 010 --> 011
  005 --> 012 --> 013
  012 --> 014
  012 & 014 --> 015
  003 & 005 --> 016
  011 & 016 --> 017
  005 --> 018
  013 & 018 --> 019
  011 & 016 & 019 --> 020
  014 & 015 --> 021 --> 022
  017 & 021 --> 023
  022 & 023 --> 024
  010 --> 025
  014 & 018 & 025 --> 026
  026 --> 027 & 028
```

## Parallel tracks

Backend and frontend proceed in parallel once a contract exists. Shared
contracts are established **first** (feature 005 and per-feature schema
additions); two agents never independently redefine the same contract.

```text
Backend track                      Frontend track
─────────────                      ──────────────
002 backend skeleton          ∥    003 frontend skeleton
005 API contracts v1               (frontend consumes generated types)
006 upload · 007 metadata     ∥    008 drop/picker UI · 009 metadata UI
010 model catalog             ∥    011 mode/quality selection
012 jobs · 013 WS · 014 fake  ∥    016 job/WS clients · 017 progress UI
018 devices · 019 telemetry   ∥    020 telemetry panel
021 results · 022 export      ∥    023 stem player · 024 export UI
```

Safe concurrency rule of thumb: features in different tracks with all
dependencies MERGED (or contract-only `*` dependencies documented) may run
simultaneously. Features touching `backend/src/straticate/schemas/` serialize
with each other.

## Phases (development plan)

- **Phase 0 — Repository:** 001, 002, 003, 004
- **Phase 1 — Contracts & skeletons:** 005 (plus 002/003 hardening)
- **Phase 2 — File ingestion:** 006, 007, 008, 009
- **Phase 3 — Separation configuration:** 010, 011
- **Phase 4 — Job infrastructure:** 012, 013, 014, 015, 016, 017
- **Phase 5 — Telemetry:** 018, 019, 020
- **Phase 6 — Results (fake stems):** 021, 022, 023, 024 → **M1**
- **Phase 7 — Real inference:** 025, 026 → **M2**
- **Phase 8 — Additional models:** 027, 028
- **Phase 9 — Model management UI, remote catalog, updates/removal** (features
  numbered when planned)
- **Phase 10 — Release:** production build, deployment docs, release
  automation → **v0.1.0** (M3)

Note the deliberate ordering: results/preview/export (Phase 6) is built against
the fake separator *before* real inference (Phase 7), so M1 proves the entire
application shell without ML risk.

## Initial agent assignments

| Agent | Feature | Notes |
| --- | --- | --- |
| Agent A (backend) | 002 | then 005 (contracts are backend-owned) |
| Agent B (frontend) | 003 | then 008 once 005's contracts merge |
| Agent C (infra) | 004 | after 002 + 003 merge |

Every assignment must follow the template in [AGENTS.md](AGENTS.md): feature
number, title, branch, objective, dependencies, scope, out-of-scope, expected
files, acceptance criteria, required tests.
