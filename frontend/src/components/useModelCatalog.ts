/**
 * The catalog itself: every logical model the backend serves, read once from
 * `GET /api/v1/models`.
 *
 * This is the *inventory* half of model management, and it is deliberately
 * separate from `useModelInstallation`, which is the *live state* half:
 *
 * - A `Model` is a projection of its manifest — display name, separation mode,
 *   quality tier, stems, `requirements`, `capabilities`, `licensing`. None of
 *   that changes while the process runs, so it is read once and kept.
 * - `installation` is the one mutable part (feature 025 confined it to that
 *   nested object on purpose), and it belongs to whoever is watching a
 *   particular model: a library row, or the configure step's selected tier.
 *   `useModelInstallation` owns it, with its polling, its request guards and
 *   its sequence-numbered responses.
 *
 * So the library reads the catalog once and then lets each row watch its own
 * model, rather than re-reading the whole collection on a timer: only a model
 * that is actually downloading costs anything after the first request, and the
 * install machinery is the one feature 035 already proved rather than a second
 * copy of it.
 *
 * The `installation` blocks this read *does* carry are still useful — they are
 * what lets the configure step price a quality tier the user has not selected
 * yet, and what a library row shows for the round trip before its own first
 * read answers.
 */

import { useCallback, useEffect, useState } from 'react'
import { listModels } from '../api/models'
import type { Model } from '../api/types'
import { errorInfo, type InstallationError } from './useModelInstallation'

/** State of the `GET /models` read. */
export type ModelCatalogStatus = 'loading' | 'loaded' | 'error'

/** What {@link useModelCatalog} hands back. */
export interface ModelCatalogHandle {
  /** State of the read. */
  readonly status: ModelCatalogStatus
  /** Every catalogued model, in catalog order; empty until a read succeeds. */
  readonly models: readonly Model[]
  /** Why the read failed, or `null`. */
  readonly error: InstallationError | null
  /** Read the catalog again. */
  readonly refresh: () => void
}

/**
 * Read the model catalog once on mount, with a `refresh` for retrying a failed
 * read or picking up a model that has just been installed.
 *
 * A failed read leaves `models` empty rather than stale: there is no partial
 * catalog worth rendering, and the caller has a retry to offer.
 */
export function useModelCatalog(): ModelCatalogHandle {
  const [readCount, setReadCount] = useState(0)
  const [models, setModels] = useState<readonly Model[] | null>(null)
  const [error, setError] = useState<InstallationError | null>(null)

  const refresh = useCallback(() => {
    // Clearing the error is what makes a retry *look* like one: without it the
    // view keeps saying the catalog could not be read while the request that
    // may disprove that is in flight.
    setError(null)
    setReadCount((count) => count + 1)
  }, [])

  useEffect(() => {
    let cancelled = false
    listModels()
      .then((catalog) => {
        if (!cancelled) {
          setModels(catalog)
          setError(null)
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setModels(null)
          setError(errorInfo(reason))
        }
      })
    return () => {
      cancelled = true
    }
    // `readCount` is the refresh trigger; the request takes no other input.
  }, [readCount])

  // A failure the user may need to act on outranks a catalog that may be a
  // moment old — and a failed read clears the catalog, so the two cannot both
  // be present.
  const status: ModelCatalogStatus =
    error !== null ? 'error' : models === null ? 'loading' : 'loaded'

  return { status, models: models ?? [], error, refresh }
}
