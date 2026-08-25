import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import prettier from 'eslint-config-prettier'

export default tseslint.config(
  // src/api/generated is machine-generated (openapi-typescript); it is
  // excluded from linting and formatting alike (see .prettierignore).
  {
    ignores: [
      'dist',
      'coverage',
      'src/api/generated',
      // Playwright's own output (traces, screenshots, HTML report).
      'test-results',
      'playwright-report',
    ],
  },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
  },
  {
    // State modules intentionally export hooks alongside the provider
    // component; fast refresh does not apply to them.
    files: ['src/state/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
  {
    // The Playwright tier (feature 030) is Node code: it starts servers,
    // reads generated fixtures off disk and drives a browser. Its
    // `page.evaluate` callbacks are browser code, so both sets of globals
    // are legitimately in scope (see tsconfig.e2e.json).
    files: ['e2e/**/*.ts', 'playwright.config.ts'],
    languageOptions: {
      globals: { ...globals.node, ...globals.browser },
    },
  },
  prettier,
)
