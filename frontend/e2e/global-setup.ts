/**
 * Builds the run directory and generates the audio fixtures with FFmpeg.
 *
 * Audio is never committed (AGENTS.md; DEVELOPMENT.md's "Audio fixtures are
 * generated … never commit copyrighted or large audio"), so the suite makes
 * its own: a stereo sine mixture whose two channels differ, which is enough
 * for ffprobe to report real metadata, for the fake separator to produce
 * distinct stems, and for the browser to decode and play them.
 *
 * Runs once per `playwright test` invocation, before any worker starts.
 */
import { spawnSync } from 'node:child_process'
import { mkdirSync, rmSync } from 'node:fs'
import { DATA_DIR, FIXTURE_DIR, FIXTURES, RUN_DIR } from './environment'
import type { AudioFixture } from './environment'

/** Run FFmpeg, turning any failure into an actionable message. */
function ffmpeg(args: readonly string[]): void {
  const result = spawnSync('ffmpeg', args, { encoding: 'utf8' })
  if (result.error !== undefined) {
    throw new Error(
      `The E2E tier generates its audio fixtures with FFmpeg, which is not on PATH. ` +
        `Install it (DEVELOPMENT.md, Prerequisites) and run the suite again. ` +
        `Cause: ${result.error.message}`,
    )
  }
  if (result.status !== 0) {
    throw new Error(
      `ffmpeg exited with ${String(result.status)} while generating a fixture:\n${result.stderr}`,
    )
  }
}

/** Generate one stereo WAV fixture: 440 Hz left, 277 Hz right. */
function generate(audio: AudioFixture): void {
  const duration = String(audio.seconds)
  ffmpeg([
    '-y',
    '-loglevel',
    'error',
    '-f',
    'lavfi',
    '-i',
    `sine=frequency=440:sample_rate=44100:duration=${duration}`,
    '-f',
    'lavfi',
    '-i',
    `sine=frequency=277:sample_rate=44100:duration=${duration}`,
    '-filter_complex',
    '[0:a][1:a]amerge=inputs=2[stereo]',
    '-map',
    '[stereo]',
    '-ac',
    '2',
    '-c:a',
    'pcm_s16le',
    audio.path,
  ])
}

export default function globalSetup(): void {
  // A previous run that was killed before its teardown leaves this behind;
  // starting from an empty directory is what makes runs independent.
  rmSync(RUN_DIR, { recursive: true, force: true, maxRetries: 5 })
  mkdirSync(DATA_DIR, { recursive: true })
  mkdirSync(FIXTURE_DIR, { recursive: true })
  for (const audio of Object.values(FIXTURES)) {
    generate(audio)
  }
}
