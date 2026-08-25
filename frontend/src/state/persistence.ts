/**
 * Session persistence for the workflow — **identifiers only**.
 *
 * A reload used to be a new session: workflow state lived in React context
 * and nothing survived it, so reloading mid-job dropped the user back at
 * file selection while the backend job ran on to completion, unreachable
 * from the UI (recorded as a finding by feature 030).
 *
 * What this module stores is deliberately tiny: the tracked job's id, the
 * uploaded audio's id, and the workflow phase. It stores **no records** —
 * no `Job`, no `AudioFile`, no result, no metrics. REST is the source of
 * truth (ARCHITECTURE.md §4/§11) and a cached record races the event
 * stream: features 017 and 031 both cost the project a stranded UI because
 * a stale snapshot of a job was allowed to win over what the events had
 * already delivered. A stored *id* cannot go stale in that way — it is
 * either still known to the backend or it is not — so rehydration re-reads
 * the records with `GET /jobs/{id}` and `GET /audio/{id}` (see
 * {@link useSessionRestore} in `SessionGate.tsx`).
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

/** Key the session snapshot is stored under; versioned so its shape can change. */
export const SESSION_STORAGE_KEY = 'straticate.session.v1'

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
}

/** A snapshot with nothing to restore; also what an unreadable store yields. */
export const emptySessionSnapshot: SessionSnapshot = {
  jobId: null,
  audioId: null,
  phase: null,
}

/** Whether a snapshot carries anything worth rehydrating. */
export function isEmptySessionSnapshot(snapshot: SessionSnapshot): boolean {
  return snapshot.jobId === null && snapshot.audioId === null
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
