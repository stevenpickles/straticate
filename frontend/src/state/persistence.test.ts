import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  SESSION_STORAGE_KEY,
  clearSessionSnapshot,
  emptySessionSnapshot,
  isEmptySessionSnapshot,
  readSessionSnapshot,
  writeSessionSnapshot,
  writeViewSnapshot,
  type SessionSnapshot,
  type ViewSnapshot,
} from './persistence'

/** A storage double whose every method throws, as a blocked store does. */
const throwingStorage = {
  getItem: () => {
    throw new Error('storage is disabled')
  },
  setItem: () => {
    throw new Error('storage is disabled')
  },
  removeItem: () => {
    throw new Error('storage is disabled')
  },
} as unknown as Storage

/**
 * Replace `globalThis.sessionStorage` with a property that throws when it
 * is *read* — what a browser configured to block site data does, before any
 * method can be called. Returns a restore function.
 */
function blockStorageProperty(): () => void {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    get() {
      throw new Error('access to storage is denied')
    },
  })
  return () => {
    if (original === undefined) {
      Reflect.deleteProperty(globalThis, 'sessionStorage')
    } else {
      Object.defineProperty(globalThis, 'sessionStorage', original)
    }
  }
}

const sampleJobId = '01SAMPLEJOBULID00000000000'

const snapshot: SessionSnapshot = {
  jobId: sampleJobId,
  audioId: '01SAMPLEAUDIOULID0000000000',
  phase: 'separate',
  view: null,
}

const view: ViewSnapshot = {
  jobId: sampleJobId,
  positionSeconds: 12.5,
  loopStart: 10,
  loopEnd: 35,
  zoom: 2.25,
  scrollSeconds: 4,
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('session snapshot round trip', () => {
  it('reads back what it wrote', () => {
    writeSessionSnapshot(snapshot)
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('stores identifiers only — never a record', () => {
    writeSessionSnapshot(snapshot)

    const raw = sessionStorage.getItem(SESSION_STORAGE_KEY) ?? ''
    expect(Object.keys(JSON.parse(raw) as object).sort()).toEqual([
      'audioId',
      'jobId',
      'phase',
      'view',
    ])
    // The fields a cached record would have brought with it. A `Job` that
    // survives a reload races the event stream on the way back in, which is
    // the failure features 017 and 031 already paid for.
    for (const field of ['state', 'result', 'metrics', 'progress', 'stems']) {
      expect(raw).not.toContain(field)
    }
  })

  it('reports an empty snapshot when nothing is stored', () => {
    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
  })

  it('removes the key rather than storing an empty snapshot', () => {
    writeSessionSnapshot(snapshot)
    writeSessionSnapshot({
      jobId: null,
      audioId: null,
      phase: 'select',
      view: null,
    })

    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
  })

  it('clears the stored snapshot', () => {
    writeSessionSnapshot(snapshot)
    clearSessionSnapshot()

    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
  })

  it('treats a snapshot with only a phase as nothing to restore', () => {
    expect(
      isEmptySessionSnapshot({
        jobId: null,
        audioId: null,
        phase: 'inspect',
        view: null,
      }),
    ).toBe(true)
    expect(isEmptySessionSnapshot(snapshot)).toBe(false)
  })

  it('treats a snapshot with only a view as something worth restoring', () => {
    // `writeViewSnapshot` can be the first writer to touch a fresh store —
    // its read-modify-write starts from whatever is already on disk, which
    // may be nothing. The view it just wrote must not be discarded as if it
    // were empty.
    expect(
      isEmptySessionSnapshot({
        jobId: null,
        audioId: null,
        phase: null,
        view,
      }),
    ).toBe(false)
  })
})

describe('session snapshot validation', () => {
  it('ignores a payload that is not JSON', () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, 'not json at all')
    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
  })

  it('ignores a payload that is not an object', () => {
    sessionStorage.setItem(SESSION_STORAGE_KEY, '"a string"')
    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
  })

  it('drops fields of the wrong type rather than trusting them', () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ jobId: 42, audioId: '', phase: 'separate' }),
    )
    expect(readSessionSnapshot()).toEqual({
      jobId: null,
      audioId: null,
      phase: 'separate',
      view: null,
    })
  })

  it('drops a phase that is not a known workflow phase', () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ ...snapshot, phase: 'transcribe' }),
    )
    expect(readSessionSnapshot()).toEqual({ ...snapshot, phase: null })
  })
})

