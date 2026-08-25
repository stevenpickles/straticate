import { defineConfig, devices } from '@playwright/test'
import {
  BACKEND_PORT,
  BACKEND_URL,
  DATA_DIR,
  FRONTEND_PORT,
  FRONTEND_URL,
} from './e2e/environment'

/**
 * Playwright configuration for the end-to-end tier (feature 030).
 *
 * The suite drives the **real** application — a real backend, a real Vite dev
 * server, a real browser — against the **fake separator**, so it needs no GPU,
 * no model weights and no download (ARCHITECTURE.md §8). It is deliberately
 * separate from Vitest in every way: its own directory (`e2e/`), its own
 * config, its own npm script. `npm test` does not run it and it does not run
 * `npm test`.
 *
 * Both servers are started by Playwright:
 *
 * - the backend on a dedicated port with `STRATICATE_DATA_DIR` pointing at a
 *   temporary directory, so a run is isolated and leaves nothing behind;
 * - the Vite dev server on a dedicated port, told where the backend is via
 *   `STRATICATE_BACKEND_URL` (see `vite.config.ts`), so the browser only ever
 *   talks to one origin and the WebSocket is proxied exactly as in
 *   development.
 *
 * One worker, no parallelism: the backend runs one separation at a time
 * (ARCHITECTURE.md §6), so parallel specs would queue behind each other
 * anyway — serially is simply the honest way to say that.
 */
export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  globalTeardown: './e2e/global-teardown.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: process.env.CI !== undefined,
  // One retry in CI absorbs an infrastructure hiccup (a runner stall while a
  // server boots); it is not a licence for flaky assertions, which is why the
  // suite waits on real conditions and never on a clock.
  retries: process.env.CI !== undefined ? 1 : 0,
  // In CI: annotations on the PR's diff (`github`), a readable log (`list`),
  // and an HTML report the workflow uploads when something fails.
  reporter:
    process.env.CI !== undefined
      ? [['github'], ['list'], ['html', { open: 'never' }]]
      : [['list']],
  // Per test. A separation of the standard fixture is seconds; the ceiling is
  // for a cold CI runner, not for normal operation.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: FRONTEND_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    launchOptions: {
      // The stem player builds a Web Audio graph; without this Chromium
      // suspends the AudioContext until it sees a user gesture it believes
      // in, which has nothing to do with what the tier is testing.
      args: ['--autoplay-policy=no-user-gesture-required'],
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'uv run python -m straticate',
      cwd: '../backend',
      url: `${BACKEND_URL}/api/v1/health`,
      env: {
        STRATICATE_HOST: '127.0.0.1',
        STRATICATE_PORT: String(BACKEND_PORT),
        STRATICATE_DATA_DIR: DATA_DIR,
        STRATICATE_LOG_LEVEL: 'WARNING',
      },
      // Never adopt a server this suite did not start: it would have the
      // wrong data directory, and the run would leave uploads behind in it.
      reuseExistingServer: false,
      stdout: 'ignore',
      stderr: 'pipe',
      timeout: 180_000,
    },
    {
      // `--host 127.0.0.1` is not decoration: Vite's default `localhost` bind
      // resolves to `::1` first on Windows, and then nothing answers on
      // 127.0.0.1 — including Playwright's own readiness probe.
      command: `npm run dev -- --host 127.0.0.1 --port ${String(FRONTEND_PORT)} --strictPort`,
      url: FRONTEND_URL,
      env: { STRATICATE_BACKEND_URL: BACKEND_URL },
      reuseExistingServer: false,
      stdout: 'ignore',
      stderr: 'pipe',
      timeout: 120_000,
    },
  ],
})
