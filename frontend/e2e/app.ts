/**
 * Shared vocabulary for the E2E specs: the page objects, the two upload
 * routes, and the small number of real conditions the suite synchronises on.
 *
 * Two rules this module exists to enforce:
 *
 * 1. **No fixed sleeps.** Nothing here waits for a duration. Every wait is a
 *    condition — an element Playwright auto-waits for, an `expect` that polls,
 *    a response that has actually arrived, or a rendered frame. A suite that
 *    sleeps is a suite that is either slow or flaky, and usually both.
 * 2. **Nothing about the catalog is hardcoded.** Modes, quality tiers and stem
 *    names are read from the backend (`GET /separation-modes`) and used to
 *    drive and assert the UI, exactly as the app does (AGENTS.md principle 6).
 * 3. **A failure says what went wrong** (feature 044). A request to `/api`
 *    that never reached the backend surfaces in the DOM as an absence — an
 *    empty stem list, a panel that never filled — and the assertion that
 *    catches it can only report the absence. Every page therefore records its
 *    failed API requests, and a failing test attaches them
 *    ({@link recordFailedApiRequests}).
 */
import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'
import {
  expect,
  test as base,
  type APIRequestContext,
  type Locator,
  type Page,
  type TestInfo,
} from '@playwright/test'

declare global {
  interface Window {
    /** Every `WebSocket` the page has constructed, newest last. */
    __straticateSockets?: WebSocket[]
  }
}

/**
 * The base `test`, with every page recording the sockets it opens and the API
 * requests that never arrived.
 *
 * Feature 016's client owns its socket privately, and nothing in the UI can
 * drop a connection — so the tier reaches for the one seam a browser test
 * has: it wraps `window.WebSocket` before the app loads and keeps the
 * instances. That is what lets a spec sever a live connection and assert what
 * the app does when it comes back (`docs/features/030-playwright-e2e.md`).
 */
export const test = base.extend<{ socketTracking: void }>({
  socketTracking: [
    async ({ page }, use) => {
      await installSocketTracking(page)
      const failures = recordFailedApiRequests(page)
      await use()
      await attachFailedApiRequests(failures, test.info())
    },
    { auto: true },
  ],
})

/** One `/api` request the browser could not complete. */
export interface FailedApiRequest {
  /** Request method. */
  readonly method: string
  /** Path, without the origin — the origin is always the dev server. */
  readonly path: string
  /** Why the browser gave up, as Chromium reported it. */
  readonly failure: string
}

/**
 * Record every request to `/api` the browser failed to complete.
 *
 * Applied automatically to the `page` fixture; call it directly on a page
 * built with `browser.newPage()` and hand the result to
 * {@link attachFailedApiRequests}.
 *
 * This exists because of what feature 044 measured. A request that never
 * reaches the backend leaves the UI showing *nothing* — the stem list stays
 * empty, a panel never fills — so the assertion that trips is one about a
 * missing element, and its message is about the element rather than about the
 * request. The cause is visible only in the dev server's stderr, which is
 * where an hour went before anyone thought to look:
 *
 * ```text
 * [vite] http proxy error: /api/v1/jobs/{id}/result
 * Error: connect ETIMEDOUT 127.0.0.1:8123
 * ```
 *
 * Recording it costs nothing and turns that hour into a line in the report.
 */
export function recordFailedApiRequests(page: Page): FailedApiRequest[] {
  const failures: FailedApiRequest[] = []
  page.on('requestfailed', (request) => {
    const { pathname, search } = new URL(request.url())
    if (!pathname.startsWith('/api/')) {
      return
    }
    failures.push({
      method: request.method(),
      path: `${pathname}${search}`,
      failure: request.failure()?.errorText ?? 'unknown',
    })
  })
  return failures
}

/**
 * Attach the failed API requests to a test that did not get the result it
 * expected.
 *
 * Only on failure, and only when there are any: a passing run says nothing
 * about them, and a run with none attaches nothing.
 */
