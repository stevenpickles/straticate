import { formatFileSize } from '../format'
import './DiskCostNotice.css'

/**
 * Bytes from which a download is worth warning about rather than merely
 * pricing.
 *
 * 100 MB. Below it a transfer is an inconvenience; above it, it is a decision
 * — minutes of bandwidth and a chunk of somebody's disk. The real weights this
 * application ships a catalog entry for are 870 MB, so every model a user can
 * actually install today is over the line; the threshold exists so that a
 * future catalog entry of a few megabytes is not dressed up as a commitment.
 */
export const LARGE_DOWNLOAD_BYTES = 100 * 1024 * 1024

/** Props of {@link DiskCostNotice}. */
export interface DiskCostNoticeProps {
  /** `installation.total_bytes` — the artifact's size, or `null` if unpublished. */
  readonly totalBytes: number | null | undefined
}

/**
 * What an install will cost on disk, and the plain admission that Straticate
 * cannot check whether that cost can be paid.
 *
 * **It genuinely cannot**, and saying so is the honest option rather than the
 * lazy one. The weights are written by the **backend**, on whatever machine
 * that is; the browser has no view of that machine's filesystem. The one disk
 * figure a browser can obtain — `navigator.storage.estimate()` — describes the
 * quota of the *page's own origin* in the *browser's* profile directory, which
 * is a different number about a different disk, and reporting it here would be
 * worse than silence: it would look like an answer.
 *
 * A backend endpoint reporting free space beside `models_dir` would be the
 * real fix, and it is a backend change — out of scope for feature 037, and
 * recorded as such rather than improvised. Until then the user is told the
 * size, told that the check is not being made, and left to make the call, on
 * the principle that an 870 MB fetch should never start without the user
 * having seen its price.
 */
export function DiskCostNotice({ totalBytes }: DiskCostNoticeProps) {
  const known = typeof totalBytes === 'number' && totalBytes > 0
  // An **unpublished** size counts as large. The alternative would be to treat
  // a transfer nobody has measured as the small case, which is the one reading
  // the evidence cannot support: a size the catalog does not state could be
  // anything, and the whole point of the warning styling is to mark a download
  // the user should think about before starting.
  const large = !known || totalBytes >= LARGE_DOWNLOAD_BYTES

  return (
    <p className={`disk-cost${large ? ' disk-cost-large' : ''}`} role="note">
      {known
        ? `${formatFileSize(totalBytes)} will be written to the machine running Straticate.`
        : 'This model publishes no download size.'}{' '}
      Straticate cannot check that machine&rsquo;s free space from the browser,
      so make sure there is room before installing.
    </p>
  )
}
