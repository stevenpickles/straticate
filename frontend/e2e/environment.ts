/**
 * Where the E2E tier runs and what it runs against.
 *
 * Everything here is derived from the environment so a developer, a second
 * checkout and CI can all run the suite without colliding, and so nothing the
 * run creates lands inside the repository:
 *
 * - the backend listens on a **dedicated port**, never 8000, so a developer's
 *   own server keeps running while the suite drives its own;
 * - the backend's `STRATICATE_DATA_DIR` is a directory under the system temp
 *   directory, created by `global-setup.ts` and removed by
 *   `global-teardown.ts`, so uploads, job outputs and export caches never
 *   touch `backend/data/`;
 * - the audio fixtures are **generated with FFmpeg at setup time** into that
 *   same directory. Audio is never committed (AGENTS.md, DEVELOPMENT.md).
 *
 * This module is imported by `playwright.config.ts`, by the global setup and
 * teardown, and by the specs, so all four agree on one set of paths. It reads
 * the environment but never mutates it, and creates no directories itself:
 * every value is a pure function of `process.env`, which is what lets the
 * config, the runner process and every worker process derive the same paths.
 */
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** Read a positive integer from the environment, or fall back. */
function port(name: string, fallback: number): number {
  const raw = process.env[name]
  if (raw === undefined || raw === '') {
    return fallback
  }
  const value = Number(raw)
  if (!Number.isInteger(value) || value <= 0 || value > 65535) {
    throw new Error(`${name} must be a TCP port number, got ${raw}`)
  }
  return value
}

/**
 * Port the suite's backend listens on. Deliberately not 8000: the suite must
 * never talk to — or fail to start because of — a developer's own server.
 * Override with `STRATICATE_E2E_BACKEND_PORT`.
 */
export const BACKEND_PORT = port('STRATICATE_E2E_BACKEND_PORT', 8123)

/**
 * Port the suite's Vite dev server listens on. Not 5173, for the same reason.
 * Override with `STRATICATE_E2E_FRONTEND_PORT`.
 */
export const FRONTEND_PORT = port('STRATICATE_E2E_FRONTEND_PORT', 5123)

/** Base URL of the suite's backend (only the dev server talks to it directly). */
export const BACKEND_URL = `http://127.0.0.1:${String(BACKEND_PORT)}`

/** Base URL of the app under test; the browser only ever talks to this. */
export const FRONTEND_URL = `http://127.0.0.1:${String(FRONTEND_PORT)}`

/**
 * Scratch directory for one run: the backend's data directory and the
 * generated audio fixtures. Created by the global setup, removed by the
 * global teardown. Override with `STRATICATE_E2E_DIR`.
 */
export const RUN_DIR =
  process.env.STRATICATE_E2E_DIR ?? join(tmpdir(), 'straticate-e2e')

/** `STRATICATE_DATA_DIR` for the backend the suite starts. */
export const DATA_DIR = join(RUN_DIR, 'data')

/** Directory the generated audio fixtures are written to. */
export const FIXTURE_DIR = join(RUN_DIR, 'fixtures')

/**
 * A generated audio fixture: a stereo WAV of `seconds` seconds.
 *
 * Lengths are chosen against the fake separator's chunking (~5 s of audio per
 * chunk, so a 30 s fixture is a six-chunk job): long enough that progress,
 * telemetry and cancellation are all observable without a single sleep, short
 * enough that the whole suite stays quick.
 */
export interface AudioFixture {
  /** Absolute path of the generated file. */
  readonly path: string
  /** Duration in seconds, as asked of FFmpeg. */
  readonly seconds: number
  /** Chunks the fake separator will report for it (`ceil(seconds / 5)`). */
  readonly chunks: number
}

/** Seconds of audio the fake separator processes per chunk. */
const SECONDS_PER_CHUNK = 5

function fixture(name: string, seconds: number): AudioFixture {
  return {
    path: join(FIXTURE_DIR, `${name}.wav`),
    seconds,
    chunks: Math.ceil(seconds / SECONDS_PER_CHUNK),
  }
}

/** Every fixture the suite generates, keyed by what it is for. */
export const FIXTURES = {
  /** The workflow fixture: six chunks — progress and telemetry both visible. */
  standard: fixture('mixture-30s', 30),
  /** Longer, so a cancellation lands comfortably mid-run on any machine. */
  long: fixture('mixture-60s', 60),
  /** Tiny, for the drag-and-drop upload test that ships its bytes into the page. */
  tiny: fixture('mixture-2s', 2),
} as const
