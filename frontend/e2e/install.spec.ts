/**
 * The first thing a fresh checkout sees: a quality tier whose model weights
 * are not on disk.
 *
 * Feature 032 stopped offering the development fixtures as quality tiers —
 * correctly, because pressing "Start separation" on the defaults used to hand
 * back comb-filtered fixture audio as a separation. The accepted cost was that
 * a default server offers exactly one mode, one tier, and an 870 MiB download
 * standing between the user and any output at all. This spec is the proof that
 * the cost is now paid: the app says what is missing, says how big it is, and
 * refuses to start with a reason.
 *
 * **Nothing here downloads any weights.** The suite's backend runs with the
 * development fixtures *on* (`playwright.config.ts` explains why), so two
 * things are scripted with `page.route`, the way `resync.spec.ts` scripts a
 * stale job snapshot:
 *
 * 1. `GET /separation-modes` is filtered to what a **default** server would
 *    serve — every tier backed by a `development_only` model removed, and any
 *    mode thereby emptied dropped. The filter is derived from `GET /models`,
 *    so nothing about the catalog is hardcoded here either (AGENTS.md
 *    principle 6): it is the same rule `ModelCatalog` applies, applied to the
 *    same data.
 * 2. `GET /models/{id}` and `POST /models/{id}/install` are scripted through
 *    `available → downloading → installed`. The real install endpoint is never
 *    reached, so CI fetches nothing.
 *
 * And no fixed sleeps: every wait is the DOM reaching a state the script put
 * it in.
 */
import type { Locator, Page } from '@playwright/test'
import { FIXTURES } from './environment'
import {
  Workflow,
  defaultServerModes,
  expect,
  listModels,
  modelIdOf,
  separationModes,
  test,
} from './app'
import type { ModelRecord } from './app'

const fixture = FIXTURES.tiny

/**
 * Size of the scripted weights artifact. 870 MiB — the size of the real
 * `vocals-hq-001` download — which `formatFileSize` renders as `870 MB`, so
 * the spec can assert the exact string a user reads.
 */
const TOTAL_BYTES = 870 * 1024 * 1024
const TOTAL_LABEL = '870 MB'

/** An `installation` block in one of its four states. */
function installation(
  state: 'available' | 'downloading' | 'installed',
  progress: number | null = null,
): NonNullable<ModelRecord['installation']> {
  return {
    state,
    requires_download: true,
    total_bytes: TOTAL_BYTES,
    downloaded_bytes:
      progress === null ? null : Math.round(progress * TOTAL_BYTES),
    progress,
    error: null,
  }
}

/** The install panel and the controls this spec drives. */
class ConfigureStep {
  readonly page: Page
  readonly workflow: Workflow

  constructor(page: Page) {
    this.page = page
    this.workflow = new Workflow(page)
  }

  get weights(): Locator {
    return this.page.getByRole('region', { name: 'Model weights' })
  }

  get installButton(): Locator {
    return this.weights.getByRole('button', { name: 'Install model' })
  }

  get downloadProgress(): Locator {
    return this.weights.getByRole('progressbar', {
      name: 'Model download progress',
    })
  }

  get startButton(): Locator {
    return this.workflow.options.getByRole('button', {
      name: 'Start separation',
    })
  }

  /** The sentence "Start separation" points at with `aria-describedby`. */
  async startReason(): Promise<Locator> {
    const id = await this.startButton.getAttribute('aria-describedby')
    expect(id, 'a disabled Start says why it is disabled').not.toBeNull()
    return this.page.locator(`#${String(id)}`)
  }
}

