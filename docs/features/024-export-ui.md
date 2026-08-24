# [024] Export UI

Branch: `024-export-ui`
Status: PR OPEN
Dependencies: 022, 023
PR: #27

## Objective

A completed job's stems can be taken out of Straticate. The user picks which
stems to export and in which format, triggers the download, and sees what
happened — the offered filename on success, a distinct and actionable message
for every error code feature 022 documents. This closes the last gap in
milestone **M1**: upload → configure → separate → inspect → export now exists
end to end in a browser.

## Scope

- `frontend/src/api/types.ts` — one line: the `ExportFormat` alias, alongside
  every other contract type. This is the only file outside 024's own modules
  that changed.
- `frontend/src/api/export.ts` — the typed client for
  `GET /api/v1/jobs/{job_id}/export`:
  - `exportUrl()` builds the URL. **A full selection omits the `stems`
    parameter entirely** (that is how the contract says "everything";
    `stems=` is a 422) and an empty one throws rather than being sent. The
    selection is deduplicated and emitted in the result's own order, so the
    same set of stems always produces the same URL however it was clicked —
    which also keeps the backend's export cache warm.
  - `downloadExport()` fetches, checks the status, and turns a failure into
    the project's `ApiError` (via `errorBodyFromText`) so the JSON envelope can
    be rendered instead of the browser navigating to an error page. It saves
    the blob through a temporary `<a download>` and revokes the object URL in
    a `finally`.
  - `filenameFromContentDisposition()` reads the server-offered filename
    (RFC 5987 `filename*` preferred, any path stripped), falling back to the
    contract's own naming when the header is absent.
  - The format table, keyed by the `ExportFormat` union.
- `frontend/src/components/ExportPanel.tsx` (+ `.css`) — replaces 023's
  placeholder: a checkbox per stem of `job.result.stems` (all ticked by
  default), a format `<select>`, an Export button with a pending state, a
  success line naming the downloaded file, and a rendered explanation for each
  documented error code.
