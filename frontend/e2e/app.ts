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
 */
import { readFile } from 'node:fs/promises'
import { basename } from 'node:path'
import {
  expect,
  test as base,
  type APIRequestContext,
  type Locator,
  type Page,
} from '@playwright/test'

declare global {
  interface Window {
    /** Every `WebSocket` the page has constructed, newest last. */
    __straticateSockets?: WebSocket[]
  }
}

/**
 * The base `test`, with every page recording the sockets it opens.
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
      await use()
    },
    { auto: true },
  ],
})

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
