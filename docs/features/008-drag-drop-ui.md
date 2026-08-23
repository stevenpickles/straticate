# [008] Drag-drop + file picker + upload state UI

Branch: `008-drag-drop-ui`
Status: PR OPEN
Dependencies: 003, 005
PR: #…

## Objective

The file-selection step of the workflow exists: a prominent drop zone with
drag-and-drop and a file picker, upload with progress state, backend
validation errors surfaced inline, and the returned `AudioFile` stored in app
state, advancing the workflow to the configure phase.

## Scope

- `frontend/src/api/audio.ts`: `uploadAudio(file, onProgress?)` posting
  multipart `file` to `POST /api/v1/audio` via `XMLHttpRequest` (fetch has no
  upload progress) with the same error-envelope handling as `client.ts`
  (`ApiError`); `startAudioUpload` variant returning an abort handle;
  `deleteAudio(id)` using the client `del` helper (added; handles 204).
- `frontend/src/state/appState.tsx`: upload slice —
  `idle | uploading (fraction) | uploaded (AudioFile) | error (code, message)`
  with actions `upload/started`, `upload/progress`, `upload/succeeded`,
  `upload/failed`, `upload/reset`. On success the `AudioFile` is stored and
  the phase advances `select → configure`.
- `frontend/src/components/DropZone.tsx`: drop zone with dragover highlight,
  drop handling (ignores non-file drags, takes the first file on multi-drop),
  hidden `<input type="file">` behind a real button (native Enter/Space),
  advisory `accept` list (wav/flac/mp3/aac/m4a/aiff/ogg), determinate or
  indeterminate progress bar during upload, cancel via `XHR.abort()`, inline
  error message with retry.
- `frontend/src/components/Workspace.tsx`: renders the DropZone in the
  `select` phase; minimal uploaded-filename placeholder in `configure`.
- Dark-minimal styling consistent with the existing CSS variables.

## Out of scope

- Metadata display panel (009).
- Separation configuration UI (011).
- Job/WebSocket clients (016).
- Backend upload endpoint (006) — developed against the documented contract
  and generated types with mocked XHR/fetch.

## Expected modules/files

- `frontend/src/api/audio.ts` (+ `audio.test.ts`)
- `frontend/src/api/client.ts` (`del` helper, shared envelope parsing)
- `frontend/src/state/appState.tsx` (+ extended `appState.test.tsx`)
- `frontend/src/components/DropZone.tsx` (+ `DropZone.test.tsx`)
- `frontend/src/components/Workspace.tsx`
- `frontend/src/index.css`
- `frontend/src/test/mockXhr.ts`, `frontend/src/test/fixtures.ts`

## Acceptance criteria

- [x] Drop zone renders both affordances (drop prompt and picker button).
- [x] Selecting a file via the picker uploads it as multipart `file` to
      `POST /api/v1/audio`.
- [x] Dropping a file uploads it; non-file drags are ignored; the first file
      is taken on multi-drop.
- [x] Upload progress is reflected in state (determinate 0..1 fraction, or
      indeterminate when length is not computable).
- [x] On success the returned `AudioFile` is stored and the workflow advances
      to `configure` (minimal filename confirmation shown).
- [x] Backend envelope errors (e.g. `audio_too_large`,
      `audio_not_decodable`) render an inline human-readable message and the
      drop zone allows retry.
- [x] Keyboard accessible: real button semantics, focus, Enter/Space.

## Required tests

- Drop zone renders both affordances.
- Picker selection triggers upload; drag-drop triggers upload.
- Progress events update state (determinate and indeterminate).
- Success stores the AudioFile and advances phase to configure.
- Envelope error (413 `audio_too_large`) shows the message and allows retry.
- Non-file drag is ignored.
- Reducer unit tests for the upload slice.
- `uploadAudio`/`deleteAudio` unit tests against a mocked `XMLHttpRequest`
  and `fetch`.

## Notes / decisions

- `XMLHttpRequest` is used only for the upload (fetch exposes no upload
  progress). Envelope parsing is shared with the fetch client via
  `errorBodyFromText` so both paths produce identical `ApiError`s.
- Aborting an in-flight upload is supported (`startAudioUpload` returns an
  abort handle; the DropZone shows a Cancel button); an aborted upload
  resets to idle rather than showing an error. Job cancellation proper is a
  later feature.
- The `accept` list is advisory only — real validation is backend-side
  against the actual media contents (feature 006).
- Non-`select` phases do not regress on `upload/succeeded`; the phase only
  advances from `select` to `configure`.
