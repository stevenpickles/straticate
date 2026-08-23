# [009] Metadata display UI

Branch: `009-metadata-display`
Status: PR OPEN
Dependencies: 008
PR: #…

## Objective

After a successful upload the workspace shows the uploaded track's filename
and its probed metadata (duration, format, channels, sample rate, bit depth,
bit rate, size), so the user can confirm they picked the right file — and
discard it and pick another — before configuring separation.

## Scope

- `frontend/src/format.ts`: pure presentation helpers —
  `formatDuration`, `formatAudioFormat`, `formatChannels`,
  `formatSampleRate`, `formatBitDepth`, `formatBitRate`, `formatFileSize`.
- `frontend/src/components/AudioSummary.tsx`: renders an `AudioFile` — the
  filename as a heading, then a `<dl>` of metadata rows — plus a
  "Choose a different file" action that clears the upload, returns the
  workflow to `select`, and best-effort calls `deleteAudio(id)`.
- `frontend/src/state/appState.tsx`: `upload/reset` now also returns the
  phase to `select` (the upload slice is cleared as a whole).
- `frontend/src/components/Workspace.tsx`: the `configure` phase renders
  `AudioSummary` plus a clearly-marked placeholder region for the separation
  mode/quality chooser (feature 011).
- `frontend/src/index.css`: dark-minimal styling for the summary card,
  definition list, action button, and the options placeholder.

## Out of scope

- Separation mode/quality selection UI (011) — only a placeholder region.
- Job creation, progress, or result UI (016/017).
- Any backend code; `frontend/src/ws/` and `frontend/src/api/jobs.ts` (016).

## Expected modules/files

- `frontend/src/format.ts` (+ `format.test.ts`)
- `frontend/src/components/AudioSummary.tsx` (+ `AudioSummary.test.tsx`)
- `frontend/src/components/Workspace.tsx`
- `frontend/src/state/appState.tsx` (+ extended `appState.test.tsx`)
- `frontend/src/index.css`

## Acceptance criteria

- [x] The `configure` phase shows the uploaded filename prominently.
- [x] Duration renders as `m:ss`, or `h:mm:ss` from one hour up.
- [x] Format renders uppercased, appending the codec only when it differs
      meaningfully from the container.
- [x] Channels render as `Mono` / `Stereo` / `N channels`.
- [x] Sample rate renders as kHz with at most one decimal.
- [x] Bit depth renders as `N bit`, and the row is omitted when `null`.
- [x] Bit rate renders as `N kbps`, and the row is omitted when `null`.
- [x] Size renders in human units (binary; see notes).
- [x] "Choose a different file" clears the upload, returns to the `select`
      phase (drop zone re-rendered), and best-effort deletes the upload on
      the backend; a failed delete never blocks the reset.
- [x] A placeholder marks where the separation options will go (011).
- [x] Styling matches the existing dark minimal CSS.

## Required tests

- Table-driven unit tests for every formatter, including 0s, ≥ 1 h, null bit
  depth, mono/5.1, 48 kHz/96 kHz, and small/large sizes.
- `AudioSummary` renders every present field with the expected formatting
  from the shared `sampleAudioFile` fixture.
- The bit-depth row is omitted for a lossy file (null bit depth); the
  bit-rate row is omitted when the bit rate is null.
- "Choose a different file" (driven through `Workspace`, so the phase
  transition is real) returns to `select`, re-renders the `DropZone`, and
  calls `deleteAudio` with the file id.
- A rejected `deleteAudio` still resets the UI.
- Reducer test: `upload/reset` clears an uploaded file and returns the phase
  to `select`.

## Notes / decisions

- **Size units are binary (1024-based) with conventional labels**
  (`B`/`KB`/`MB`/`GB`/`TB`), matching Windows Explorer. The 44,771,328-byte
  sample FLAC therefore reads `42.7 MB`, not the decimal `44.8 MB`. This is
  documented in `format.ts` and applied consistently app-wide; any future
  size display should reuse `formatFileSize`.
- Rounding can carry a value into the next unit (`1048575` bytes would render
  as `1024 KB`); `formatFileSize` promotes it to `1 MB`.
- "Meaningfully different" codec: the codec is appended in parentheses unless
  it equals the container name, or it is a `pcm_*` codec inside a PCM-only
  container (`wav`, `w64`, `aiff`, …) where the bit-depth row already carries
  the information. The backend already reduces ffprobe's comma-separated
  `format_name` to its first entry, so an m4a file reads e.g. `MOV (AAC)`.
- `upload/reset` was reused rather than adding a new action, and now also
  resets the phase to `select`. Its existing callers (dismissing an upload
  error, aborting an in-flight upload) are only reachable while the phase is
  already `select`, so the added phase reset is a no-op for them.
- Backend cleanup on discard is best-effort: the reducer action is dispatched
  before `deleteAudio` is called, and a rejection is logged via `console.warn`
  only. Orphaned uploads are the backend's retention concern, not the UI's.
- The bit-rate row shows for lossless files too when the backend reports one
  (the sample FLAC reads `1411 kbps`); it is informative rather than harmful,
  and costs nothing when absent.
