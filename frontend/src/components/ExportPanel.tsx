import { useCallback, useRef, useState, type ReactNode } from 'react'
import { ApiError } from '../api/client'
import {
  DEFAULT_EXPORT_FORMAT,
  EXPORT_FORMAT_OPTIONS,
  downloadExport,
  exportFormatOption,
  type ExportFormat,
} from '../api/export'
import { useJobState } from '../state/jobState'
import './ExportPanel.css'

/** Envelope-shaped view of any rejection. */
interface ErrorInfo {
  readonly code: string
  readonly message: string
  readonly detail: unknown
}

/** Fallback for a rejection that is not an {@link ApiError}. */
const UNKNOWN_ERROR: ErrorInfo = {
  code: 'unknown_error',
  message: 'Something went wrong. Please try again.',
  detail: undefined,
}

/** State of the download the user asked for. */
type DownloadState =
  | { readonly status: 'idle' }
  | { readonly status: 'downloading' }
  | { readonly status: 'done'; readonly filename: string }
  | { readonly status: 'error'; readonly error: ErrorInfo }

/** Everything the panel remembers, and which job it remembers it for. */
interface PanelState {
  /** Job the selection and the download outcome below belong to. */
  readonly jobId: string | null
  /**
   * Stems the user has *unticked*. Storing the deselection rather than the
   * selection is what makes "everything" the default for any stem list,
   * whatever it contains and however long it is.
   */
  readonly excluded: ReadonlySet<string>
  /** Outcome of the download the user last asked for. */
  readonly download: DownloadState
}

const initialPanelState: PanelState = {
  jobId: null,
  excluded: new Set<string>(),
  download: { status: 'idle' },
}

/** Envelope-shaped `{code, message, detail}` for any rejection reason. */
function errorInfo(reason: unknown): ErrorInfo {
  if (reason instanceof ApiError) {
    return { code: reason.code, message: reason.message, detail: reason.detail }
  }
  return UNKNOWN_ERROR
}

/**
 * The job state a `result_not_available` envelope carries in its `detail`,
 * or `null` when the backend sent none.
 */
function detailState(detail: unknown): string | null {
  if (typeof detail !== 'object' || detail === null || !('state' in detail)) {
    return null
  }
  const state: unknown = (detail as { state: unknown }).state
  return typeof state === 'string' ? state : null
}

/** DOM id of the checkbox for the stem at `index` in the result. */
function stemOptionId(index: number): string {
  return `export-stem-${String(index)}`
}

/** Humanize a snake_case contract identifier for use mid-sentence. */
function humanizeInline(identifier: string): string {
  return identifier.replaceAll('_', ' ').trim()
}

/**
 * Turn a backend error into something worth acting on, using the codes
 * feature 022 documents for this route.
 *
 * `result_not_available` is one code for three situations — still running,
 * cancelled, failed — told apart by the `state` its `detail` carries.
 * `export_failed`'s `detail.reason` is a coarse classification
 * (`transcode_failed` / `filesystem_error`), never a message, so it is
 * deliberately not surfaced: the detail is in the backend log.
 *
 * (The phrasing of the shared codes is lifted from 023's local `explainError`
 * rather than imported from `StemPlayer.tsx`, as that feature's notes ask.)
 */
function explainExportError(error: ErrorInfo): string {
  switch (error.code) {
    case 'job_not_found':
      return 'The backend no longer knows about this job, so there is nothing left to export. Run the separation again.'
    case 'result_not_available': {
      const state = detailState(error.detail)
      if (state === 'cancelled') {
        return 'This separation was cancelled, so there are no stems to export.'
      }
      if (state === 'failed') {
        return 'This separation failed, so there are no stems to export.'
      }
      return state === null
        ? 'The stems are not ready yet. Try again once the separation finishes.'
        : `The stems are not ready yet — this job is ${humanizeInline(state)}. Try again once it finishes.`
    }
    case 'stem_not_found':
      return 'One of the selected stems is not part of this job any more. Change the selection and try again.'
    case 'stem_file_missing':
      return 'The audio for this job is gone from disk. Run the separation again to recreate the stems.'
    case 'export_failed':
      return 'The export could not be built. Try again — if it keeps failing, the reason is in the backend log.'
    case 'validation_error':
      return 'Straticate asked for an export the backend rejected. This is a bug; please report it.'
    default:
      return error.message
  }
}

/**
 * The `export` step of the workflow: choose stems and a format, and download
 * them.
 *
 * Every checkbox comes from the tracked job's `SeparationResult.stems`, so a
 * two-stem and a four-stem job render through the same code and no stem name
 * or count appears here (AGENTS.md principle 6). The format options come from
 * the generated `ExportFormat` union by way of `api/export.ts`, so a format
 * the backend gains reaches the picker without a hand-written list.
 *
 * Selecting every stem sends no `stems` parameter at all — that is the
 * contract's way of asking for everything, and `stems=` would be a 422.
 * Selecting none disables the button rather than sending one.
 *
 * Must be rendered under a `JobStateProvider`.
 */
