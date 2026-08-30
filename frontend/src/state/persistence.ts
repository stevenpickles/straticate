/**
 * Session persistence for the workflow — **identifiers, plus the numbers a
 * view is drawn from**.
 *
 * A reload used to be a new session: workflow state lived in React context
 * and nothing survived it, so reloading mid-job dropped the user back at
 * file selection while the backend job ran on to completion, unreachable
 * from the UI (recorded as a finding by feature 030).
 *
 * What this module stores is deliberately tiny: the tracked job's id, the
 * uploaded audio's id, the workflow phase, and (feature 066) the Inspect
 * view for that job. It stores **no records** — no `Job`, no `AudioFile`, no
 * result, no metrics. REST is the source of truth (ARCHITECTURE.md §4/§11)
 * and a cached record races the event stream: features 017 and 031 both
 * cost the project a stranded UI because a stale snapshot of a job was
 * allowed to win over what the events had already delivered. A stored *id*
 * cannot go stale in that way — it is either still known to the backend or
 * it is not — so rehydration re-reads the records with `GET /jobs/{id}` and
 * `GET /audio/{id}` (see {@link useSessionRestore} in `SessionGate.tsx`).
 *
 * The view is the same idea applied one layer down: it never carries a
 * `Job` or a `SeparationResult`, only seconds and a zoom factor, and it is
 * **keyed by the jobId it was recorded against** — a view for a job that is
 * not the one being restored is exactly as stale as an unknown job id, and
 * is dropped the same way (see `stemSession.tsx`).
 *
 * `sessionStorage` rather than `localStorage`, and no cross-tab sync: the
 * hub broadcasts every event to every client, and feature 017 deliberately
 * removed `job_created` adoption so that a second tab can never take over
 * the first one's job. Session scope keeps each tab's workflow its own.
 *
 * Every access is wrapped: `sessionStorage` throws on the property itself
 * when site data is blocked, and `setItem` throws in some private modes.
 * When storage is unavailable each function here is a no-op or returns the
 * empty snapshot, and the app behaves exactly as it did before this
 * feature.
 */
import { WORKFLOW_PHASES, type WorkflowPhase } from './appState'

/**
 * Key the session snapshot is stored under; versioned so its shape can
 * change. Bumped to `.v2` by feature 066, which adds the optional `view`
 * field below — a `.v1` record simply is not found under this key, so an
 * old tab's snapshot is silently ignored rather than misread, exactly as an
 * unparsable payload under this key already is (see
 * {@link readSessionSnapshot}).
 */
export const SESSION_STORAGE_KEY = 'straticate.session.v2'

/**
 * The Inspect view for one job: the playhead, the loop region, and the
 * timeline's zoom/scroll window — restored once the engine reaches `ready`
 * for the **same** `jobId` (see `stemSession.tsx`). Numbers only, in the
 * spirit of the module docstring's "identifiers only": every field either
 * applies to `jobId`'s own timeline or the whole view is stale.
 */
export interface ViewSnapshot {
  /** ULID of the job this view was recorded against. */
  readonly jobId: string
  /** The playhead, in seconds. */
  readonly positionSeconds: number
  /** Where a loop region starts, in seconds — `null` with `loopEnd` when there is none. */
  readonly loopStart: number | null
  /** Where a loop region ends, in seconds — `null` with `loopStart` when there is none. */
  readonly loopEnd: number | null
  /** The timeline's zoom factor. */
  readonly zoom: number
  /** The timeline window's left edge, in seconds. */
  readonly scrollSeconds: number
}

/**
 * The identifiers that survive a reload.
 *
 * Every field is nullable and independently meaningful: an audio id with no
 * job is the `configure` phase, a job id implies its own audio through
 * `Job.audio_id`, and the phase disambiguates the several places a user can
 * be with the *same* completed job (`separate` showing the result summary,
 * or `inspect` playing its stems).
 */
export interface SessionSnapshot {
  /** ULID of the tracked job, or `null` when none is tracked. */
  readonly jobId: string | null
  /** ULID of the uploaded audio, or `null` when nothing is uploaded. */
  readonly audioId: string | null
  /** Workflow phase the user was on, or `null` when it was never stored. */
  readonly phase: WorkflowPhase | null
  /**
   * The Inspect view, or `null` when there is none to restore — no session
   * was ever opened, or it belonged to a job that is not `jobId` above. See
   * {@link ViewSnapshot}.
   */
  readonly view: ViewSnapshot | null
}

/** A snapshot with nothing to restore; also what an unreadable store yields. */
export const emptySessionSnapshot: SessionSnapshot = {
  jobId: null,
  audioId: null,
  phase: null,
  view: null,
}

/**
 * Whether a snapshot carries anything worth rehydrating. A `view` counts:
 * `writeViewSnapshot` can be the first thing to touch a fresh store (its
 * read-modify-write starts from whatever is already there, identifiers or
 * not), and a view with no identifiers to go with it is still something a
 * reload should restore rather than silently drop.
 */
export function isEmptySessionSnapshot(snapshot: SessionSnapshot): boolean {
  return (
    snapshot.jobId === null &&
    snapshot.audioId === null &&
    snapshot.view === null
  )
}

