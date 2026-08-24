/**
 * Result and stem endpoints of the Straticate backend
 * (`/api/v1/jobs/{job_id}/result` and `/api/v1/jobs/{job_id}/stems/{stem}`).
 *
 * A result exists only for a `completed` job: any other state answers `409
 * result_not_available` with the job's current `state` in the envelope's
 * `detail`, which is what lets the player say *why* there is nothing to play
 * ("still separating…" vs. "you cancelled this job") without a second error
 * code to branch on. A stem the result lists whose file is gone answers `404
 * stem_file_missing` — the job record outlived its files, so the honest
 * remedy is to run the separation again. See `docs/contracts/rest-api.md`.
 *
 * Every function rejects with an {@link ApiError} carrying the backend error
 * envelope's `code`, `message`, and `detail`.
 */

import { ApiError, API_BASE, errorBodyFromText, get } from './client'
import type { SeparationResult } from './types'

/** Path segment of the jobs collection, relative to the `/api/v1` base. */
const JOBS_PATH = '/jobs'

/**
 * Absolute URL of one stem's audio, ready to hand to `fetch`, an
 * `<audio src>`, or an `HTMLAudioElement`.
 *
 * Both the job ID and the stem name are percent-encoded: the backend
 * validates the decoded stem name against the result's stem list, so an
 * encoded separator comes back as a clean `404 stem_not_found` rather than
 * reaching for a file outside the job's stem directory.
 */
export function stemUrl(jobId: string, stemName: string): string {
  return `${API_BASE}${JOBS_PATH}/${encodeURIComponent(jobId)}/stems/${encodeURIComponent(stemName)}`
}

/**
 * Fetch the {@link SeparationResult} of a completed job
 * (`GET /api/v1/jobs/{job_id}/result`).
 *
 * The result's `stems` list is the authority on which stems exist — never a
 * hardcoded name or count.
 */
export function getSeparationResult(jobId: string): Promise<SeparationResult> {
  return get<SeparationResult>(
    `${JOBS_PATH}/${encodeURIComponent(jobId)}/result`,
  )
}

/**
 * Fetch one stem's audio as raw bytes, ready for
 * `AudioContext.decodeAudioData` (`GET /api/v1/jobs/{job_id}/stems/{stem}`).
 *
 * Failures are translated into the same {@link ApiError} envelope the JSON
 * helpers raise, so `stem_file_missing` is a code the caller can branch on
 * rather than an opaque HTTP status.
 */
export async function fetchStemAudio(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url)
  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new ApiError(
      response.status,
      errorBodyFromText(response.status, text),
    )
  }
  return await response.arrayBuffer()
}
