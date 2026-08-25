/**
 * Turning a model's `installation` block into a percentage.
 *
 * Its own module rather than a second export of `InstallProgress.tsx`, so the
 * bar stays a component file (and the fast-refresh boundary stays clean) while
 * the arithmetic is importable and unit-testable on its own.
 */

import type { ModelInstallation } from '../api/types'

/** Clamp a `0..1` fraction to a whole percentage. */
export function toPercent(fraction: number): number {
  if (!Number.isFinite(fraction)) {
    return 0
  }
  return Math.min(100, Math.max(0, Math.round(fraction * 100)))
}

/**
 * How far a download has got, as a whole percentage, or `null` when that
 * cannot honestly be said.
 *
 * `progress` is the backend's own figure. The byte counts are the fallback for
 * a transfer whose total is known but whose fraction has not been computed
 * yet, and `null` — an indeterminate bar — is the answer when neither is
 * available. **Nothing here is ever a timer**: every number comes from bytes
 * the server has actually written (ARCHITECTURE.md §3's rule about progress,
 * applied to a transfer instead of to inference).
 */
export function installPercent(
  installation: ModelInstallation | null | undefined,
): number | null {
  if (installation === null || installation === undefined) {
    return null
  }
  const received = installation.downloaded_bytes ?? null
  const total = installation.total_bytes ?? null
  const fraction =
    installation.progress ??
    (received !== null && total !== null && total > 0 ? received / total : null)
  return fraction === null ? null : toPercent(fraction)
}