/**
 * The session store, or `null` when it is unavailable.
 *
 * Reading the property itself throws in browsers configured to block site
 * data, so even the lookup is guarded.
 */
function sessionStore(): Storage | null {
  try {
    const storage: Storage | undefined = globalThis.sessionStorage
    return storage ?? null
  } catch {
    return null
  }
}

/** A string field of the parsed payload, or `null` for anything else. */
function optionalString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

/** The stored phase, or `null` when it is missing or not a known phase. */
function optionalPhase(value: unknown): WorkflowPhase | null {
  return WORKFLOW_PHASES.find((phase) => phase === value) ?? null
}

/** A finite number, or `null` for anything else — including `NaN`/`Infinity`. */
function optionalFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * The stored `view`, or `null` when it is missing or not the shape this
 * version writes — a record from before feature 066 (no `view` key at all)
 * reads exactly the same as one where the field failed validation, which is
 * the point: **a v2 reader tolerates a missing or malformed `view`** just as
 * `readSessionSnapshot` already tolerates a missing or malformed field
 * anywhere else in the payload, and the session snapshot around it still
 * restores.
 */
function optionalView(value: unknown): ViewSnapshot | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }
  const fields = value as Record<string, unknown>
  const jobId = optionalString(fields.jobId)
  const positionSeconds = optionalFiniteNumber(fields.positionSeconds)
  const zoom = optionalFiniteNumber(fields.zoom)
  const scrollSeconds = optionalFiniteNumber(fields.scrollSeconds)
  if (
    jobId === null ||
    positionSeconds === null ||
    zoom === null ||
    scrollSeconds === null
  ) {
    return null
  }
  const loopStart = optionalFiniteNumber(fields.loopStart)
  const loopEnd = optionalFiniteNumber(fields.loopEnd)
  if ((loopStart === null) !== (loopEnd === null)) {
    // A region needs both ends; one without the other is not a region this
    // module ever wrote, so it is treated as no region at all rather than
    // guessed at.
    return null
  }
  return { jobId, positionSeconds, loopStart, loopEnd, zoom, scrollSeconds }
}

/**
 * Read the stored snapshot.
 *
 * Returns {@link emptySessionSnapshot} when storage is unavailable, nothing
 * is stored, or the stored payload is not the shape this version writes —
 * a snapshot written by an older build, or corrupted by anything else on
 * the origin, must start the user cleanly rather than throw during startup.
 */
export function readSessionSnapshot(): SessionSnapshot {
  const store = sessionStore()
  if (store === null) {
    return emptySessionSnapshot
  }
  let raw: string | null
  try {
    raw = store.getItem(SESSION_STORAGE_KEY)
  } catch {
    return emptySessionSnapshot
  }
  if (raw === null) {
    return emptySessionSnapshot
  }
  let payload: unknown
  try {
    payload = JSON.parse(raw)
  } catch {
    return emptySessionSnapshot
  }
  if (typeof payload !== 'object' || payload === null) {
    return emptySessionSnapshot
  }
  const fields = payload as Record<string, unknown>
  return {
    jobId: optionalString(fields.jobId),
    audioId: optionalString(fields.audioId),
    phase: optionalPhase(fields.phase),
    view: optionalView(fields.view),
  }
}

/**
 * Write the snapshot, or remove it entirely when there is nothing to
 * restore (an empty snapshot and no stored key must be indistinguishable,
 * so clearing the workflow really clears it).
 *
 * Silently does nothing when storage is unavailable or full.
 */
export function writeSessionSnapshot(snapshot: SessionSnapshot): void {
  const store = sessionStore()
  if (store === null) {
    return
  }
  try {
    if (isEmptySessionSnapshot(snapshot)) {
      store.removeItem(SESSION_STORAGE_KEY)
      return
    }
    store.setItem(SESSION_STORAGE_KEY, JSON.stringify(snapshot))
  } catch {
    // Storage is disabled, full, or otherwise refusing writes. The session
    // simply will not survive a reload; nothing else about the app changes.
  }
}

/**
 * Update just the `view`, leaving `jobId`/`audioId`/`phase` exactly as they
 * are on disk.
 *
 * The view changes far more often than the identifiers do — every seek, loop
 * edit, zoom, pan and pause — while the identifiers are written by
 * `SessionGate` on its own, much less frequent, schedule. A read-modify-write
 * over the whole snapshot is what lets both writers reach the same key
 * without one clobbering the other's field, and it is the single choke point
 * every view-changing commit in `stemSession.tsx` goes through.
 *
 * Silently does nothing when storage is unavailable, exactly like
 * {@link writeSessionSnapshot}.
 */
export function writeViewSnapshot(view: ViewSnapshot | null): void {
  const current = readSessionSnapshot()
  writeSessionSnapshot({ ...current, view })
}

/** Forget the stored snapshot. Silently does nothing when storage is unavailable. */
export function clearSessionSnapshot(): void {
  const store = sessionStore()
  if (store === null) {
    return
  }
  try {
    store.removeItem(SESSION_STORAGE_KEY)
  } catch {
    // As above: an unwritable store is not an error the user can act on.
  }
}