export async function attachFailedApiRequests(
  failures: readonly FailedApiRequest[],
  info: TestInfo,
): Promise<void> {
  if (failures.length === 0 || info.status === info.expectedStatus) {
    return
  }
  await info.attach('failed API requests', {
    contentType: 'text/plain',
    body: failures
      .map(
        (request) => `${request.method} ${request.path} — ${request.failure}`,
      )
      .join('\n'),
  })
}

/**
 * Record every `WebSocket` the page opens on `window.__straticateSockets`.
 * Applied automatically to the `page` fixture; call it directly on a page
 * built with `browser.newPage()`.
 */
export async function installSocketTracking(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const sockets: WebSocket[] = []
    window.__straticateSockets = sockets
    const NativeWebSocket = window.WebSocket
    class TrackedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols)
        sockets.push(this)
      }
    }
    window.WebSocket = TrackedWebSocket
  })
}

/** One `job_progress` event as it arrived over the wire. */
export interface ProgressEvent {
  readonly jobId: string
  readonly progress: number
  readonly chunksCompleted: number
  readonly chunksTotal: number
}

/**
 * Collect every `job_progress` event the page receives, into an array that
 * fills as the run goes.
 *
 * The DOM shows the newest sample; this keeps the whole sequence, which is
 * what makes "progress is real work" assertable: chunk counts that climb one
 * at a time to a total fixed by the audio's length are not a timer
 * (AGENTS.md principle 3).
 */
export function recordProgressEvents(page: Page): ProgressEvent[] {
  const events: ProgressEvent[] = []
  page.on('websocket', (socket) => {
    socket.on('framereceived', (frame) => {
      if (typeof frame.payload !== 'string') {
        return
      }
      let payload: unknown
      try {
        payload = JSON.parse(frame.payload)
      } catch {
        return
      }
      const event = payload as Partial<Record<string, unknown>>
      if (event.type !== 'job_progress') {
        return
      }
      events.push({
        jobId: String(event.job_id),
        progress: Number(event.progress),
        chunksCompleted: Number(event.chunks_completed),
        chunksTotal: Number(event.chunks_total),
      })
    })
  })
  return events
}

export { expect }

/** A separation mode as the backend serves it. */
export interface SeparationMode {
  readonly id: string
  readonly display_name: string
  readonly stems: readonly string[]
  readonly quality_options: readonly {
    readonly id: string
    readonly display_name: string
    readonly model_id: string
  }[]
}

/** A job record as the backend serves it (only the fields the suite reads). */
export interface JobRecord {
  readonly id: string
  readonly state: string
  readonly progress: number
  readonly result: {
    readonly stems: readonly { readonly name: string }[]
  } | null
  readonly finished_at: string | null
}

/** The separation-mode catalog, straight from the backend. */
export async function separationModes(
  request: APIRequestContext,
): Promise<SeparationMode[]> {
  const response = await request.get('/api/v1/separation-modes')
  expect(response.ok(), 'the backend serves its separation-mode catalog').toBe(
    true,
  )
  return (await response.json()) as SeparationMode[]
}

/** Installation state, as `Model.installation` carries it. */
export interface InstallationRecord {
  state: 'available' | 'downloading' | 'installed' | 'failed'
  requires_download: boolean
  total_bytes: number | null
  downloaded_bytes: number | null
  progress: number | null
  error?: { code: string; message: string } | null
}

/** Licence terms, as `Model.licensing` carries them. */
export interface LicensingRecord {
  code_license?: string | null
  weights_license?: string | null
  redistribution_permitted?: boolean | null
  commercial_use_permitted?: boolean | null
  attribution?: string | null
}

/** A model as the backend serves it (only the fields the suite reads or writes). */
export interface ModelRecord {
  readonly id: string
  readonly display_name: string
  readonly development_only: boolean
  licensing?: LicensingRecord | null
  installation?: InstallationRecord
}

/** The model catalog, straight from the backend. */
export async function listModels(
  request: APIRequestContext,
): Promise<ModelRecord[]> {
  const response = await request.get('/api/v1/models')
  expect(response.ok(), 'the backend serves its model catalog').toBe(true)
  return (await response.json()) as ModelRecord[]
}

