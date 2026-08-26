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
import type { Job, SeparationConfiguration, StereoHandling } from './types'

/** Path segment of the jobs collection, relative to the `/api/v1` base. */
const JOBS_PATH = '/jobs'

/** How one `StereoHandling` value is offered to the user. */
export interface StereoHandlingOption {
  /** Contract value sent as `stereo_handling` on the create-job request. */
  readonly id: StereoHandling
  /** Short human-readable name for the picker. */
  readonly label: string
  /** What choosing it actually does to the user's audio, and what it costs. */
  readonly note: string
}

/**
 * Every stereo-handling choice, keyed by the generated `StereoHandling` union.
 *
 * Same shape and the same reason as `EXPORT_FORMAT_TABLE` in `./export`: the
 * union is a *type*, so keying a `Record` by it makes the table exhaustive in
 * both directions, and a backend that gains a value turns this object into a
 * type error until it is described. Declaration order is picker order.
 *
 * The wording matters more here than in most pickers. This control changes the
 * user's *audio*, not a model parameter, so each note says what is done and
 * what it costs — never "improves separation", which would be a promise the
 * app cannot keep for an arbitrary mix.
 *
 * The fold is specifically a control that **recovers a stem you would otherwise
 * lose**, not one that separates better. Feature 041 measured it: the four
 * stems reconstruct the mixture at +0.999 either way, so nothing is gained or
 * lost overall — a stem that was near-silent becomes usable because the low end
 * is *reassigned*. The note says that, and no more.
 */
const STEREO_HANDLING_TABLE: Record<
  StereoHandling,
  Omit<StereoHandlingOption, 'id'>
> = {
  as_is: {
    label: 'Keep stereo',
    note: 'Separate the recording exactly as it is.',
  },
  mono: {
    label: 'Fold to mono',
    note: 'Mixes left and right together first, so the stems come back mono. On a very wide older stereo mix this recovers a stem that would otherwise come out near-silent. It does not otherwise change how well the parts are told apart.',
  },
}

/** Every stereo-handling choice the backend offers, in picker order. */
export const STEREO_HANDLING_OPTIONS: readonly StereoHandlingOption[] =
  Object.entries(STEREO_HANDLING_TABLE).map(([id, option]) => ({
    id: id as StereoHandling,
    ...option,
  }))

/** What the backend applies when `stereo_handling` is omitted. */
export const DEFAULT_STEREO_HANDLING: StereoHandling = 'as_is'

/** Look up one choice's presentation metadata. */
export function stereoHandlingOption(
  handling: StereoHandling,
): StereoHandlingOption {
  return { id: handling, ...STEREO_HANDLING_TABLE[handling] }
}

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