test('a fresh checkout is told what it needs, and cannot start without it', async ({
  page,
  request,
}) => {
  const step = new ConfigureStep(page)
  const modes = await defaultServerModes(request)
  const mode = modes[0]
  const tier = mode?.quality_options[0]
  expect(tier, 'the default server offers a tier to install').toBeDefined()
  const modelId = tier?.model_id ?? ''
  // The real catalogue entry, with only its `installation` block scripted —
  // so the panel names the model the backend actually offers.
  const record = (await listModels(request)).find(
    (candidate) => candidate.id === modelId,
  )
  expect(record, 'the tier names a model the catalog serves').toBeDefined()

  // What a default server serves.
  await page.route(
    (url) => url.pathname === '/api/v1/separation-modes',
    async (route) => {
      await route.fulfill({ json: modes })
    },
  )

  // The model's own state, scripted end to end: `available` until the install
  // is requested, then two polled progress samples, then `installed`. Nothing
  // is downloaded — the real install route is never reached.
  const progressScript = [0.25, 0.6]
  let installRequests = 0
  let readsAfterInstall = 0
  let installStarted = false

  await page.route(
    (url) => modelIdOf(url) !== null,
    async (route) => {
      const url = new URL(route.request().url())
      if (modelIdOf(url) !== modelId) {
        await route.fallback()
        return
      }
      const model: ModelRecord = record ?? {
        id: modelId,
        display_name: modelId,
        development_only: false,
      }
      if (url.pathname.endsWith('/install')) {
        installRequests += 1
        installStarted = true
        await route.fulfill({
          status: 202,
          json: { ...model, installation: installation('downloading', 0) },
        })
        return
      }
      if (!installStarted) {
        await route.fulfill({
          json: { ...model, installation: installation('available') },
        })
        return
      }
      const sample = progressScript[readsAfterInstall]
      readsAfterInstall += 1
      await route.fulfill({
        json: {
          ...model,
          installation:
            sample === undefined
              ? installation('installed', 1)
              : installation('downloading', sample),
        },
      })
    },
  )

  await step.workflow.open()
  await step.workflow.uploadWithPicker(fixture.path)
  await expect(step.workflow.phase).toHaveText('Configure')

  // 1. The affordance is there, it names the model, and it names the size.
  await expect(step.weights).toBeVisible()
  await expect(step.weights).toContainText(record?.display_name ?? modelId)
  await expect(step.weights).toContainText(TOTAL_LABEL)
  await expect(step.installButton).toBeEnabled()

  // 2. Start is refused, with a reason that is visible and announced.
  await expect(step.startButton).toBeDisabled()
  await expect(await step.startReason()).toBeVisible()
  await expect(await step.startReason()).toContainText(/weights/i)

  // 3. Installing shows the backend's own progress.
  await step.installButton.click()
  await expect(step.downloadProgress).toHaveAttribute('aria-valuenow', '25')
  // Still refused while it runs, and still with a reason.
  await expect(step.startButton).toBeDisabled()
  await expect(await step.startReason()).toBeVisible()
  // The rest of the configure step is untouched by the download.
  await expect(
    step.workflow.options.getByLabel(mode?.display_name ?? '', {
      exact: true,
    }),
  ).toBeEnabled()

  // 4. It settles, and only then does Start become pressable.
  await expect(step.weights).toContainText('Model weights installed')
  await expect(step.startButton).toBeEnabled()
  await expect(step.startButton).not.toHaveAttribute('aria-describedby')

  expect(installRequests, 'exactly one install was requested').toBe(1)
  expect(
    readsAfterInstall,
    'progress came from polling the model, not from one answer',
  ).toBeGreaterThanOrEqual(progressScript.length + 1)
})

test('a tier whose model needs no download shows none of it', async ({
  page,
  request,
}) => {
  const step = new ConfigureStep(page)
  const models = await listModels(request)
  const fixtures = new Set(
    models.filter((model) => model.development_only).map((model) => model.id),
  )
  const mode = (await separationModes(request)).find(
    (candidate) =>
      candidate.quality_options.some((option) =>
        fixtures.has(option.model_id),
      ) &&
      candidate.quality_options.some(
        (option) => !fixtures.has(option.model_id),
      ),
  )
  expect(
    mode,
    'the suite catalog has a mode offering both a fixture tier and a real one',
  ).toBeDefined()
  if (mode === undefined) {
    throw new Error('unreachable')
  }

  const builtIn = mode.quality_options.find((option) =>
    fixtures.has(option.model_id),
  )
  const downloadable = mode.quality_options.find(
    (option) => !fixtures.has(option.model_id),
  )

  // The downloadable model is scripted `available` so this spec does not
  // depend on whether the machine running it happens to have the weights.
  await page.route(
    (url) => modelIdOf(url) === downloadable?.model_id,
    async (route) => {
      await route.fulfill({
        json: {
          id: downloadable?.model_id,
          display_name: downloadable?.display_name,
          development_only: false,
          installation: installation('available'),
        },
      })
    },
  )

  await step.workflow.open()
  await step.workflow.uploadWithPicker(fixture.path)
  await expect(step.workflow.phase).toHaveText('Configure')

  // The tier backed by a built-in separator: no panel, nothing in the way.
  await step.workflow.options
    .getByLabel(builtIn?.display_name ?? '', { exact: true })
    .check()
  await expect(step.weights).toHaveCount(0)
  await expect(step.startButton).toBeEnabled()

  // Switching to the tier that needs weights brings the affordance back —
  // and switching away takes it with it, mid-configure, with no reload.
  await step.workflow.options
    .getByLabel(downloadable?.display_name ?? '', { exact: true })
    .check()
  await expect(step.weights).toContainText(TOTAL_LABEL)
  await expect(step.startButton).toBeDisabled()

  await step.workflow.options
    .getByLabel(builtIn?.display_name ?? '', { exact: true })
    .check()
  await expect(step.weights).toHaveCount(0)
  await expect(step.startButton).toBeEnabled()
})

