# [005] API contracts v1

Branch: `005-api-contracts`
Status: PR OPEN
Dependencies: 002
PR: #5

## Objective

The shared contract layer exists: Pydantic v2 schemas for all core entities
and WebSocket events, an OpenAPI export script that includes every contract
schema, and generated TypeScript types consumed by the frontend. Backend and
frontend features now develop in parallel against these types without ever
hand-writing duplicate schemas.

## Scope

- `backend/src/straticate/schemas/` package:
  - `common.py` — `ErrorInfo`, `ErrorEnvelope`, `HealthStatus`, `VersionInfo`
  - `audio.py` — `AudioMetadata`, `AudioFile`
  - `models.py` — `Model`, `ModelRequirements`, `QualityOption`,
    `SeparationMode`
  - `devices.py` — `ComputeDevice`
  - `jobs.py` — `JobState` (str enum with `is_terminal`),
    `SeparationConfiguration`, `Stem`, `SeparationResultMetrics`,
    `SeparationResult`, `Job`
  - `events.py` — the eight WebSocket event models plus the `WebSocketEvent`
    discriminated union (on `type`), with nested `ModelInfo`, `GpuMetrics`
    (nullable), `ProcessingMetrics`
- `errors.py` refactored to build responses from `ErrorEnvelope`/`ErrorInfo`
  (one source of truth for the envelope); `api/system.py` returns
  `HealthStatus`/`VersionInfo` so those endpoints have named OpenAPI schemas.
- `backend/src/straticate/scripts/export_openapi.py` — exports the OpenAPI
  document with all contract schemas injected into `components.schemas`.
- Frontend: `openapi-typescript` dev dependency, `npm run generate:api`,
  committed `src/api/generated/api.d.ts`, friendly aliases in
  `src/api/types.ts`, and `getHealth`/`getVersion` typed with generated types.
- Contract docs flipped from "proposal" to authoritative; DEVELOPMENT.md "API
  types" section finalized.

## Out of scope

- New REST endpoints (audio/jobs/models routes: features 006/010/015/018)
- The WebSocket server/event hub (feature 013)
- Frontend UI changes; CI workflow changes

## Expected modules/files

- `backend/src/straticate/schemas/{__init__,common,audio,models,devices,jobs,events}.py`
- `backend/src/straticate/scripts/{__init__,export_openapi}.py`
- `backend/tests/{test_schemas,test_export_openapi}.py`
- `frontend/src/api/generated/api.d.ts` (committed), `frontend/src/api/types.ts`,
  `frontend/src/api/types.test.ts`

## Acceptance criteria

- [x] Every entity/event in docs/contracts appears in the exported OpenAPI
      `components.schemas` (verified by test)
- [x] `uv run python -m straticate.scripts.export_openapi [output]` writes a
      valid document (default `backend/openapi.json`, gitignored)
- [x] `npm run generate:api` regenerates `src/api/generated/api.d.ts`; the
      committed copy matches the current schemas
- [x] `WebSocketEvent` is a discriminated union on `type` in both Python
      (pydantic) and generated TypeScript (exhaustive `switch` narrows)
- [x] `errors.py` builds the envelope from the shared schemas
- [x] All backend and frontend quality checks green

## Required tests

- Schema JSON round-trips for representative entities (AudioFile, Model,
  SeparationMode, ComputeDevice, Job, ErrorEnvelope)
- `JobState.is_terminal` (terminal = completed/cancelled/failed exactly)
- Discriminated union parses each documented event example; rejects unknown
  `type`, missing `type`, and invalid payloads; GPU block nullable
- Export script writes valid JSON to a tmp path containing all expected
  component names; discriminator mapping complete; all `$ref`s resolve
- Frontend: type-level smoke test constructing typed sample objects and
  exhaustively narrowing `WebSocketEvent` (compile-time via typecheck plus
  trivial runtime assertions)

## Notes / decisions

- **WS schemas → OpenAPI injection:** FastAPI only emits schemas referenced by
  routes, so `export_openapi.py` merges the JSON Schemas of all contract root
  models (`pydantic.json_schema.models_json_schema`) and the
  `WebSocketEvent` union (`TypeAdapter(...).json_schema`) into
  `components.schemas` using ref template `#/components/schemas/{model}`,
  deduplicating `$defs`. Schemas already registered by a route keep the
  route-generated definition (`setdefault`). Validation-mode JSON schemas are
  used throughout so request- and response-side types share one name.
- **Committed generated types:** `frontend/src/api/generated/api.d.ts` is
  committed so frontend CI/development never needs the backend. Regenerate
  (`export_openapi` + `npm run generate:api`) and commit in the same PR
  whenever schemas change. `backend/openapi.json` stays gitignored. The
  generated file is excluded from Prettier and ESLint (machine-formatted);
  app code imports aliases from `src/api/types.ts`, never the raw file.
- **`SeparationConfiguration` includes `audio_id`** (it is the create-job
  request body), so a serialized `Job.configuration` also carries `audio_id`
  in addition to the job's top-level `audio_id` — the abridged example in
  rest-api.md elided it.
- **Stage fields reuse `JobState`** (`stage`, `previous_stage`,
  `stage_at_cancellation`, `ProcessingMetrics.stage`) rather than a separate
  processing-stage enum; the contract doc says stages "use the job state
  machine's processing states".
- **Nullable-when-unknown fields are required** (e.g. `Job.started_at`,
  `RuntimeMetricsEvent.gpu`, `AudioMetadata.bit_depth`): producers must pass
  them explicitly, and generated TS gets `T | null` instead of optional —
  received payloads always carry every field. Only true input-side defaults
  (`SeparationConfiguration.device_id`, `Model.requirements`,
  `ErrorInfo.detail`) have defaults.
- IDs are ULID-style plain `str` fields (no ulid dependency at the contract
  layer); datetimes are `pydantic.AwareDatetime` (timezone-aware, ISO-8601);
  `progress` is validated to [0, 1].
- **openapi-typescript vs TypeScript 6:** `openapi-typescript@7.13` declares a
  `typescript@^5.x` peer; the project uses TS 6. An npm `overrides` entry pins
  its peer to the root `typescript` version. Output verified by typecheck,
  tests, and build. Remove the override once upstream supports TS 6.