/** The model ID a request path names, or `null` for a non-model path. */
export function modelIdOf(url: URL): string | null {
  const match = /^\/api\/v1\/models\/([^/]+)/.exec(url.pathname)
  return match?.[1] === undefined ? null : decodeURIComponent(match[1])
}

/**
 * The separation modes a server with **default** settings would serve: the
 * same catalog, with every development fixture's tier removed and any mode
 * left without a tier dropped (feature 032).
 *
 * Derived from `GET /models`, never hardcoded: it is 032's own predicate
 * applied to the same data, so a catalog change cannot make a spec assert
 * something the app never sees.
 */
export async function defaultServerModes(
  request: APIRequestContext,
): Promise<SeparationMode[]> {
  const models = await listModels(request)
  const fixtures = new Set(
    models.filter((model) => model.development_only).map((model) => model.id),
  )
  expect(
    fixtures.size,
    'the suite runs with the development fixtures enabled, so there are some to hide',
  ).toBeGreaterThan(0)

  const modes = (await separationModes(request))
    .map((mode) => ({
      ...mode,
      quality_options: mode.quality_options.filter(
        (option) => !fixtures.has(option.model_id),
      ),
    }))
    .filter((mode) => mode.quality_options.length > 0)

  expect(
    modes.length,
    'a default server still offers at least one real separation mode',
  ).toBeGreaterThan(0)
  return modes
}

/**
 * The catalog's four-stem mode — the one M1's workflow asks for — resolved
 * from what the backend actually serves rather than from a literal mode ID.
 */
export async function fourStemMode(
  request: APIRequestContext,
): Promise<SeparationMode> {
  const modes = await separationModes(request)
  const mode = modes.find((candidate) => candidate.stems.length === 4)
  expect(mode, 'the catalog serves a four-stem separation mode').toBeDefined()
  // `find` narrows to `SeparationMode | undefined`; the expect above is the
  // assertion, this is the type-level consequence of it.
  if (mode === undefined) {
    throw new Error('unreachable')
  }
  return mode
}

/** The Straticate workflow, as a page object. */
export class Workflow {
  readonly page: Page

  constructor(page: Page) {
    this.page = page
  }

  /** Open the app at the `select` phase. */
  async open(): Promise<void> {
    await this.page.goto('/')
    await expect(this.phase).toHaveText('Select')
  }

  /** The workflow phase the workspace is showing. */
  get phase(): Locator {
    return this.page.locator('.workspace-phase')
  }

  get dropZone(): Locator {
    return this.page.getByRole('region', { name: 'Audio file selection' })
  }

  get summary(): Locator {
    return this.page.getByRole('region', { name: 'Uploaded file' })
  }

  get options(): Locator {
    return this.page.getByRole('region', { name: 'Separation options' })
  }

  get progress(): Locator {
    return this.page.getByRole('region', { name: 'Separation progress' })
  }

  get telemetry(): Locator {
    return this.page.getByRole('region', { name: 'Runtime telemetry' })
  }

  get player(): Locator {
    return this.page.getByRole('region', { name: 'Stem player' })
  }

  get exportPanel(): Locator {
    return this.page.getByRole('region', { name: 'Export' })
  }

  /** The waveform timeline: ruler, lanes, playhead and the seek surface. */
  get timeline(): Locator {
    return this.player.locator('.stem-timeline')
  }

  /**
   * The seek control. Since feature 050 it is the timeline's interaction
   * layer rather than an `<input type="range">` — a `div` with `role="slider"`
   * — so it is still found by role and name, but it is driven with the mouse
   * and the keyboard rather than with `fill()`.
   */
  get seek(): Locator {
    return this.player.getByRole('slider', { name: 'Seek' })
  }

  /** The `m:ss / m:ss` readout under the transport. */
  get playhead(): Locator {
    return this.player.locator('.stem-player-time')
  }