export function ExportPanel() {
  const { job } = useJobState()
  const jobId = job?.id ?? null
  const stems = job?.result?.stems ?? null

  const [format, setFormat] = useState<ExportFormat>(DEFAULT_EXPORT_FORMAT)
  const [stored, setStored] = useState<PanelState>(initialPanelState)
  const downloadingRef = useRef(false)

  // The panel state remembers which job it belongs to, so a different job is
  // back at the defaults — every stem selected, no stale success or failure —
  // without an effect that resets state after the fact.
  const state =
    stored.jobId === jobId ? stored : { ...initialPanelState, jobId }
  const { excluded, download } = state

  const toggleStem = useCallback(
    (name: string) => {
      setStored((current) => {
        const base = current.jobId === jobId ? current : { ...current, jobId }
        const next = new Set(base.excluded)
        if (!next.delete(name)) {
          next.add(name)
        }
        return { ...base, jobId, excluded: next }
      })
    },
    [jobId],
  )

  const stemNames = stems?.map((stem) => stem.name) ?? []
  const selected = stemNames.filter((name) => !excluded.has(name))
  const downloading = download.status === 'downloading'
  const canExport = jobId !== null && selected.length > 0 && !downloading

  const startDownload = () => {
    // The ref, not `download.status`, is what makes a double click a single
    // request: it flips synchronously, before React has re-rendered.
    if (downloadingRef.current || jobId === null || selected.length === 0) {
      return
    }
    downloadingRef.current = true
    // A settled download only speaks for the job it was started for: the user
    // may have moved on to another one while it was in flight.
    const settle = (result: DownloadState) => {
      setStored((current) =>
        current.jobId === jobId ? { ...current, download: result } : current,
      )
    }
    setStored({ ...state, jobId, download: { status: 'downloading' } })
    downloadExport(jobId, {
      format,
      stems: selected,
      availableStems: stemNames,
    })
      .then((result) => {
        settle({ status: 'done', filename: result.filename })
      })
      .catch((reason: unknown) => {
        settle({ status: 'error', error: errorInfo(reason) })
      })
      .finally(() => {
        downloadingRef.current = false
      })
  }

  let body: ReactNode
  if (jobId === null) {
    body = <p className="workspace-hint">No separation job is being tracked.</p>
  } else if (stems === null || stems.length === 0) {
    body = (
      <p className="workspace-hint">This job has no result to export yet.</p>
    )
  } else {
    const selectedFormat = exportFormatOption(format)
    const single = selected.length === 1
    body = (
      <>
        <fieldset className="export-panel-group">
          <legend className="export-panel-legend">Stems</legend>
          {stems.map((stem, index) => (
            <div className="export-panel-option" key={stem.name}>
              <input
                type="checkbox"
                // Positional id: a stem name is not guaranteed to be a legal
                // DOM id (whitespace is not), and the label only has to reach
                // the input next to it.
                id={stemOptionId(index)}
                checked={!excluded.has(stem.name)}
                onChange={() => {
                  toggleStem(stem.name)
                }}
              />
              <label
                className="export-panel-option-label"
                htmlFor={stemOptionId(index)}
              >
                {stem.name}
              </label>
            </div>
          ))}
        </fieldset>

        <div className="export-panel-format">
          <label className="export-panel-label" htmlFor="export-format">
            Format
          </label>
          <select
            id="export-format"
            className="export-panel-select"
            value={format}
            aria-describedby="export-format-note"
            onChange={(event) => {
              setFormat(event.target.value as ExportFormat)
            }}
          >
            {EXPORT_FORMAT_OPTIONS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <p className="export-panel-note" id="export-format-note">
          {selectedFormat.note} The separator writes 16-bit audio today, so
          24-bit and 32-bit float exports change the encoding without adding
          information.
        </p>

        <p className="export-panel-note">
          {selected.length === 0
            ? 'Select at least one stem to export.'
            : single
              ? `You will get a single .${selectedFormat.extension} file.`
              : `You will get a .zip with ${String(selected.length)} audio files and separation.json.`}
        </p>

        <button
          type="button"
          className="export-panel-download"
          disabled={!canExport}
          aria-busy={downloading}
          onClick={startDownload}
        >
          Export
        </button>

        {downloading && (
          <p className="export-panel-status" role="status">
            Preparing your download…
          </p>
        )}

        {download.status === 'done' && (
          <p className="export-panel-status" role="status">
            Downloaded {download.filename}.
          </p>
        )}

        {download.status === 'error' && (
          <p className="export-panel-error" role="alert">
            {explainExportError(download.error)}
          </p>
        )}
      </>
    )
  }

  return (
    <section className="export-panel" aria-label="Export">
      <h2 className="export-panel-title">Export</h2>
      {body}
    </section>
  )
}
