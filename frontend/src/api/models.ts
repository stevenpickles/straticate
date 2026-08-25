/**
 * Model endpoints of the Straticate backend (`/api/v1/models`).
 *
 * A catalogued model is not necessarily a *ready* one: weights are never
 * shipped in the repository (ARCHITECTURE.md §9), so a fresh checkout offers
 * models whose bytes are still on the network. Every model therefore carries
 * an `installation` block — state, artifact size, bytes received, progress and
 * the last failure — and these three calls are the whole install interaction:
 * read it, start a download, throw the weights away again.
 *
 * **Progress is read here, not pushed.** Feature 025 chose a REST field over a
 * WebSocket event deliberately (`docs/features/025-model-download-manager.md`):
 * an install is rare, user-initiated and coarse-grained, REST is the source of
 * truth for reconnect and refresh (ARCHITECTURE.md §11), and the state field is
 * needed on a plain `GET` anyway. So a client watching a download polls
 * {@link getModel} — see `useModelInstallation` for the interval and the rules
 * that stop it. AGENTS.md principle 3 ("no polling loops") is about *job*
 * progress, which is chunk-grained real work with an event stream of its own.
 *
 * Every function rejects with an {@link ApiError} carrying the backend error
 * envelope's `code`, `message`, and `detail`.
 */

import { del, get, post } from './client'
import type { Model } from './types'

/** Path segment of the models collection, relative to the `/api/v1` base. */
const MODELS_PATH = '/models'

function modelPath(modelId: string, suffix = ''): string {
  return `${MODELS_PATH}/${encodeURIComponent(modelId)}${suffix}`
}

/**
 * Fetch one model, including its live installation state
 * (`GET /api/v1/models/{model_id}`).
 *
 * This is where download progress is read: while an install runs the returned
 * `installation` reports `downloading` with `downloaded_bytes` and `progress`.
 *
 * Rejects with `model_not_found` (404) for an unknown ID — which includes a
 * development fixture on a server that hides them (feature 032).
 */
export function getModel(modelId: string): Promise<Model> {
  return get<Model>(modelPath(modelId))
}

/**
 * Start downloading a model's weights
 * (`POST /api/v1/models/{model_id}/install`).
 *
 * Returns as soon as the download is queued (`202`), with the model in state
 * `downloading`; the transfer is hundreds of megabytes and never holds the
 * request open. Poll {@link getModel} for progress and the outcome — a failed
 * install reports `failed` with the reason in `installation.error`, and never
 * reaches this promise.
 *
 * Installing weights that are already present is an idempotent no-op that
 * answers `installed`. Rejects with `model_not_found` (404),
 * `model_not_downloadable` (409) for a model that has no artifact, or
 * `model_busy` (409) when an install for it is already running.
 */
export function installModel(modelId: string): Promise<Model> {
  return post<Model>(modelPath(modelId, '/install'))
}

/**
 * Delete a model's weights, returning it to `available`
 * (`DELETE /api/v1/models/{model_id}/weights`).
 *
 * A running install is **cancelled** first, which is also the only way out of
 * a download that will not finish. Idempotent: removing weights that are not
 * installed succeeds. Rejects with `model_not_found` (404) or
 * `model_not_downloadable` (409) for a model that has no artifact.
 */
export function removeModelWeights(modelId: string): Promise<Model> {
  return del<Model>(modelPath(modelId, '/weights'))
}
