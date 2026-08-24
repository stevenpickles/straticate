/**
 * Stem export endpoint of the Straticate backend
 * (`GET /api/v1/jobs/{job_id}/export`, feature 022).
 *
 * The route transcodes a completed job's stems and answers with a download.
 * Two optional query parameters decide what comes back:
 *
 * - `format` — one of {@link ExportFormat}; `wav_pcm24` when omitted.
 * - `stems` — a comma-separated selection, validated against the job's
 *   `SeparationResult.stems`. **Omitting it is how you ask for every stem**;
 *   a present-but-empty `stems=` is a `validation_error` 422, which is why
 *   {@link exportUrl} drops the parameter for a full selection and refuses an
 *   empty one outright.
 *
 * The response shape follows the selection: exactly one stem is the audio
 * file itself, two or more (including the default) is a zip carrying one file
 * per stem plus `separation.json`. Both are
 * `Content-Disposition: attachment`, so a plain `<a href>` would download
 * them — {@link downloadExport} goes through `fetch` instead so a failure can
 * be rendered as the backend's JSON error envelope rather than replacing the
 * app with an error page.
 *
 * See `docs/contracts/rest-api.md` ("Results, stems, export").
 */

import { ApiError, API_BASE, errorBodyFromText } from './client'
import type { components } from './generated/api'

/**
 * Audio format an export is encoded to.
 *
 * Aliased from the generated OpenAPI types here rather than in `api/types.ts`
 * so that no module outside `src/api` reaches into the generated file, and no
 * hand-written copy of the backend's enum exists anywhere.
 */
export type ExportFormat = components['schemas']['ExportFormat']

/** How one {@link ExportFormat} is offered to the user. */
export interface ExportFormatOption {
  /** Contract value sent as the `format` query parameter. */
  readonly id: ExportFormat
  /** Short human-readable name for the picker. */
  readonly label: string
  /**
   * What the user actually gets. The separator writes 16-bit PCM WAV, so
   * `wav_pcm24` and `wav_float32` change the encoding and add **no**
   * information — the contract says so and so does the UI.
   */
  readonly note: string
  /** File extension a single-stem export of this format carries. */
  readonly extension: string
}

/**
 * Every export format, keyed by the generated {@link ExportFormat} union.
 *
 * The union is a *type*, so it cannot be enumerated at runtime; keying a
 * `Record` by it is what makes this table exhaustive in both directions.
 * When the backend gains (or drops) a format, regenerating `api.d.ts` turns
 * this object into a type error until it is updated, and the picker, the
 * default filename and the format notes all follow from the one edit.
 * Declaration order is the order the picker offers them in.
 */
const EXPORT_FORMAT_TABLE: Record<
  ExportFormat,
  Omit<ExportFormatOption, 'id'>
> = {
  wav_pcm24: {
    label: 'WAV · 24-bit',
    note: 'Re-encoded from the 16-bit stems; adds no detail.',
    extension: 'wav',
  },
  wav_float32: {
    label: 'WAV · 32-bit float',
    note: 'Re-encoded from the 16-bit stems; adds no detail.',
    extension: 'wav',
  },
  flac: {
    label: 'FLAC',
    note: 'Lossless compression of the same 16-bit audio.',
    extension: 'flac',
  },
}

/** Every export format the backend offers, in picker order. */
export const EXPORT_FORMAT_OPTIONS: readonly ExportFormatOption[] =
  Object.entries(EXPORT_FORMAT_TABLE).map(([id, option]) => ({
    id: id as ExportFormat,
    ...option,
  }))

/** The `format` the backend applies when the parameter is omitted. */
export const DEFAULT_EXPORT_FORMAT: ExportFormat = 'wav_pcm24'

/** Look up one format's presentation metadata. */
export function exportFormatOption(format: ExportFormat): ExportFormatOption {
  return { id: format, ...EXPORT_FORMAT_TABLE[format] }
}

/** What to export, and out of what. */
export interface ExportSelection {
  /** Format to encode the stems in. */
  readonly format: ExportFormat
  /** Stem names the user picked, in any order. Must not be empty. */
  readonly stems: readonly string[]
  /**
   * Every stem name the job's `SeparationResult` lists. The selection is
   * compared against it: covering all of them omits the `stems` parameter
   * entirely, which is the contract's way of saying "everything".
   */
  readonly availableStems: readonly string[]
}

/** Outcome of a completed download. */
export interface ExportDownload {
  /** Name the file was saved under (the server's, when it offered one). */
  readonly filename: string
  /** Size of the downloaded body in bytes. */
  readonly sizeBytes: number
}

/** Path segment of the jobs collection, relative to the `/api/v1` base. */
const JOBS_PATH = '/jobs'

