/**
 * Job endpoints of the Straticate backend (`/api/v1/jobs`).
 *
 * Jobs are asynchronous: {@link createJob} returns as soon as the job is
 * queued, and progress arrives over the WebSocket (see `src/ws/client.ts`).
 * REST stays the source of truth for reconnect/refresh — use
 * {@link getJob}/{@link listJobs} after a reconnect, then apply events.
 *
 * Every function rejects with an {@link ApiError} carrying the backend
 * error envelope's `code`, `message`, and `detail`.
 */

import { get, post } from './client'
import type { Job, SeparationConfiguration } from './types'

/** Path segment of the jobs collection, relative to the `/api/v1` base. */
const JOBS_PATH = '/jobs'

function jobPath(jobId: string, suffix = ''): string {
  return `${JOBS_PATH}/${encodeURIComponent(jobId)}${suffix}`
}

/**
 * Create a separation job (`POST /api/v1/jobs`).
 *
 * Returns immediately with the queued {@link Job}; the separation itself
 * runs in the background and reports over the WebSocket.
 */
export function createJob(
  configuration: SeparationConfiguration,
): Promise<Job> {
  return post<Job>(JOBS_PATH, configuration)
}

/** List every known job, newest-first as ordered by the backend (`GET /api/v1/jobs`). */
export function listJobs(): Promise<Job[]> {
  return get<Job[]>(JOBS_PATH)
}

/**
 * Fetch a single job by ULID (`GET /api/v1/jobs/{job_id}`).
 *
 * This is the authoritative view of a job: prefer it over replaying events
 * when the client (re)connects or the page reloads.
 */
export function getJob(jobId: string): Promise<Job> {
  return get<Job>(jobPath(jobId))
}

/**
 * Request cooperative cancellation of a job
 * (`POST /api/v1/jobs/{job_id}/cancel`).
 *
 * Cancellation is a request, not an immediate stop: the returned {@link Job}
 * may still be in a processing state, and the authoritative transition to
 * `cancelled` arrives as a `job_cancelled` WebSocket event.
 */
export function cancelJob(jobId: string): Promise<Job> {
  return post<Job>(jobPath(jobId, '/cancel'))
}