  /**
   * The track strip. Since feature 051 it carries the window as data —
   * `data-zoom` and `data-scroll-seconds` — which is what lets a spec assert
   * in seconds of audio about a view that is no longer the whole file.
   */
  get strip(): Locator {
    return this.player.locator('.stem-timeline-tracks')
  }

  /** The ruler's tick labels, left to right. */
  get ticks(): Locator {
    return this.player.locator('.stem-timeline-tick')
  }

  get zoomIn(): Locator {
    return this.player.getByRole('button', { name: 'Zoom in' })
  }

  get zoomOut(): Locator {
    return this.player.getByRole('button', { name: 'Zoom out' })
  }

  get zoomFit(): Locator {
    return this.player.getByRole('button', { name: 'Zoom to fit' })
  }

  /**
   * The ruler's row. Since feature 053 it is the loop-region control: a drag
   * across it draws a region, a plain click clears one and seeks.
   */
  get ruler(): Locator {
    return this.player.locator('.stem-timeline-ruler-row')
  }

  /** The `Loop m:ss – m:ss` badge, absent when there is no region. */
  get loopBadge(): Locator {
    return this.player.locator('.stem-player-loop-badge')
  }

  get clearLoop(): Locator {
    return this.player.getByRole('button', { name: 'Clear loop' })
  }

  /**
   * Drag across the ruler from one fraction of the strip to another — the
   * gesture that sets a loop region, and the one that must commit exactly one
   * of them.
   *
   * Like {@link Workflow.seekToFraction}, the pixels come from the element's
   * own box, so the spec asserts in seconds of audio. The intermediate move is
   * not cosmetic: a press and a release at two points with nothing in between
   * is a click, and a click on the ruler means the opposite of a drag.
   */
  async dragRuler(from: number, to: number): Promise<void> {
    const box = await this.ruler.boundingBox()
    expect(box, 'the ruler has been laid out').not.toBeNull()
    if (box === null) {
      throw new Error('unreachable')
    }
    const y = box.y + box.height / 2
    await this.page.mouse.move(box.x + box.width * from, y)
    await this.page.mouse.down()
    await this.page.mouse.move(box.x + box.width * ((from + to) / 2), y)
    await this.page.mouse.move(box.x + box.width * to, y)
    await this.page.mouse.up()
  }

  /** The window the timeline is showing, in seconds of audio. */
  async window(): Promise<{ zoom: number; scrollSeconds: number }> {
    const zoom = await this.strip.getAttribute('data-zoom')
    const scrollSeconds = await this.strip.getAttribute('data-scroll-seconds')
    expect(zoom, 'the strip reports its zoom').not.toBeNull()
    expect(scrollSeconds, 'the strip reports its scroll').not.toBeNull()
    return { zoom: Number(zoom), scrollSeconds: Number(scrollSeconds) }
  }

  /**
   * Click the timeline `fraction` of the way along it — the gesture a user
   * makes to jump somewhere, and the one that must produce exactly one seek.
   *
   * The position is read from the element's own box rather than assumed, so
   * the spec asserts in *seconds of audio* and never in pixels.
   */
  async seekToFraction(fraction: number): Promise<void> {
    const box = await this.seek.boundingBox()
    expect(box, 'the timeline has been laid out').not.toBeNull()
    if (box === null) {
      throw new Error('unreachable')
    }
    await this.page.mouse.click(
      box.x + box.width * fraction,
      box.y + box.height / 2,
    )
  }

  /** The stage line of the progress panel (`Separating`, `Completed`, …). */
  get stage(): Locator {
    return this.progress.locator('.separation-progress-stage')
  }

  /** The `chunks_completed / chunks_total` field, present only while running. */
  get chunks(): Locator {
    return this.progress
      .locator('.separation-progress-field')
      .filter({ hasText: 'Chunks' })
      .locator('.separation-progress-value')
  }

  get cancelButton(): Locator {
    return this.progress.getByRole('button', { name: 'Cancel', exact: true })
  }

  get viewResultsButton(): Locator {
    return this.progress.getByRole('button', { name: 'View results' })
  }

