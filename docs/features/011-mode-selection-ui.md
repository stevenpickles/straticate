# [011] Separation mode + quality selection UI

Branch: `011-mode-selection-ui`
Status: PR OPEN
Dependencies: 009, 010, 015, 016
PR: #19

## Objective

After uploading a file the user can choose **what** to separate (a separation
mode) and **how well** (a quality tier) — both rendered entirely from
`GET /api/v1/separation-modes` — and press a button that actually starts the
job via `POST /api/v1/jobs`. This is the first UI that creates a job, so it
closes the loop from the browser to the fake separator.

## Scope

- `frontend/src/api/modes.ts` — `listSeparationModes()` over the shared `get`
  helper, typed with the contract aliases from `api/types.ts`.
- `frontend/src/state/appState.tsx` — a `configure` slice holding the catalog
  fetch state, the selected `modeId`/`qualityId`, and the create-job request
  state, with the selection rules in the reducer (see *Notes*).
- `frontend/src/components/SeparationOptions.tsx` (+ `SeparationOptions.css`) —
  the configure panel: mode radio group with each mode's stems, quality radio
  group for the selected mode, "Start separation", and loading/error/retry
  states for both the catalog fetch and the create request.
- `frontend/src/components/Workspace.tsx` — mounts `<SeparationOptions />` in
  the `configure` phase and `<SeparationProgress />` + `<TelemetryPanel />` in
  the `separate` phase.
- `frontend/src/components/SeparationProgress.tsx`,
  `TelemetryPanel.tsx` (+ their `.css`) — minimal placeholders handed to
  features 017 and 020 (see *Component ownership*).
- `frontend/src/index.css` — the per-component CSS convention comment; the
  now-unused `.separation-options-placeholder` rule was dropped with the
  placeholder markup it styled.
- `frontend/src/test/fixtures.ts` — `sampleSeparationModes`.

## Out of scope

- Anything under `backend/` and `frontend/src/api/generated/api.d.ts`
  (everything needed is already served; 019 and 021 were editing the backend in
  parallel).
- `frontend/src/state/jobState.tsx`, `frontend/src/ws/*`, and
  `frontend/src/api/{client,audio,jobs,types}.ts` — 016 built them; they are
  consumed, not modified.
- The progress bar, stage rendering, cancel button and job error UI (**017**),
  including calling `useJobEvents()` — nothing in this feature opens the job
  socket.
- The telemetry panel's real content (**020**).
- A compute-device picker: the create request omits `device_id` and the backend
  picks. No feature owns device selection UI yet.
- Stem player (023) and export UI (024).

## Expected modules/files

- `frontend/src/api/modes.ts` · `modes.test.ts`
- `frontend/src/state/appState.tsx` · `appState.test.tsx`
- `frontend/src/components/SeparationOptions.tsx` · `.test.tsx` · `.css`
- `frontend/src/components/SeparationProgress.tsx` · `.css`
- `frontend/src/components/TelemetryPanel.tsx` · `.css`
- `frontend/src/components/Workspace.tsx` · `Workspace.test.tsx`
- `frontend/src/components/{AudioSummary,DropZone}.test.tsx` (rewired for the
  new tree — the components themselves are untouched)
- `frontend/src/index.css` · `frontend/src/test/fixtures.ts`
- `docs/features/011-mode-selection-ui.md` · `ROADMAP.md`

## Acceptance criteria

- [x] Mode names, stem lists and quality-tier labels are rendered entirely from
      `GET /separation-modes`; no mode ID, stem name, tier label or model ID is
      hardcoded anywhere in `src/components` or `src/state`.
- [x] A two-stem mode and a four-stem mode both render correctly, and a mode
      with a single quality option still shows it.
- [x] Selecting a different mode resets the quality selection to that mode's
      first option.
- [x] "Start separation" issues exactly one `POST /jobs` with the selected
      `audio_id`/`mode_id`/`quality_id` and **no** `device_id`, then tracks the
      returned job (`job/track`) and advances the workflow to `separate`.
- [x] A failed create renders the envelope's `message` and the button becomes
      usable again; a failed catalog fetch renders an error with a working
      retry.
- [x] The button is disabled while a create is in flight (no double submit).
- [x] `Workspace.tsx` renders `<SeparationProgress />` and `<TelemetryPanel />`
      in the `separate` phase, and neither 017 nor 020 needs to edit
      `Workspace.tsx` or `index.css` to do their work.
- [x] `npm run format:check` · `lint` · `typecheck` · `test` · `build` green.

## Required tests

- `modes.test.ts` — request URL and parsing (including 2-stem/4-stem and
  multi-/single-tier payloads); a typed `ApiError` from the error envelope.
- `appState.test.tsx` — preselection of the first mode and its first tier; an
  empty catalog selects nothing; mode change resets the tier; a tier from
  another mode is never accepted; selections before load and unknown IDs are
  ignored; `upload/reset` clears the slice; the create-request state machine
  (`creating` → `idle` + phase `separate`, → `error`, and retry).
