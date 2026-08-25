import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  SESSION_STORAGE_KEY,
  clearSessionSnapshot,
  emptySessionSnapshot,
  isEmptySessionSnapshot,
  readSessionSnapshot,
  writeSessionSnapshot,
  type SessionSnapshot,
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

const snapshot: SessionSnapshot = {
  jobId: '01SAMPLEJOBULID00000000000',
  audioId: '01SAMPLEAUDIOULID0000000000',
  phase: 'separate',
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
    writeSessionSnapshot({ jobId: null, audioId: null, phase: 'select' })

    expect(sessionStorage.getItem(SESSION_STORAGE_KEY)).toBeNull()
  })

  it('clears the stored snapshot', () => {
    writeSessionSnapshot(snapshot)
    clearSessionSnapshot()

    expect(readSessionSnapshot()).toEqual(emptySessionSnapshot)
  })

  it('treats a snapshot with only a phase as nothing to restore', () => {
    expect(
      isEmptySessionSnapshot({ jobId: null, audioId: null, phase: 'inspect' }),
    ).toBe(true)
    expect(isEmptySessionSnapshot(snapshot)).toBe(false)
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
