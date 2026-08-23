import { useRef, useState, type DragEvent } from 'react'
import { startAudioUpload, type UploadHandle } from '../api/audio'
import { ApiError } from '../api/client'
import { useAppDispatch, useAppState } from '../state/appState'

/**
 * Advisory `accept` list for the file picker. Real validation happens on
 * the backend against the actual media contents — never the extension.
 */
const ACCEPT_EXTENSIONS = '.wav,.flac,.mp3,.aac,.m4a,.aiff,.aif,.ogg'

/** True when a drag carries at least one file (ignores text/link drags). */
function dragHasFiles(event: DragEvent<HTMLElement>): boolean {
  return Array.from(event.dataTransfer.types).includes('Files')
}

/**
 * The file-selection step of the workflow: a prominent drop zone with
 * drag-and-drop and a file picker. Uploads the chosen file to the backend
 * with progress, surfaces backend validation errors inline with a retry
 * affordance, and (via app state) advances the workflow to the configure
 * phase on success.
 *
 * Must be rendered under an `AppStateProvider`.
 */
export function DropZone() {
  const { upload } = useAppState()
  const dispatch = useAppDispatch()
  const [dragActive, setDragActive] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const handleRef = useRef<UploadHandle | null>(null)

  const beginUpload = (file: File) => {
    if (handleRef.current !== null) {
      return
    }
    dispatch({ type: 'upload/started' })
    const handle = startAudioUpload(file, (fraction) => {
      dispatch({ type: 'upload/progress', fraction })
    })
    handleRef.current = handle
    handle.promise
      .then((audioFile) => {
        dispatch({ type: 'upload/succeeded', file: audioFile })
      })
      .catch((error: unknown) => {
        if (error instanceof ApiError && error.code === 'upload_aborted') {
          dispatch({ type: 'upload/reset' })
        } else if (error instanceof ApiError) {
          dispatch({
            type: 'upload/failed',
            code: error.code,
            message: error.message,
          })
        } else {
          dispatch({
            type: 'upload/failed',
            code: 'unknown_error',
            message: 'The upload failed unexpectedly. Please try again.',
          })
        }
      })
      .finally(() => {
        handleRef.current = null
      })
  }

  const uploading = upload.status === 'uploading'

  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (uploading || !dragHasFiles(event)) {
      return
    }
    event.preventDefault()
    setDragActive(true)
  }

  const onDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (
      event.relatedTarget instanceof Node &&
      event.currentTarget.contains(event.relatedTarget)
    ) {
      return
    }
    setDragActive(false)
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (uploading || !dragHasFiles(event)) {
      return
    }
    event.preventDefault()
    setDragActive(false)
    // On multi-drop, only the first file is taken.
    const file = event.dataTransfer.files[0]
    if (file !== undefined) {
      beginUpload(file)
    }
  }

  const onPickerChange = (files: FileList | null) => {
    const file = files?.[0]
    if (file !== undefined) {
      beginUpload(file)
    }
    // Allow re-selecting the same file after an error.
    if (inputRef.current !== null) {
      inputRef.current.value = ''
    }
  }

  return (
    <div
      className={`drop-zone${dragActive ? ' drop-zone-active' : ''}`}
      role="region"
      aria-label="Audio file selection"
      onDragOver={onDragOver}
      onDragEnter={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {uploading ? (
        <UploadProgress
          fraction={upload.fraction}
          onCancel={() => handleRef.current?.abort()}
        />
      ) : (
        <>
          <p className="drop-zone-prompt">Drop a music file here</p>
          <p className="drop-zone-or">or</p>
          <button
            type="button"
            className="drop-zone-button"
            onClick={() => inputRef.current?.click()}
          >
            Choose a File
          </button>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT_EXTENSIONS}
            className="drop-zone-input"
            aria-hidden="true"
            tabIndex={-1}
            onChange={(event) => {
              onPickerChange(event.currentTarget.files)
            }}
          />
          {upload.status === 'error' && (
            <p className="drop-zone-error" role="alert">
              {upload.message}
            </p>
          )}
        </>
      )}
    </div>
  )
}

/** Props for {@link UploadProgress}. */
interface UploadProgressProps {
  /** Upload fraction in 0..1, or `null` when the length is not computable. */
  fraction: number | null
  /** Abort the in-flight upload. */
  onCancel: () => void
}

/** Determinate or indeterminate upload progress bar with a cancel affordance. */
function UploadProgress({ fraction, onCancel }: UploadProgressProps) {
  const percent = fraction === null ? null : Math.round(fraction * 100)
  return (
    <div className="upload-progress">
      <p className="drop-zone-prompt">
        {percent === null ? 'Uploading…' : `Uploading… ${String(percent)}%`}
      </p>
      <div
        className={`progress-track${percent === null ? ' progress-indeterminate' : ''}`}
        role="progressbar"
        aria-label="Upload progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
      >
        <div
          className="progress-fill"
          style={
            percent === null ? undefined : { width: `${String(percent)}%` }
          }
        />
      </div>
      <button type="button" className="drop-zone-cancel" onClick={onCancel}>
        Cancel
      </button>
    </div>
  )
}
