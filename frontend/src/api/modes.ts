/**
 * Model-catalog endpoints of the Straticate backend.
 *
 * Separation modes are *derived* by the backend from the installed model
 * catalog (ARCHITECTURE.md §9): mode IDs, display names, stem lists and
 * quality tiers all arrive from here, so the UI never hardcodes them.
 *
 * Rejects with an {@link ApiError} carrying the backend envelope's `code`,
 * `message`, and `detail`.
 */

import { get } from './client'
import type { SeparationMode } from './types'

/**
 * List the separation modes the backend can offer
 * (`GET /api/v1/separation-modes`).
 *
 * Each {@link SeparationMode} carries its stem list and a non-empty,
 * ordered `quality_options` array (`fast → balanced → high_quality`); a
 * mode served by a single model still exposes exactly one option.
 */
export function listSeparationModes(): Promise<SeparationMode[]> {
  return get<SeparationMode[]>('/separation-modes')
}