- `docs/features/024-export-ui.md`, `ROADMAP.md` (024's ledger row).

## Out of scope

- Anything under `backend/` and `frontend/src/api/generated/api.d.ts` — 022
  shipped the endpoint and regenerated the types; nothing here needed either.
- `frontend/src/components/{StemPlayer,Workspace,SeparationProgress,
  TelemetryPanel,SeparationOptions,DropZone,AudioSummary,Header}.tsx`,
  `frontend/src/state/**`, `frontend/src/audio/**`, `frontend/src/ws/**`,
  `frontend/src/api/{client,jobs,stems}.ts`, `frontend/src/format.ts`,
  `frontend/src/index.css` — read, never written (`api/types.ts` gained the
  one-line `ExportFormat` alias and nothing else). `Workspace.tsx` already
  mounts `<ExportPanel />` in the `inspect` phase, so no edit was needed there.
- Export presets, download history, resumable or background downloads,
  exporting from a phase other than `inspect`, and any retention or cleanup of
  server-side export artifacts (022 records that as unclaimed).

## Expected modules/files

- `frontend/src/api/export.ts` · `export.test.ts`
- `frontend/src/api/types.ts` (the `ExportFormat` alias only)
- `frontend/src/components/ExportPanel.tsx` · `.css` · `.test.tsx`
- `docs/features/024-export-ui.md` · `ROADMAP.md`

## Acceptance criteria

- [x] Stem checkboxes come from `result.stems`; a two-stem and a four-stem job
      both render correctly, and no stem name or count is hardcoded anywhere in
      the diff.
- [x] Format options are derived from the generated `ExportFormat` union, not a
      hand-written list.
- [x] All stems selected → the request omits `stems` entirely. A subset → a
      correctly joined `stems` parameter. None → export disabled, and an empty
      `stems=` can never be sent (the URL builder throws).
- [x] The download is triggered with the server-offered filename, and the
      object URL is always revoked.
- [x] Each documented error code renders a distinct, actionable message, and
      the button is usable again afterwards.
- [x] `result_not_available` distinguishes "still running" from `cancelled`
      and `failed` via `detail.state`.
- [x] No double submit; the button is disabled and `aria-busy` while in flight.
- [x] The UI states that 24-bit and float32 add no information today.
- [x] `index.css` untouched; `format:check` · `lint` · `typecheck` · `test` ·
      `build` all green.

## Required tests

`frontend/src/api/export.test.ts` (33) and
`frontend/src/components/ExportPanel.test.tsx` (36), Vitest + Testing Library,
queried by accessible role and name, awaited with `findBy*`/`waitFor` and never
a timer:

- URL building — everything selected (no `stems`), a subset, a single stem, a
  reordered full selection, a duplicated name, an unknown name, percent-encoded
  job IDs and stem names, and an empty selection that throws.
- `Content-Disposition` parsing — quoted, unquoted, RFC 5987, a path-bearing
  name, and the absent/empty cases.
- Download — the exact URL fetched, the filename used, both fallback names, the
  reported size, `revokeObjectURL` called on success *and* when the save click
  throws, the revoke landing in a *later task* than the click, no object URL
  created for a failed request, and a typed `ApiError` for each documented code
  plus a non-JSON body.
- Panel — two-stem and four-stem rendering, arbitrary stem names, deselecting
  everything disabling export, the "single file" vs "zip + separation.json"
  line, the format list and the honesty note, the exact request URL for a
  given selection and format, a double click downloading once, in-flight
  disabling, every error code's message with the button re-enabled, a network
  failure, a retry after a failure, the success/error line clearing when the
  selection or format changes (but not mid-download), tracking a different job
  resetting the panel *and staying reset through the next toggle*, the Export
  button still working when the job changes mid-download, and the no-job /
  no-result states.

## Notes / decisions

### The format list is derived from the generated union, not written out

`ExportFormat` is a TypeScript union, so it has no runtime values to iterate.
`api/export.ts` keys a `Record<ExportFormat, …>` table by it, which is
exhaustive in both directions: a format the backend adds or removes turns that
object into a type error until it is handled, and the picker, the format note
and the fallback filename's extension all follow from that one edit. No list of
format strings exists anywhere else, and the component never sees one.

### `ExportFormat` is aliased in `api/types.ts`

DEVELOPMENT.md's rule holds: `api/types.ts` is the only module that imports
from `./generated/api`, and everything else — `api/export.ts` and
`ExportPanel.tsx` alike — imports the alias from there.

### `fetch` + blob rather than a plain link

022 notes that the simplest correct UI is an `<a href>` straight at the URL,
since the response is `Content-Disposition: attachment`. That was rejected here
because it makes failures unrenderable: a 409 or a 500 would either navigate
away or be silently ignored by the browser, and the panel could not tell the
user that the job was cancelled or that the transcode failed. Fetching lets the
error envelope be read and explained, at the cost of buffering the export in
memory (see the limitations).

### The object URL is revoked a task later, not in the click's task

Revoking immediately after `anchor.click()` is what Chrome tolerates and what
Safari and older Firefox can read as "this blob is gone" before the download
has been handed to the download manager — the promise resolves, the success
line appears, and no file is written. The revoke is therefore scheduled with a
zero-delay `setTimeout` (MDN's and FileSaver.js's approach; the delay length is
irrelevant, landing in a later task is the point), still from a `finally` so a
click that throws cannot leak the blob either.

### The panel state is keyed by job ID

The selection is stored as the set of *deselected* stems, so "everything" is
the default for any stem list, of any length, with no seeding step — and stem
names never appear in the component. Both it and the download outcome are held
in one state object stamped with the job it belongs to, and *every* read of it
— the render and every updater, through the shared `updateState` — falls back
to the defaults when the stamp does not match the tracked job. Doing it in one
place is deliberate: an updater that spread the stored state instead would
resurrect the previous job's deselection on the first toggle after a switch and
silently export a stem short. That avoids a reset effect (and the cascading
render React's lint rules rightly object to), and a download that settles after
the user has moved on cannot write its result into the new job's panel.

The in-flight guard is stamped the same way: the ref holds the *job* a download
was started for, not a boolean, so a download abandoned by a job change cannot
leave the new job's Export button enabled-but-inert, and its `finally` releases
only its own claim.

### A changed selection or format clears the last outcome

"Downloaded X." beside a different selection is a lie, and `stem_not_found`'s
message asks the user to change their selection — which must not leave the same
error on screen once they do. Both controls therefore reset the outcome to
idle. A download still in flight is exempt: its pending line is still true and
its button is still disabled.

### Selection order and the export cache

Names are always emitted in the result's order, so `bass,vocals` and
`vocals,bass` produce the same URL. 022's cache is keyed by the sorted stem
list and is order-insensitive anyway, but sending a stable URL means the
browser's own cache and any proxy see one resource too.

### `export_failed`'s reason is never shown

022 makes `detail.reason` (`transcode_failed` / `filesystem_error`) a
classification, not a message — FFmpeg's stderr names server paths and stays in
the log. The panel says the export could not be built and offers a retry; the
reason is deliberately absent from the UI.

### The honesty note is in the UI, not just the docs

The separator writes 16-bit PCM WAV, so `wav_pcm24` and `wav_float32` change
the encoding and add nothing. Each format carries a note saying so, and the
panel repeats the point below the picker. This stops being true when a real
separator (026) produces higher-precision output, at which point the notes in
`api/export.ts` are the one place to change.

## Milestone M1

024 is the last feature of **M1 — Fake-separator end-to-end**. The whole
workflow now exists in a browser: drop a file in, watch it upload and its
metadata appear, choose a catalog-derived separation mode and quality, start
the job, follow real chunk-grained progress and telemetry over the WebSocket
(and cancel it if you want), then play the stems in sync with solo and mute,
and finally export the ones you want in the format you want.

Known to be **outside** M1 as delivered:

- **The stems are fake.** 014's separator produces placeholder audio; a real
  model arrives with 025/026. Everything above is therefore verified against
  the fake separator only.
- **The E2E (Playwright) suite** DEVELOPMENT.md schedules "around M1" is not
  part of this feature; the coverage here is unit and component tests.
- **Nothing cleans up.** Uploads, job directories, stems and now export
  artifacts accumulate under the data directory forever (021's and 022's notes)
  — no feature owns retention yet.
- **Job records are in-memory.** A backend restart loses every job, so a page
  reopened afterwards gets `job_not_found`; the panel explains it and points at
  re-running the separation.
- **One job at a time in the UI.** The store tracks a single job; there is no
  job history or library view.

## Known limitations

- **The whole export is buffered in memory.** `fetch` + blob is what makes the
  error envelope renderable, but it means a large multi-stem export is held in
  the tab before it is written to disk. For the fake separator's short fixtures
  this is irrelevant; for hour-long real stems a future feature may want to
  switch to a plain navigation once it can show errors another way (for example
  by pre-flighting the request).
- **No download progress.** The button shows a pending state, not a byte count:
  the backend sends no length until the transcode finishes, and the first
  request for a given format/selection is doing FFmpeg work rather than
  transferring. Repeats are cache hits and return immediately.
- **A download cannot be cancelled.** There is no abort control on the button;
  022 also notes that a cancelled request does not stop the server-side build.
- **`stem_not_found` does not refresh the selection.** The message tells the
  user their selection is stale and asks them to change it; it does not read
  `detail.available_stems` and re-render the checkboxes from it. The panel only
  ever offers stems the tracked result lists, so this needs a job whose result
  changed underneath it — which cannot happen while stems are immutable.
- **The panel reads `job.result` rather than re-fetching it.** `StemPlayer`
  fetches `GET /jobs/{id}/result` (it needs the 409 `detail.state` to say why
  there is nothing to play); the export panel is mounted alongside it in the
  same phase and uses the tracked job's result instead of issuing a second
  request. If it is ever mounted somewhere the job record is not already
  complete, it will need `getSeparationResult()`.
- **A download started for a job the user then leaves still saves.** The file
  is what they asked for, so it is written; only the confirmation is
  suppressed, because the panel now belongs to a different job. There is no
  "your earlier export finished" notice anywhere.
- **No export presets or history.** Each export is configured from scratch, and
  nothing remembers what was downloaded before.