/**
 * The selection, deduplicated and put into the result's own order, so the
 * same set of stems always produces the same URL however the user clicked
 * them — which is also what keeps the backend's export cache warm.
 *
 * A selected name the result does not list is kept (at the end) rather than
 * dropped: the backend answers `stem_not_found`, which is the honest outcome,
 * where silently omitting it would export something the user did not ask for.
 *
 * @throws Error when nothing is selected: `stems=` is a 422, so the UI must
 * disable export instead of asking for "no stems".
 */
function orderedSelection(selection: ExportSelection): string[] {
  const selected = new Set(selection.stems)
  if (selected.size === 0) {
    throw new Error('Select at least one stem to export.')
  }
  const available = new Set(selection.availableStems)
  return [
    ...selection.availableStems.filter((name) => selected.has(name)),
    ...[...selected].filter((name) => !available.has(name)),
  ]
}

/**
 * The `stems` query value for a selection, or `null` when the parameter must
 * be omitted because every stem the result lists was selected.
 */
function stemsParameter(selection: ExportSelection): string | null {
  const names = orderedSelection(selection)
  const available = new Set(selection.availableStems)
  const coversResult =
    names.length === available.size &&
    names.every((name) => available.has(name))
  if (coversResult) {
    return null
  }
  return names.map((name) => encodeURIComponent(name)).join(',')
}

/**
 * Build the download URL for a selection
 * (`GET /api/v1/jobs/{job_id}/export?format=…&stems=…`).
 *
 * The job ID and every stem name are percent-encoded; the separators between
 * stem names are not, so the value reads as the contract writes it
 * (`stems=vocals,drums`).
 *
 * @throws Error when the selection is empty (see {@link stemsParameter}).
 */
export function exportUrl(jobId: string, selection: ExportSelection): string {
  const stems = stemsParameter(selection)
  const query = `format=${encodeURIComponent(selection.format)}${
    stems === null ? '' : `&stems=${stems}`
  }`
  return `${API_BASE}${JOBS_PATH}/${encodeURIComponent(jobId)}/export?${query}`
}

/** Strip anything that could make a server-offered filename a path. */
function sanitizeFilename(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? ''
  return base.trim()
}

/**
 * The filename a `Content-Disposition` header offers, or `null` when it
 * offers none. RFC 5987's `filename*` wins over a plain `filename`, which is
 * what a non-ASCII stem name would arrive as.
 */
export function filenameFromContentDisposition(
  header: string | null,
): string | null {
  if (header === null) {
    return null
  }
  const extended = /filename\*\s*=\s*[^']*'[^']*'([^;]+)/i.exec(header)
  if (extended?.[1] !== undefined) {
    try {
      const decoded = sanitizeFilename(decodeURIComponent(extended[1].trim()))
      if (decoded !== '') {
        return decoded
      }
    } catch {
      // Malformed percent-encoding; fall through to the plain parameter.
    }
  }
  const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(header)
  const raw = plain?.[2] ?? plain?.[1]
  if (raw === undefined) {
    return null
  }
  const name = sanitizeFilename(raw)
  return name === '' ? null : name
}

/**
 * Name to save under when the server offered no `Content-Disposition`,
 * mirroring the contract's own naming: one stem is a bare audio file, two or
 * more are a zip.
 */
function fallbackFilename(jobId: string, selection: ExportSelection): string {
  const names = orderedSelection(selection)
  if (names.length === 1) {
    const extension = exportFormatOption(selection.format).extension
    return `${jobId}-${selection.format}-${names[0] ?? ''}.${extension}`
  }
  return `${jobId}-${selection.format}.zip`
}

/**
 * Hand a downloaded blob to the browser under `filename`.
 *
 * The object URL is revoked in a `finally`, so a click that throws does not
 * leak the blob for the lifetime of the document.
 */
function saveBlob(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    anchor.rel = 'noopener'
    anchor.style.display = 'none'
    document.body.append(anchor)
    try {
      anchor.click()
    } finally {
      anchor.remove()
    }
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}

/**
 * Download an export and save it under the server-offered filename.
 *
 * Rejects with an {@link ApiError} carrying the backend envelope's `code`,
 * `message` and `detail` — `job_not_found`, `result_not_available` (whose
 * `detail.state` says *why* there is no result), `stem_not_found`,
 * `stem_file_missing`, `export_failed` and `validation_error` — so the caller
 * can render each one rather than dropping the user on a raw error page.
 *
 * The first download of a given format/selection runs FFmpeg and can take
 * seconds; repeats are served from the backend's export cache.
 */
export async function downloadExport(
  jobId: string,
  selection: ExportSelection,
): Promise<ExportDownload> {
  const url = exportUrl(jobId, selection)
  const response = await fetch(url)
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new ApiError(
      response.status,
      errorBodyFromText(response.status, text),
    )
  }
  const blob = await response.blob()
  const filename =
    filenameFromContentDisposition(
      response.headers.get('Content-Disposition'),
    ) ?? fallbackFilename(jobId, selection)
  saveBlob(blob, filename)
  return { filename, sizeBytes: blob.size }
}
