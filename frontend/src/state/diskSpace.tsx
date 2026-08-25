/**
 * How much room the machine running Straticate has left — read where an
 * install is offered, and nowhere else.
 *
 * **Why this is not part of `useModelInstallation`.** That hook watches *one
 * model's installation record*: a per-model thing with a per-model lifetime,
 * polled while its own download runs. Free space is the opposite on every
 * count — one fact about the whole machine, shared by every install affordance
 * on screen, and true (or not) regardless of which model is selected. Giving
 * each card its own copy would mean N requests for one number and N answers
 * that could disagree with each other; so it is held once, above them.
 *
 * **There is no poll here, deliberately.** Features 025 and 035 both reasoned
 * carefully about when a timer is defensible (a resource re-read while its own
 * download runs, 1 Hz, stopping on every terminal state) and AGENTS.md
 * principle 3 rules out the rest. A figure that only matters at the moment
 * somebody decides whether to start a download needs exactly two reads:
 *
 * 1. **when an install is offered** — {@link DiskSpaceHandle.ensureRead}, called
 *    from the notice that renders the comparison, so nothing is fetched on a
 *    screen where no install is on offer;
 * 2. **when the disk demonstrably changed** —
 *    {@link DiskSpaceHandle.noteDiskChanged}, called when weights land, are
 *    thrown away, or a failed download's partial file is discarded.
 *
 * Simultaneous mounts collapse into one request (a read in flight is never
 * duplicated), and a held figure is reused until it is
 * {@link STORAGE_MAX_AGE_MS} old — which is what keeps "read when an install is
 * offered" honest for a session that opens the library an hour later, without
 * a single background timer.
 *
 * **Unknown is not "fine".** Feature 037's `DiskCostNotice` treats an
 * *unmeasured download size* as the large case, because a size nobody has
 * stated could be anything. The same reasoning applies here, so this handle
 * distinguishes four states rather than reporting `0`: a consumer can tell "no
 * room" from "no answer", and render the honest sentence for the latter.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { getSystemStorage } from '../api/system'

/**
 * How long a reading stays fresh enough to reuse, in milliseconds.
 *
 * Thirty seconds. Long enough that opening the library, scrolling through
 * three cards and pressing Install costs one request rather than four; short
 * enough that a figure the user reads before committing to a download is
 * *about now* rather than about whenever this tab was opened. It is a
 * staleness bound on a read that only happens when an affordance mounts —
 * never a schedule: nothing here sets a timer, so a page nobody is installing
 * from makes no requests at all.
 */
export const STORAGE_MAX_AGE_MS = 30_000

/** Whether the machine's free space is known, being read, or unavailable. */
export type DiskSpaceStatus = 'idle' | 'loading' | 'known' | 'unavailable'

/** What {@link useDiskSpace} hands back. */
export interface DiskSpaceHandle {
  /**
   * `idle` before anything asked, `loading` while a read is in flight with
   * nothing held, `known` once figures arrived, `unavailable` when the host
   * could not produce them or the request failed.
   */
  readonly status: DiskSpaceStatus
  /** Free bytes on the filesystem holding the models directory, or `null`. */
  readonly freeBytes: number | null
  /** Total bytes of that filesystem, or `null`. */
  readonly totalBytes: number | null
  /**
   * Read the figure if nothing fresh is held. Safe to call from an effect on
   * every mount and from several components at once: a read in flight is not
   * duplicated, and a reading younger than {@link STORAGE_MAX_AGE_MS} is
   * reused.
   */
  readonly ensureRead: () => void
  /**
   * Something just changed what is on the disk — weights landed, weights were
   * removed, a partial download was discarded — so whatever is held is stale.
   * Reads again immediately.
   */
  readonly noteDiskChanged: () => void
}

/**
 * What a tree with no provider sees: no figure, and no request either.
 *
 * `unavailable` rather than `idle`, because it is the truth for such a tree
 * and it is the *cautious* reading — a component asking this question renders
 * feature 037's honest "cannot check" sentence rather than a comparison it
 * cannot make. Unit tests that mount a card in isolation therefore keep
 * exactly the behaviour they had before this feature existed.
 */
const NO_PROVIDER: DiskSpaceHandle = {
  status: 'unavailable',
  freeBytes: null,
  totalBytes: null,
  ensureRead: () => undefined,
  noteDiskChanged: () => undefined,
}

const DiskSpaceContext = createContext<DiskSpaceHandle>(NO_PROVIDER)

/**
 * The most recent settled read: which request it answered, and what it said.
 *
 * Only *outcomes* are stored. "Loading" is a derivation — a request has been
 * asked for that this has not answered yet — rather than a state somebody has
 * to remember to set, which is also what keeps the whole read out of a
 * `setState`-inside-an-effect shape.
 */
interface DiskSpaceReading {
  /** The `readCount` this answers; `0` means nothing has answered yet. */
  readonly count: number
  /** Whether it produced figures at all. */
  readonly known: boolean
  readonly freeBytes: number | null
  readonly totalBytes: number | null
}

