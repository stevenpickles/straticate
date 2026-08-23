/**
 * Audio endpoints of the Straticate backend.
 *
 * Uploads use `XMLHttpRequest` rather than `fetch` because only XHR
 * exposes upload progress events; errors are normalized into the same
 * {@link ApiError} envelope handling as the fetch-based client helpers.
 */

import { API_BASE, ApiError, del, errorBodyFromText } from './client'
import type { AudioFile } from './types'

/**
 * Reports upload progress as a fraction in `[0, 1]`, or `null` when the
 * total length is not computable (indeterminate progress).
 */
export type UploadProgressCallback = (fraction: number | null) => void

/** Handle returned by {@link uploadAudio} to abort an in-flight upload. */
export interface UploadHandle {
  /** Resolves with the registered {@link AudioFile} on success. */
  readonly promise: Promise<AudioFile>
  /** Abort the in-flight upload; the promise rejects with an `upload_aborted` {@link ApiError}. */
  readonly abort: () => void
}

/**
 * Upload an audio file to the backend (`POST /api/v1/audio`, multipart
 * `file` field) with progress reporting, returning a handle whose
 * `promise` resolves to the registered {@link AudioFile}.
 *
 * Rejects with {@link ApiError}:
 * - backend validation errors carry the envelope's `code`/`message`
 *   (e.g. `audio_too_large`, `audio_not_decodable`);
 * - network failures reject with code `network_error` (status 0);
 * - aborted uploads reject with code `upload_aborted` (status 0).
 */
export function startAudioUpload(
  file: File,
  onProgress?: UploadProgressCallback,
): UploadHandle {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<AudioFile>((resolve, reject) => {
    xhr.open('POST', `${API_BASE}/audio`)
    xhr.responseType = 'text'

    xhr.upload.addEventListener('progress', (event: ProgressEvent) => {
      if (!onProgress) {
        return
      }
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(1, event.loaded / event.total))
      } else {
        onProgress(null)
      }
    })

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as AudioFile)
        } catch {
          reject(
            new ApiError(xhr.status, {
              code: 'invalid_response',
              message: 'The backend returned a malformed upload response.',
            }),
          )
        }
      } else {
        reject(
          new ApiError(
            xhr.status,
            errorBodyFromText(xhr.status, xhr.responseText),
          ),
        )
      }
    })

    xhr.addEventListener('error', () => {
      reject(
        new ApiError(0, {
          code: 'network_error',
          message:
            'The upload failed because the backend could not be reached.',
        }),
      )
    })

    xhr.addEventListener('abort', () => {
      reject(
        new ApiError(0, {
          code: 'upload_aborted',
          message: 'The upload was cancelled.',
        }),
      )
    })

    const body = new FormData()
    body.append('file', file)
    xhr.send(body)
  })

  return {
    promise,
    abort: () => {
      xhr.abort()
    },
  }
}

/**
 * Upload an audio file and resolve with the registered {@link AudioFile}.
 * Convenience wrapper over {@link startAudioUpload} without abort support.
 */
export function uploadAudio(
  file: File,
  onProgress?: UploadProgressCallback,
): Promise<AudioFile> {
  return startAudioUpload(file, onProgress).promise
}

/** Delete an uploaded audio file and its derived data (`DELETE /api/v1/audio/{id}`). */
export function deleteAudio(id: string): Promise<void> {
  return del(`/audio/${encodeURIComponent(id)}`)
}