describe('view snapshot (feature 066)', () => {
  it('round-trips a snapshot that carries a view', () => {
    writeSessionSnapshot({ ...snapshot, view })
    expect(readSessionSnapshot()).toEqual({ ...snapshot, view })
  })

  it('round-trips a snapshot with no view', () => {
    writeSessionSnapshot(snapshot)
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('restores a record without a view field — jobId/audioId/phase, no `view` key at all', () => {
    // Not a *real* v1 record: an actual pre-066 payload lives under the
    // superseded `straticate.session.v1` key and is never read under this
    // one (silently ignored, per the module docstring). This is a v2-shaped
    // payload that simply omits the optional `view` field, which is the
    // shape `readSessionSnapshot` has to tolerate the same way it tolerates
    // a malformed one.
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({
        jobId: snapshot.jobId,
        audioId: snapshot.audioId,
        phase: snapshot.phase,
      }),
    )
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('tolerates a view of the wrong shape, restoring the rest of the snapshot', () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ ...snapshot, view: { jobId: view.jobId } }),
    )
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('tolerates a view that is not an object', () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ ...snapshot, view: 'soon' }),
    )
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('tolerates a loop bound with no matching partner', () => {
    sessionStorage.setItem(
      SESSION_STORAGE_KEY,
      JSON.stringify({ ...snapshot, view: { ...view, loopEnd: null } }),
    )
    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('tolerates a numeric field that parsed to Infinity', () => {
    // `JSON.parse` accepts a raw `1e999` in the source text and turns it into
    // `Infinity` — there is no way to *write* such a value through this
    // module's own `JSON.stringify` (`Infinity` serializes to `null`), but a
    // hand-edited or otherwise foreign payload can still contain the literal.
    // This is `optionalFiniteNumber`'s one reachable case:
    // `typeof value === 'number'` is already true for it, so only the
    // `Number.isFinite` half of the guard is what saves it.
    const raw = JSON.stringify({
      ...snapshot,
      view: { ...view, positionSeconds: '__PROBE__' },
    }).replace('"__PROBE__"', '1e999')
    sessionStorage.setItem(SESSION_STORAGE_KEY, raw)

    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('writeViewSnapshot updates the view without touching the identifiers', () => {
    writeSessionSnapshot(snapshot)
    writeViewSnapshot(view)

    expect(readSessionSnapshot()).toEqual({ ...snapshot, view })
  })

  it('writeViewSnapshot(null) clears the view, keeping the identifiers', () => {
    writeSessionSnapshot({ ...snapshot, view })
    writeViewSnapshot(null)

    expect(readSessionSnapshot()).toEqual(snapshot)
  })

  it('drops a view recorded for a different job — the caller matches jobIds', () => {
    // persistence.ts stores exactly what it is given; it is `stemSession.tsx`
    // that refuses to apply a view whose `jobId` does not match the job being
    // restored (see its module docstring). This just pins that the module
    // itself keeps the mismatched view intact rather than guessing — the
    // consumer is the one with the authoritative `jobId` to compare against.
    const otherJobView: ViewSnapshot = { ...view, jobId: 'a-different-job' }
    writeSessionSnapshot({ ...snapshot, view: otherJobView })

    const restored = readSessionSnapshot()
    expect(restored.view?.jobId).toBe('a-different-job')
    expect(restored.view?.jobId).not.toBe(restored.jobId)
  })
})

describe('session snapshot when storage is unavailable', () => {
  it('reads an empty snapshot and swallows writes when there is no storage', () => {
    vi.stubGlobal('sessionStorage', undefined)

    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
    expect(() => {
      writeSessionSnapshot(snapshot)
    }).not.toThrow()
    expect(() => {
      clearSessionSnapshot()
    }).not.toThrow()
  })

  it('swallows a store whose every method throws', () => {
    vi.stubGlobal('sessionStorage', throwingStorage)

    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
    expect(() => {
      writeSessionSnapshot(snapshot)
    }).not.toThrow()
    expect(() => {
      writeSessionSnapshot(emptySessionSnapshot)
    }).not.toThrow()
    expect(() => {
      clearSessionSnapshot()
    }).not.toThrow()
  })

  it('swallows a store that throws on property access', () => {
    const restore = blockStorageProperty()
    try {
      expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
      expect(() => {
        writeSessionSnapshot(snapshot)
      }).not.toThrow()
      expect(() => {
        clearSessionSnapshot()
      }).not.toThrow()
    } finally {
      restore()
    }
  })
})
