/**
 * System endpoints of the Straticate backend (`/api/v1/system/...`).
 *
 * **Why free disk space is a backend question.** Model weights are written by
 * the backend, to `Settings.models_dir`, on whatever machine that is. The one
 * disk figure a browser can obtain — `navigator.storage.estimate()` —
 * describes the quota of the *page's own origin* inside the *browser's*
 * profile directory: a different number about a different disk. Feature 037
 * stated that limitation honestly rather than rendering a figure that would
 * look like an answer; feature 040 replaced it with this call.
 */

import { get } from './client'
import type { StorageReport } from './types'

/** Path of the storage report, relative to the `/api/v1` base. */
const STORAGE_PATH = '/system/storage'

/**
 * Read free and total bytes for the filesystem holding the models directory
 * (`GET /api/v1/system/storage`).
 *
 * **`null` is a documented answer, not a failure.** A host that cannot produce
 * the figures — a models directory whose whole path is missing, a permissions
 * failure, a filesystem the platform has no answer for — still responds `200`,
 * with both fields `null`. A caller must render that as "unknown" and treat it
 * as the cautious case; only a rejected promise means the request itself
 * failed.
 *
 * `free_bytes: 0` is emphatically *not* unknown: it is a full disk, which is
 * the case worth warning loudest about.
 */
export function getSystemStorage(): Promise<StorageReport> {
  return get<StorageReport>(STORAGE_PATH)
}
