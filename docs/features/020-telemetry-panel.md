# [020] Telemetry panel UI (model / GPU / processing)

Branch: `020-telemetry-panel`
Status: PR OPEN
Dependencies: 011, 016, 019\*
PR: #…

## Objective

While a job runs, the user can see what the machine is actually doing: which
model is loaded, what the compute device is doing (VRAM, utilization,
temperature) when there is one, and how the processing is going — stage, chunk
counts, elapsed time, audio processed, and the **real-time factor**
(ARCHITECTURE.md §12). The `separate` phase's telemetry mount point stops being
a placeholder.

## Scope

- `frontend/src/components/TelemetryPanel.tsx` (+ `.css`) — renders the newest
  `runtime_metrics` event from `useJobState().metrics` as three labelled
  groups: **Model**, **Device** (omitted entirely when `gpu` is `null`) and
  **Processing**.
- `frontend/src/format.ts` — three new pure formatters: `formatPercentage`
  (a `0..1` fraction → `91%`), `formatTemperature` (→ `63 °C`) and
  `formatRealtimeFactor` (→ `7.9×`). VRAM reuses the existing
  `formatFileSize`; durations reuse `formatDuration`.
- `frontend/src/test/fixtures.ts` — `sampleFakeDeviceRuntimeMetrics` (the
  development separator's honest `backend: "fake"` device) and
  `sampleCpuRuntimeMetrics` (`gpu: null`), alongside the CUDA-shaped
  `sampleRuntimeMetrics` that 016 already added.
- `ROADMAP.md` — the 020 ledger row and its dependency-graph edge.

## Out of scope

- Everything under `backend/` and `frontend/src/api/generated/api.d.ts` (019
  and 021 were editing the backend in parallel). This feature is built strictly
  against `docs/contracts/websocket-events.md` and the generated types; the
  telemetry sampler that *produces* the event is **019**, a contract-only
  dependency here.
- `frontend/src/components/SeparationProgress.*`, `state/jobState.tsx`,
  `App.tsx` and `ws/**` — owned by **017** this wave. In particular nothing
  here calls `useJobEvents()`: 017 mounts the socket.
- `Workspace.tsx` (already mounts `<TelemetryPanel />`) and `index.css` (the
  panel owns `TelemetryPanel.css`, per the convention 011 introduced).
- Charts, sparklines, or any metrics history — the store keeps exactly one
  sample and this panel renders that sample.
- Compute-device *selection* UI, the stem player (023) and export UI (024).

## Expected modules/files

- `frontend/src/components/TelemetryPanel.tsx` · `.css` · `.test.tsx`
- `frontend/src/format.ts` · `format.test.ts`
- `frontend/src/test/fixtures.ts`
- `docs/features/020-telemetry-panel.md` · `ROADMAP.md`

## Acceptance criteria

- [x] All three groups render from a `runtime_metrics` fixture with values
      formatted for humans (`8.6 GB`, `91%`, `63 °C`, `0:18`, `7.9×`).
- [x] `metrics === null` renders nothing at all — no panel of empty rows.
- [x] `gpu === null` omits the entire device group and still renders the model
      and processing groups.
- [x] `utilization === null` and `temperature_celsius === null` omit only their
      own rows; the memory rows still render in both cases and when both are
      null together.
- [x] A CUDA-shaped device and the development separator's `backend: "fake"`
      device both render correctly: the panel never assumes a device is a GPU,
      and never assumes it is not.
- [x] Nothing branches on a model ID, architecture, stem name, mode ID or
      device backend; the panel renders whatever the contract delivers.
- [x] The panel updates when a newer `runtime_metrics` event reaches the store.
- [x] `index.css` is untouched; all styles live in `TelemetryPanel.css`.
- [x] `npm run format:check` · `lint` · `typecheck` · `test` · `build` green.

## Required tests

- `TelemetryPanel.test.tsx` — full render from the CUDA-shaped fixture
  (asserting formatted strings, not raw numbers); `metrics === null`;
  `gpu === null`; `utilization === null` alone; `temperature_celsius === null`
  alone; both null with the memory rows still shown; the `fake`-shaped device;
  and a newer sample dispatched through the real reducer replacing the older
  values.
- `format.test.ts` — table-driven cases for each new formatter including
  non-finite (`NaN`, `±Infinity`), negative and out-of-range input.

## Notes / decisions

### Nullability is the contract, not a rendering accident

Three independent null cases exist and each is handled by *absence*, never by a
substitute value:

| Contract state | Rendering |
| --- | --- |
| `metrics === null` (no event yet) | the component returns `null` — nothing mounts |
| `gpu === null` (no compute device) | the whole **Device** group is dropped |
| `utilization` / `temperature_celsius` `=== null` (no NVML) | only that row is dropped; memory rows are unaffected |

`deviceFields()` and `telemetryGroups()` build their arrays with spread-empty
(`...(x === null ? [] : [row])`), the same shape `AudioSummary` already uses
for the nullable bit-depth/bit-rate rows. There is no `—` placeholder and no
zero: a row the backend could not measure simply is not there.

`Workspace.test.tsx` (feature 011) already asserts that the telemetry region is
absent until metrics arrive, so the `metrics === null` behaviour is pinned from
both sides.

### VRAM keeps the app's single size convention

Memory figures go through the existing `formatFileSize`, which uses **binary**
units with conventional `KB`/`MB`/`GB` labels by documented convention. A
second byte formatter (or a decimal-unit one for VRAM specifically) would put
two size conventions in one UI, so `9234179686` bytes reads `8.6 GB` here
exactly as a file of that size would in the metadata panel.

### The three new formatters

- `formatPercentage(fraction)` — the contract documents `utilization` as
  `[0, 1]`, so values above `1` are **clamped** to `100%` rather than rendered
  as `140%`; negative and non-finite input renders `0%`.
- `formatTemperature(celsius)` — rounds to whole degrees and keeps negative
  readings (`-0` is normalised so it never renders `-0 °C`); non-finite input
  renders `0 °C`.
- `formatRealtimeFactor(factor)` — one decimal from `1×` upwards, **two below
  it** so a run slower than real time reads `0.42×` instead of collapsing to
  `0×`; trailing zeros are dropped (`12.0` → `12×`).

All three follow the house style of the module: pure, no React, `@example`
TSDoc, and a zero-ish fallback for non-finite input (matching `formatDuration`
→ `0:00` and `formatFileSize` → `0 B`).

### Contract values are shown verbatim; only the stage is humanised

`architecture`, `separation_mode`, `backend` and `device_id` are identifiers
and this is a diagnostic panel, so they render exactly as the contract delivers
them. The single exception is `processing.stage`, a job state-machine token
that reads as prose in the UI: a purely mechanical `humanize()` turns
`loading_model` into `Loading model`. It enumerates no stage names, so a stage
added to `JobState` later renders without a code change.

### No socket, no history

The panel is a pure reader of `useJobState().metrics`. It does not call
`useJobEvents()` — 017 owns mounting the socket — and it keeps no buffer of
past samples, because the store deliberately keeps only the newest one.

## Known limitations

- Until 017 lands nothing opens the job WebSocket, so in the running app the
  panel stays unmounted (no `runtime_metrics` ever reaches the store) even
  though every rendering path is covered by tests.
- The panel shows `processing.audio_processed_seconds` but not a total audio
  duration: `RuntimeMetricsEvent.processing` carries no
  `audio_total_seconds` (`job_progress` does). "Audio Processed" is therefore
  an absolute figure rather than `2:28 / 3:47`. Changing that would be a
  contract change and was left alone.
- `ROADMAP.md`'s 020 row and graph edge now record 011 and 019\* alongside 016;
  the ledger previously listed 016 only. No other row was touched.
