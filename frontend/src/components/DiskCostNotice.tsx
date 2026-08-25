import { useEffect } from 'react'
import { formatFileSize } from '../format'
import { LARGE_DOWNLOAD_BYTES, diskFit, type DiskFit } from './diskFit'
import { useDiskSpace, type DiskSpaceHandle } from '../state/diskSpace'
import './DiskCostNotice.css'

/** Props of {@link DiskCostNotice}. */
export interface DiskCostNoticeProps {
  /** `installation.total_bytes` — the artifact's size, or `null` if unpublished. */
  readonly totalBytes: number | null | undefined
}

/** What the notice says about the space on the machine, per state. */
function spaceSentence(space: DiskSpaceHandle, fit: DiskFit): string {
  if (space.status === 'idle' || space.status === 'loading') {
    return 'Checking how much space is free on that machine…'
  }
  if (space.status === 'unavailable' || space.freeBytes === null) {
    // Feature 037's sentence, kept for the case it was written for: no figure.
    // The reason has changed — Straticate now asks its backend rather than
    // being unable to ask at all — but the user's position has not, so the
    // advice has not either.
    return (
      'Straticate cannot check that machine’s free space right now, ' +
      'so make sure there is room before installing.'
    )
  }
  const free = formatFileSize(space.freeBytes)
  switch (fit) {
    case 'insufficient':
      return `Only ${free} is free there, so this download will not fit. Free some space first — starting it now will fail when the disk fills.`
    case 'tight':
      return `${free} is free there, so it will fit with little to spare.`
    default:
      return `${free} is free there.`
  }
}

/**
 * What an install will cost on disk, and whether the machine writing it has
 * the room.
 *
 * **The browser cannot answer this and never could.** The weights are written
 * by the *backend*, on whatever machine that is; `navigator.storage.estimate()`
 * describes the quota of the *page's own origin* inside the *browser's* profile
 * directory, which is a different number about a different disk and would look
 * like an answer while being none. Feature 037 said so plainly instead of
 * rendering it; feature 040 added `GET /system/storage`, so this notice now
 * states the comparison the user actually needs — and falls back to 037's
 * honest sentence whenever the figure is missing.
 *
 * **Nothing here refuses an install.** The reading is a fact about one moment:
 * free space moves under it, and a wrong reading (a quota'd volume, a network
 * filesystem) would refuse a download that would have worked, on the one
 * screen that exists to get weights onto the machine. A failed install is
 * cheap and safe by comparison — feature 025 writes to a `.part` sibling,
 * verifies it, and unlinks it on every exit — so the honest design is a loud
 * warning at the moment of the decision, and a button that still works. The
 * feature doc records the reasoning in full.
 *
 * The free-space figure is read **once, when an install is offered** (this
 * component's mount) and again when a download changes what is on the disk.
 * There is no poll: see `state/diskSpace.tsx`.
 */
export function DiskCostNotice({ totalBytes }: DiskCostNoticeProps) {
  const space = useDiskSpace()
  const { ensureRead } = space

  // The read happens *here* — where an install is actually on offer — rather
  // than at application start, so a session that never installs anything never
  // asks. Repeated mounts collapse into one request.
  useEffect(() => {
    ensureRead()
  }, [ensureRead])

  const size =
    typeof totalBytes === 'number' && totalBytes > 0 ? totalBytes : null
  const fit = diskFit(size, space.freeBytes)

  // An **unpublished** size counts as large. The alternative would be to treat
  // a transfer nobody has measured as the small case, which is the one reading
  // the evidence cannot support: a size the catalog does not state could be
  // anything, and the whole point of the warning styling is to mark a download
  // the user should think about before starting. Unknown *free space* earns the
  // same treatment for the same reason.
  const large = size === null || size >= LARGE_DOWNLOAD_BYTES || fit !== 'fits'
  const className = [
    'disk-cost',
    large ? 'disk-cost-large' : '',
    fit === 'insufficient' ? 'disk-cost-insufficient' : '',
  ]
    .filter((part) => part !== '')
    .join(' ')

  return (
    <p className={className} role="note">
      {size === null
        ? 'This model publishes no download size. It will be written to the machine running Straticate.'
        : `${formatFileSize(size)} will be written to the machine running Straticate.`}{' '}
      {spaceSentence(space, fit)}
    </p>
  )
}
