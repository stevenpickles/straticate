/**
 * M1's workflow, end to end, in a browser, against the fake separator:
 *
 *   upload → configure → separate → progress → telemetry → complete
 *   → inspect → playback controls → export → a route back out
 *
 * The tests share one page and one job, in declaration order
 * (`describe.serial`), because they are stages of a single run rather than
 * independent cases: re-uploading and re-separating for each would multiply
 * the suite's runtime for no coverage. A failure fails the rest of the block,
 * which is the honest outcome — the stages depend on each other.
 */
import { statSync } from 'node:fs'
import { basename } from 'node:path'
import type { Page } from '@playwright/test'
import { FIXTURES } from './environment'
import {
  Workflow,
  expect,
  fourStemMode,
  installSocketTracking,
  recordProgressEvents,
  test,
  type JobRecord,
  type ProgressEvent,
  type SeparationMode,
} from './app'

// The long fixture, deliberately: twelve chunks give every stage of the run
// (progress, telemetry, a live Cancel) seconds of overlap with the
// assertions about them, on a fast laptop and on a loaded CI runner alike.
// Nothing here waits for a duration, but a job that is over before the first
// assertion runs would make the tests meaningless.
const fixture = FIXTURES.long

test.describe.serial('a separation, end to end', () => {
  let page: Page
  let workflow: Workflow
  let mode: SeparationMode
  let job: JobRecord
  let progressEvents: ProgressEvent[]

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage()
    await installSocketTracking(page)
    progressEvents = recordProgressEvents(page)
    workflow = new Workflow(page)
    mode = await fourStemMode(page.request)
  })

  test.afterAll(async () => {
    await page.close()
  })

  test('uploads a file and starts the four-stem separation', async () => {
    await workflow.open()
    await workflow.uploadWithPicker(fixture.path)
    await expect(workflow.phase).toHaveText('Configure')

    await workflow.choose(mode)
    job = await workflow.startSeparation()

    await expect(workflow.phase).toHaveText('Separate')
    expect(job.state, 'a new job starts queued').toBe('queued')
  })

  test('reports progress as real chunk counts while it runs', async () => {
    // `chunks_total` is `ceil(duration / 5 s)` of the audio that was actually
    // uploaded, so asserting the denominator asserts that the progress bar is
    // driven by the work and not by a clock.
    await expect(workflow.chunks).toHaveText(
      new RegExp(`^\\d+ / ${String(fixture.chunks)}$`),
    )
    await expect(
      workflow.progress.getByRole('progressbar', {
        name: 'Separation progress',
      }),
    ).toBeVisible()
    // Still running, so the escape hatch on offer is cancellation.
    await expect(workflow.cancelButton).toBeVisible()
  })

  test('populates the telemetry panel', async () => {
    await expect(workflow.telemetry).toBeVisible()
    for (const group of ['Model', 'Device', 'Processing']) {
      await expect(
        workflow.telemetry.getByRole('heading', { name: group }),
      ).toBeVisible()
    }
    const chunks = workflow.telemetry
      .locator('.telemetry-field')
      .filter({ hasText: 'Chunks' })
      .locator('.telemetry-value')
    await expect(chunks).toHaveText(
      new RegExp(`^\\d+ of ${String(fixture.chunks)}$`),
    )
    await expect(
      workflow.telemetry
        .locator('.telemetry-field')
        .filter({ hasText: 'Real-Time Factor' }),
    ).toBeVisible()
  })

  test('completes, and every chunk was accounted for', async () => {
    await expect(workflow.stage).toHaveText('Completed')
    await expect(workflow.progress).toContainText(
      `Separation complete — ${String(mode.stems.length)} stems are ready.`,
    )
    await expect(workflow.viewResultsButton).toBeVisible()
    await expect(workflow.cancelButton).toHaveCount(0)

    // The separator reports once before the first chunk (`0 / n`, "starting")
    // and once after each chunk, so the whole sequence is `0 … n` with no gap
    // and no repeat. A timer could not produce that, and neither could a UI
    // that interpolated between samples.
    const forThisJob = progressEvents.filter((event) => event.jobId === job.id)
    expect(
      forThisJob.map((event) => event.chunksCompleted),
      'every chunk was reported exactly once, in order',
    ).toEqual(Array.from({ length: fixture.chunks + 1 }, (_, index) => index))
    expect(
      new Set(forThisJob.map((event) => event.chunksTotal)),
      'the chunk total never moved',
    ).toEqual(new Set([fixture.chunks]))
  })

  test('lists the stems with working playback controls', async () => {
    await workflow.viewResultsButton.click()
    await expect(workflow.phase).toHaveText('Inspect')

    // The player renders what the backend produced, which is what the mode's
    // catalog entry promised.
    await expect(workflow.player.locator('.stem-player-stem-name')).toHaveText([
      ...mode.stems,
    ])

    const play = workflow.player.getByRole('button', { name: 'Play' })
    await expect(play).toBeEnabled()

    const seek = workflow.player.getByRole('slider', { name: 'Seek' })
    await seek.fill('15')
    await seek.blur()
    await expect(workflow.player.locator('.stem-player-time')).toContainText(
      '0:15',
    )

    const first = mode.stems[0] ?? ''
    const mute = workflow.player.getByRole('button', { name: `Mute ${first}` })
    const solo = workflow.player.getByRole('button', { name: `Solo ${first}` })
    await mute.click()
    await expect(mute).toHaveAttribute('aria-pressed', 'true')
    await solo.click()
    await expect(solo).toHaveAttribute('aria-pressed', 'true')

    await play.click()
    const pause = workflow.player.getByRole('button', { name: 'Pause' })
    await expect(pause).toBeVisible()
    await pause.click()
    await expect(play).toBeVisible()
  })

  test('exports the stems as a real download', async () => {
    // FLAC by choice: it exercises the format picker, and a lossless encode
    // of four minute-long stems is a fraction of the 24-bit WAV zip to build
    // and to hand to the browser. The expected filename is read back from the
    // control rather than written out, so no format ID is hardcoded here.
    const picker = workflow.exportPanel.getByLabel('Format')
    await picker.selectOption({ label: 'FLAC' })
    const format = await picker.inputValue()

    const downloaded = page.waitForEvent('download')
    await workflow.exportPanel.getByRole('button', { name: 'Export' }).click()
    const download = await downloaded

    expect(download.suggestedFilename()).toBe(`${job.id}-${format}.zip`)
    const saved = await download.path()
    expect(
      statSync(saved).size,
      'the download has bytes in it',
    ).toBeGreaterThan(0)
    await expect(workflow.exportPanel).toContainText(
      `Downloaded ${job.id}-${format}.zip.`,
    )
  })

  test('offers a route back out of the inspect phase', async () => {
    // Defect 2: "View results" used to be one-way, and nothing in `inspect`
    // led anywhere. A phase with no exit is a dead end whatever it renders,
    // so this asserts the way out exists *and* that taking it lands
    // somewhere the user can act.
    await workflow.player
      .getByRole('button', { name: 'Start another separation' })
      .click()

    await expect(workflow.phase).toHaveText('Configure')
    await expect(workflow.summary.getByRole('heading')).toHaveText(
      basename(fixture.path),
    )
    await expect(
      workflow.options.getByRole('button', { name: 'Start separation' }),
    ).toBeEnabled()
  })
})
