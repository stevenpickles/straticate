/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Backend the dev server proxies `/api` to. Defaults to the port
 * DEVELOPMENT.md tells a developer to run the backend on; the Playwright E2E
 * tier (feature 030) overrides it so the suite drives its own backend on its
 * own port, against a temporary data directory, without colliding with a
 * developer's running server.
 */
const backendTarget =
  process.env.STRATICATE_BACKEND_URL ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // All backend traffic goes through /api; ws:true also upgrades
      // WebSocket connections (progress/telemetry arrive in later features).
      '/api': {
        target: backendTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    // Vitest owns `src/` only. The Playwright specs live in `e2e/` and are
    // driven by `playwright.config.ts`; without this they would match
    // Vitest's default `**/*.spec.ts` glob and be collected by `npm test`.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
})
