/**
 * Removes the run directory: the generated fixtures, every upload the suite
 * made, every job output the backend wrote and its export cache. A run leaves
 * nothing behind.
 *
 * A failure here is reported, never thrown: the tests have already passed or
 * failed by now, and a Windows file handle that has not been released yet is
 * not a reason to fail a green run. The next run starts by removing the
 * directory again.
 */
import { rmSync } from 'node:fs'
import { RUN_DIR } from './environment'

export default function globalTeardown(): void {
  try {
    rmSync(RUN_DIR, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 200,
    })
  } catch (error) {
    console.warn(`Could not remove the E2E run directory ${RUN_DIR}:`, error)
  }
}