test('a job refused for missing weights becomes an install that finishes', async ({
  page,
  request,
}) => {
  const step = new ConfigureStep(page)
  const modes = await defaultServerModes(request)
  const mode = modes[0]
  const modelId = mode?.quality_options[0]?.model_id ?? ''
  const record = (await listModels(request)).find(
    (candidate) => candidate.id === modelId,
  )

  await page.route(
    (url) => url.pathname === '/api/v1/separation-modes',
    async (route) => {
      await route.fulfill({ json: modes })
    },
  )

  // The weights vanish between the check and the job: the record says
  // `installed` until the job is refused, and the truth arrives on the re-read
  // that refusal triggers.
  let refused = false
  let installStarted = false
  let readsAfterInstall = 0
  const progressScript = [0.3, 0.7]

  await page.route(
    (url) => modelIdOf(url) === modelId,
    async (route) => {
      const url = new URL(route.request().url())
      const model: ModelRecord = record ?? {
        id: modelId,
        display_name: modelId,
        development_only: false,
      }
      if (url.pathname.endsWith('/install')) {
        installStarted = true
        await route.fulfill({
          status: 202,
          json: { ...model, installation: installation('downloading', 0) },
        })
        return
      }
      if (!refused) {
        await route.fulfill({
          json: { ...model, installation: installation('installed', 1) },
        })
        return
      }
      if (!installStarted) {
        await route.fulfill({
          json: { ...model, installation: installation('available') },
        })
        return
      }
      const sample = progressScript[readsAfterInstall]
      readsAfterInstall += 1
      await route.fulfill({
        json: {
          ...model,
          installation:
            sample === undefined
              ? installation('installed', 1)
              : installation('downloading', sample),
        },
      })
    },
  )

  // The documented answer, scripted rather than provoked: on a machine that
  // *does* have these weights the real backend would happily start an 870 MiB
  // model's separation, which is not what this spec is about.
  await page.route(
    (url) => url.pathname === '/api/v1/jobs',
    async (route) => {
      if (route.request().method() !== 'POST') {
        await route.fallback()
        return
      }
      refused = true
      await route.fulfill({
        status: 409,
        json: {
          error: {
            code: 'model_weights_missing',
            message: `Model '${modelId}' is catalogued but its weights are not installed.`,
            detail: { model_id: modelId },
          },
        },
      })
    },
  )

  await step.workflow.open()
  await step.workflow.uploadWithPicker(fixture.path)
  await expect(step.workflow.phase).toHaveText('Configure')

  // The record claims the weights are there, so Start is offered and there is
  // nothing to install.
  await expect(step.startButton).toBeEnabled()
  await expect(step.weights).toContainText('Model weights installed')
  await expect(step.installButton).toHaveCount(0)

  // The job is refused — and that is rendered as the install, not as a raw
  // error the user cannot act on.
  await step.startButton.click()
  await expect(step.weights).toContainText('weights are not installed')
  await expect(step.installButton).toBeEnabled()
  await expect(step.startButton).toBeDisabled()

  // Pressing it shows the download. This is the flow the whole feature exists
  // for, and the one where a stale job error used to hide 870 MB of progress.
  await step.installButton.click()
  await expect(step.downloadProgress).toHaveAttribute('aria-valuenow', '30')
  await expect(step.weights).toContainText(TOTAL_LABEL)
  await expect(
    step.weights.getByRole('alert'),
    'the refusal is gone: the server has spoken more recently than it did',
  ).toHaveCount(0)
  await expect(
    step.installButton,
    'and no second install is offered beside a running one',
  ).toHaveCount(0)

  await expect(step.weights).toContainText('Model weights installed')
  await expect(step.startButton).toBeEnabled()
})