  /** Upload through the file picker's `<input type="file">`. */
  async uploadWithPicker(path: string): Promise<void> {
    await this.page.locator('input[type="file"]').setInputFiles(path)
  }

  /**
   * Upload by dropping the file on the drop zone: a real `DataTransfer`
   * carrying the real bytes, dispatched as `dragover` then `drop`, which is
   * the path a mouse takes and the one no component test covers.
   */
  async uploadWithDrop(path: string): Promise<void> {
    const bytes = await readFile(path)
    const handle = await this.page.evaluateHandle(
      ({ base64, name }) => {
        const binary = atob(base64)
        const buffer = new Uint8Array(binary.length)
        for (let index = 0; index < binary.length; index += 1) {
          buffer[index] = binary.charCodeAt(index)
        }
        const transfer = new DataTransfer()
        transfer.items.add(new File([buffer], name, { type: 'audio/wav' }))
        return transfer
      },
      { base64: bytes.toString('base64'), name: basename(path) },
    )
    await this.dropZone.dispatchEvent('dragover', { dataTransfer: handle })
    await this.dropZone.dispatchEvent('drop', { dataTransfer: handle })
    await handle.dispose()
  }

  /** Select a separation mode and one of its quality tiers by display name. */
  async choose(mode: SeparationMode, qualityIndex = 0): Promise<void> {
    const quality = mode.quality_options[qualityIndex]
    expect(quality, 'the mode serves the requested quality tier').toBeDefined()
    await this.options.getByLabel(mode.display_name, { exact: true }).check()
    if (quality !== undefined) {
      await this.options
        .getByLabel(quality.display_name, { exact: true })
        .check()
    }
  }

  /**
   * Start the separation and return the job the backend created. Waiting on
   * the `POST /jobs` response is what gives the specs the job ID they need to
   * talk to the backend directly — and it is a real condition, so nothing
   * here guesses how long creating a job takes.
   */
  async startSeparation(): Promise<JobRecord> {
    const created = this.page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/v1/jobs',
    )
    await this.options.getByRole('button', { name: 'Start separation' }).click()
    const response = await created
    expect(response.status(), 'the backend queues the job').toBe(201)
    return (await response.json()) as JobRecord
  }
}

/**
 * Sever the page's newest WebSocket, which is the app's job event socket.
 *
 * The client treats this as an unexpected close and reconnects with backoff;
 * on `open` it refetches the tracked job over REST (`JobEventBridge`). That
 * refetch is the moment defect 1 lived in, so this is how the suite gets at
 * it.
 */
export async function dropJobSocket(page: Page): Promise<void> {
  const dropped = await page.evaluate(() => {
    const socket = window.__straticateSockets?.at(-1)
    if (socket === undefined) {
      return false
    }
    socket.close(4000, 'e2e: simulated connection drop')
    return true
  })
  expect(dropped, 'the app had a live job event socket to drop').toBe(true)
}

/**
 * Drop the socket and wait for the resync the reconnect triggers — the
 * `GET /jobs/{id}` whose answer is applied on top of everything the events
 * have already delivered.
 */
export async function resyncOverRest(page: Page, jobId: string): Promise<void> {
  const resynced = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === `/api/v1/jobs/${jobId}`,
  )
  await dropJobSocket(page)
  await resynced
  await renderedFrames(page)
}

/**
 * Wait for the browser to paint two frames.
 *
 * This is how the suite says "let anything that was going to happen, happen"
 * without naming a duration: a React state update queued by the response that
 * just arrived is committed within a frame, so two frames later the DOM is
 * either changed or it is not going to be. It is a real condition — the
 * browser's own rendering — not a timer.
 */
export async function renderedFrames(page: Page): Promise<void> {
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => {
            resolve()
          })
        })
      }),
  )
}

/** Fetch a job record straight from the backend. */
export async function fetchJob(
  request: APIRequestContext,
  jobId: string,
): Promise<JobRecord> {
  const response = await request.get(`/api/v1/jobs/${jobId}`)
  expect(response.ok(), `the backend knows job ${jobId}`).toBe(true)
  return (await response.json()) as JobRecord
}
