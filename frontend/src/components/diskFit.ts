/**
 * The arithmetic behind "870 MB needed, 2.1 GB free": does a download fit in
 * the room the backend reports, and is it big enough to be worth pausing over?
 *
 * Pure, tested, and out of `DiskCostNotice.tsx` so the component file exports
 * only its component — the same split feature 037 made for `installProgress.ts`
 * when two places had to agree about a download's progress.
 */

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

/**
 * Free space a download should leave behind it before it counts as fitting
 * comfortably.
 *
 * 512 MB. A download that fits with nothing to spare technically fits and
 * practically does not: the same filesystem holds this application's uploads,
 * job outputs and export artifacts (which accumulate — features 021, 022 and
 * 025 all record that nothing prunes them), and an operating system with a few
 * hundred megabytes left is one about to have problems of its own. Below this
 * margin the notice says the install will *just* fit, which is a different
 * sentence from both "there is room" and "this will not fit" — and it never
 * blocks anything.
 */
export const TIGHT_HEADROOM_BYTES = 512 * 1024 * 1024

/** How a download's size compares with the space available for it. */
export type DiskFit = 'unknown' | 'fits' | 'tight' | 'insufficient'

/**
 * Compare a download's size with the free space it has to fit into.
 *
 * `unknown` whenever *either* number is missing, and unknown is deliberately
 * not "fine": an unmeasured download could be any size, and an unread disk
 * could have any amount of room. Both are the cautious case, which is the same
 * reasoning feature 037 applied to an artifact whose size the catalog does not
 * publish.
 *
 * Zero free bytes is emphatically *not* unknown — it is a full disk, the case
 * worth warning loudest about.
 */
export function diskFit(
  sizeBytes: number | null,
  freeBytes: number | null,
): DiskFit {
  if (sizeBytes === null || sizeBytes <= 0 || freeBytes === null) {
    return 'unknown'
  }
  if (freeBytes < sizeBytes) {
    return 'insufficient'
  }
  return freeBytes - sizeBytes < TIGHT_HEADROOM_BYTES ? 'tight' : 'fits'
}