const NOTHING_READ: DiskSpaceReading = {
  count: 0,
  known: false,
  freeBytes: null,
  totalBytes: null,
}

/** Props of {@link DiskSpaceProvider}. */
export interface DiskSpaceProviderProps {
  readonly children: ReactNode
}

/**
 * Hold one reading of the machine's free space for the whole page.
 *
 * Mounted once, at the application root, because both places an install is
 * offered — the configure step's panel (035) and the library's cards (037) —
 * are separate trees that must not disagree about a fact concerning one disk.
 * It fetches nothing on mount: the first read happens when something actually
 * offers an install.
 */
export function DiskSpaceProvider({ children }: DiskSpaceProviderProps) {
  const [reading, setReading] = useState<DiskSpaceReading>(NOTHING_READ)
  // When the held reading was taken (or started being taken). `null` means
  // "nothing held", which is also how a disk change invalidates it.
  const readAtRef = useRef<number | null>(null)
  const inFlightRef = useRef(false)
  // A disk change that arrived while a read was already in flight: that read
  // was started before the change and its answer describes the world as it
  // was, so another one follows it.
  const staleRef = useRef(false)

  // The read trigger, in the same shape `useModelInstallation` uses: a counter
  // whose every increment is one request. It starts at zero and *nothing* is
  // fetched there, which is what "no read until an install is offered" means.
  const [readCount, setReadCount] = useState(0)

  const read = useCallback(() => {
    if (inFlightRef.current) {
      return
    }
    // Stamped when the read is *asked for*, so several components mounting in
    // the same frame make one request between them.
    readAtRef.current = Date.now()
    staleRef.current = false
    setReadCount((count) => count + 1)
  }, [])

  useEffect(() => {
    if (readCount === 0) {
      return
    }
    let cancelled = false
    inFlightRef.current = true
    getSystemStorage()
      .then((report) => {
        if (cancelled) {
          return
        }
        setReading(
          // `null` is the backend's documented "this host cannot tell you",
          // and anything that is not a number is the same situation arriving
          // less politely. Neither is a figure, and neither is `0`.
          typeof report.free_bytes === 'number'
            ? {
                count: readCount,
                known: true,
                freeBytes: report.free_bytes,
                totalBytes:
                  typeof report.total_bytes === 'number'
                    ? report.total_bytes
                    : null,
              }
            : { ...NOTHING_READ, count: readCount },
        )
      })
      .catch(() => {
        // The request failed rather than the host declining to answer. Both
        // leave the user without a figure, and both are the cautious case.
        if (!cancelled) {
          setReading({ ...NOTHING_READ, count: readCount })
        }
      })
      .finally(() => {
        inFlightRef.current = false
        readAtRef.current = Date.now()
        if (staleRef.current && !cancelled) {
          // The disk changed while this request was in the air, so its answer
          // describes the world as it was. One more read, and only one.
          staleRef.current = false
          readAtRef.current = null
          setReadCount((count) => count + 1)
        }
      })
    return () => {
      cancelled = true
    }
  }, [readCount])

  const ensureRead = useCallback(() => {
    const readAt = readAtRef.current
    if (readAt !== null && Date.now() - readAt < STORAGE_MAX_AGE_MS) {
      return
    }
    read()
  }, [read])

  const noteDiskChanged = useCallback(() => {
    if (inFlightRef.current) {
      // Let the read in flight settle, then take a fresh one: its answer
      // predates the change.
      staleRef.current = true
      return
    }
    readAtRef.current = null
    read()
  }, [read])

  // Status is derived, never stored: nothing has to remember to set "loading",
  // and a figure already held **survives a re-read** rather than blanking the
  // comparison somebody is reading for a round trip.
  let status: DiskSpaceStatus = 'idle'
  if (readCount > 0) {
    status = reading.known
      ? 'known'
      : reading.count < readCount
        ? 'loading'
        : 'unavailable'
  }

  // Identity changes only when the figures do: both actions are stable, so an
  // effect depending on this handle does not re-run on every render of the
  // tree below it.
  const handle = useMemo<DiskSpaceHandle>(
    () => ({
      status,
      freeBytes: reading.freeBytes,
      totalBytes: reading.totalBytes,
      ensureRead,
      noteDiskChanged,
    }),
    [status, reading, ensureRead, noteDiskChanged],
  )

  return (
    <DiskSpaceContext.Provider value={handle}>
      {children}
    </DiskSpaceContext.Provider>
  )
}

/**
 * The machine's free space, as far as this page knows it.
 *
 * Without a {@link DiskSpaceProvider} above it this answers `unavailable` and
 * requests nothing, so a component that renders the comparison degrades to
 * feature 037's honest wording rather than to a wrong number.
 */
export function useDiskSpace(): DiskSpaceHandle {
  return useContext(DiskSpaceContext)
}
