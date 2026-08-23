# [003] Frontend skeleton

Branch: `003-frontend-skeleton`
Status: PR OPEN
Dependencies: 001
PR: #3

## Objective

A React + TypeScript + Vite application skeleton: app shell and layout, typed
API abstraction, workflow state structure, and the full quality toolchain — the
foundation every later frontend feature builds on.

## Scope

- `frontend/` Vite (react-ts) project, strict TS + `noUncheckedIndexedAccess`,
  committed `package-lock.json`, scoped `.gitignore`
- `src/api/client.ts` — typed `get`/`post` wrapper, `ApiError` parsing the
  backend error envelope (with non-JSON fallback), `getHealth()`, `getVersion()`
- `src/state/appState.tsx` — `WorkflowPhase`
  (`select | configure | separate | inspect | export`), context + reducer,
  `useAppState`/`useAppDispatch` (no state library)
- `src/components/` — `Header` (name, tagline, backend version /
  "backend unavailable" indicator), `Workspace` (phase placeholder)
- Dev proxy `/api → http://localhost:8000` with `ws: true`
- ESLint (flat config, typescript-eslint, react-hooks, react-refresh,
  prettier-compat) + Prettier + Vitest/Testing Library
- npm scripts (stable names CI depends on): `dev`, `build`, `preview`, `test`,
  `test:watch`, `lint`, `typecheck`, `format`, `format:check`

## Out of scope

Drag-drop/upload UI, WebSocket client, progress/telemetry UI, stem player,
generated OpenAPI types (feature 005 adds `generate:api`), CI workflows.

## Acceptance criteria

- [x] `npm ci` then `format:check`, `lint`, `typecheck`, `test` (14 tests), `build` all green
- [x] Dev server serves the shell; proxy targets the backend with WS enabled
- [x] No stem counts or model architectures hardcoded
- [x] Public components/functions have TSDoc

## Required tests

Vitest + Testing Library: shell render, header version/unavailable states,
reducer transitions, API client success + error-envelope + fallback paths.

## Notes / decisions

- Current Vite template ships oxlint; replaced with ESLint flat config per
  project tooling decisions.
- Header polls the backend once on mount; live status arrives with the
  WebSocket client feature (016).