- `SeparationOptions.test.tsx` — renders modes/stems/tiers from a fixture with
  a 2-stem **and** a 4-stem mode; the single-option mode renders its one tier;
  the catalog loads once per mount; selecting a mode swaps the tier options and
  resets the selection; start posts the exact expected body and advances the
  phase; the button is disabled in flight; a double click submits once; a
  create failure shows the envelope message, re-enables the button and allows a
  retry that succeeds; a catalog failure shows a retry that refetches.
- `Workspace.test.tsx` — each phase mounts the right components, including both
  `separate`-phase placeholders, and the telemetry region only once metrics
  exist.

## Notes / decisions

### Component ownership from now on

| File | Owned by |
| --- | --- |
| `components/SeparationOptions.tsx` · `.css` · `.test.tsx` | 011 |
| `components/SeparationProgress.tsx` · `.css` | **017** |
| `components/TelemetryPanel.tsx` · `.css` | **020** |
| `components/Workspace.tsx` | 011 (phase scaffolding; 017/020 need not touch it) |
| `state/appState.tsx` | 011 (workflow + upload + configure slices) |
| `state/jobState.tsx`, `ws/*`, `api/{client,audio,jobs,types}.ts` | 016 |

`SeparationProgress.tsx` and `TelemetryPanel.tsx` exist only so the `separate`
phase has mount points. Each carries a `TODO(feature NNN)` naming its owner and
renders the plainest possible thing; 017 and 020 replace the bodies (and fill
in the sibling `.css`) without editing `Workspace.tsx` or `index.css`.

### CSS convention: a component owns a sibling stylesheet

`index.css` is a single global file and would otherwise become a three-way
merge conflict between 011, 017 and 020. From this feature onwards:

- **A new component owns `ComponentName.css`, imported by its own module**
  (`import './SeparationOptions.css'`). Vite bundles it into the production
  stylesheet; Vitest processes it too (`test.css: true` in `vite.config.ts`).
  Both were verified — `npm run build` emits the rules and `npm test` is green.
- **`index.css` keeps global and app-shell styles only** (design tokens,
  `body`, `.app`, `.header`, `.workspace`, and the pre-existing component rules
  that were already there).
- **Existing styles were not moved.** Relocating `.drop-zone*`,
  `.audio-summary*` or the shared `.progress-*` rules would be pure churn for
  other branches to conflict with. The convention applies going forward.

The one deletion is `.separation-options-placeholder`, whose only markup this
feature replaced.

### Selection rules live in the reducer

`configure/modesLoaded` preselects the first mode and its **first**
`quality_options` entry — the backend already orders options
`fast → balanced → high_quality` (feature 010), so "first" *is* the sensible
default and the client needs no tier table of its own.
`configure/modeSelected` always recomputes `qualityId` from the newly selected
mode, so a `quality_id` belonging to another mode can never survive; and
`configure/qualitySelected` ignores any tier that is not an option of the
current mode. Both rules are unit-tested against the reducer directly, which is
why they live there rather than in the component.

`upload/reset` clears the whole configure slice: the catalog is cheap to
refetch and any selection referred to a file that is gone.

### Why the create request omits `device_id`

`SeparationConfiguration.device_id` is optional and the backend resolves the
best device, echoing it back on `Job.configuration.device_id` (015). Sending
nothing is therefore both correct and the only option, since no feature owns a
device picker yet. The component asserts this in its test by inspecting the
posted body's keys.

### Double submit is guarded by a ref, not by state

The button is `disabled` while `create.status === 'creating'`, but the click
handler additionally short-circuits on a `useRef` flag that flips
*synchronously*, before React re-renders. That is what makes a genuine
double click a single `POST`, and it is what the test asserts.

## Known limitations

- **Nothing opens the job WebSocket yet.** `useJobEvents()` is not called
  anywhere, so after the workflow reaches `separate` the placeholder shows the
  queued job as returned by REST and does not update. Feature 017 mounts the
  hook (and should use its `onOpen` option to refetch `getJob(id)`).
- The `separate` phase has no way back to `configure`; re-selecting a file
  (`upload/reset`) is the only reset path. Whether a "start another
  separation" affordance belongs to 017 or a later feature is unclaimed.
- `AudioSummary.test.tsx` and `DropZone.test.tsx` render `Workspace`, so both
  needed a `JobStateProvider` wrapper and a mocked `../api/modes` once the
  configure phase started mounting `SeparationOptions`. The components
  themselves were not touched; `AudioSummary.test.tsx` lost its assertion about
  the removed "Separation options coming next." placeholder.
- Two frontend nits inherited from 016 were left alone as out of scope, and are
  already recorded in 015's known limitations: `listJobs()`'s TSDoc claims
  newest-first ordering (the backend is oldest-first), and `jobs.test.ts` mocks
  a `job_not_cancellable` (409) response the backend never produces. Both are
  017's to correct.
