/**
 * What the app does when the connection blinks, and what a page reload does
 * to a run in flight.
 *
 * This is the file that exists because of defect 1. On socket reconnect the
 * app refetches the tracked job over REST — and a REST record is a snapshot
 * that races the event stream. A `getJob` answer prepared *before* a
 * `job_completed` event was applied used to overwrite it, reverting a finished
 * job to `separating`. Because the job really had finished, no further event
 * ever arrived to correct it: the progress bar sat mid-run with a live Cancel
 * button until the page was reloaded.
 *
 * A green unit suite shipped that. It is invisible to component tests because
 * it is not about what a component renders — it is about which of two sources
 * of truth wins when they disagree about a job's *history*.
 *
 * The reload tests below are the other half of the file, and they used to
 * assert the *absence* of a capability: this suite reported that a reload
 * dropped the user back at file selection while the backend job ran on
 * unreachably. Feature 033 made the session survive a reload, so they now
 * assert what it restores — and, just as importantly, what it does with a
 * stored id the backend no longer knows.
 */
import type { Page } from '@playwright/test'
import { FIXTURES } from './environment'
import {
  Workflow,
  expect,
  fetchJob,
  fourStemMode,
  resyncOverRest,
  test,
  type JobRecord,
} from './app'

const fixture = FIXTURES.standard

/**
 * Key the app stores its session snapshot under
 * (`src/state/persistence.ts`). Named here rather than imported because the
 * specs are Node programs that never load application modules; if it ever
 * changes, the "starts cleanly" test below fails loudly rather than
 * silently testing nothing — it asserts the key it planted was consumed.
 */
const SESSION_KEY = 'straticate.session.v2'

/** Read the app's stored session snapshot, or `null` when there is none. */
async function storedSession(
  page: Page,
): Promise<Record<string, unknown> | null> {
  return page.evaluate((key) => {
    const raw = sessionStorage.getItem(key)
    return raw === null
      ? null
      : (JSON.parse(raw) as Record<string, unknown> | null)
  }, SESSION_KEY)
}

test('a stale REST snapshot cannot revert a completed job', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  const mode = await fourStemMode(request)

  await workflow.open()
  await workflow.uploadWithPicker(fixture.path)
  await workflow.choose(mode)
  const job = await workflow.startSeparation()

  await expect(workflow.stage).toHaveText('Completed')
  await expect(workflow.viewResultsButton).toBeVisible()

  // The snapshot the backend would have produced a moment before the job
  // finished: the exact record that stranded the UI.
  const completed = await fetchJob(request, job.id)
  expect(completed.state, 'the job really did complete').toBe('completed')
  const stale: JobRecord = {
    ...completed,
    state: 'separating',
    progress: 0.5,
    finished_at: null,
    result: null,
  }

  let staleAnswers = 0
  await page.route(
    (url) => url.pathname === `/api/v1/jobs/${job.id}`,
    async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback()
        return
      }
      staleAnswers += 1
      await route.fulfill({ json: stale })
    },
  )

  // Twice, because "reached the right state" and "stayed there" are different
  // claims: the second reconnect re-serves the stale record to an app that
  // has already refused it once.
  for (const attempt of [1, 2]) {
    await resyncOverRest(page, job.id)
    expect(
      staleAnswers,
      `the app refetched the job on reconnect ${String(attempt)}`,
    ).toBe(attempt)

    await expect(workflow.stage).toHaveText('Completed')
    await expect(workflow.viewResultsButton).toBeVisible()
    await expect(workflow.cancelButton).toHaveCount(0)
    await expect(
      workflow.progress.getByRole('progressbar', {
        name: 'Separation progress',
      }),
    ).toHaveCount(0)
  }

  // The results are still reachable afterwards — a job stranded in the old
  // defect could not be inspected at all.
  await workflow.viewResultsButton.click()
  await expect(workflow.phase).toHaveText('Inspect')
  await expect(workflow.player.locator('.stem-player-stem-name')).toHaveText([
    ...mode.stems,
  ])
})

test('reloading mid-run returns to the running job, which runs on to completion', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  const mode = await fourStemMode(request)

  // The longer fixture, for the same reason the cancel spec uses it: the
  // reload has to land while the run is still going, and twelve chunks give
  // that margin on any machine.
  const running = FIXTURES.long
  await workflow.open()
  await workflow.uploadWithPicker(running.path)
  await workflow.choose(mode)
  const job = await workflow.startSeparation()
  await expect(workflow.chunks).toHaveText(
    new RegExp(`^\\d+ / ${String(running.chunks)}$`),
  )

  // Only identifiers are kept: no `Job`, no result, no metrics. A cached
  // record would race the event stream on the way back in, which is the
  // failure features 017 and 031 already paid for.
  const stored = await storedSession(page)
  expect(stored, 'the session snapshot survives the run').not.toBeNull()
  expect(Object.keys(stored ?? {}).sort()).toEqual([
    'audioId',
    'jobId',
    'phase',
    'view',
  ])
  expect(stored).toMatchObject({ jobId: job.id, phase: 'separate' })
  // No Inspect view yet — the job is still running, so nothing has been
  // there to record a playhead, a loop or a window (feature 066).
  expect(stored?.view).toBeNull()

  // Every `POST /jobs` from here on would be a *second* separation: resuming
  // means re-reading the one that is already running, never starting another.
  let jobsCreated = 0
  page.on('request', (request_) => {
    if (
      request_.method() === 'POST' &&
      new URL(request_.url()).pathname === '/api/v1/jobs'
    ) {
      jobsCreated += 1
    }
  })

  const restored = page.waitForResponse(
    (response) =>
      response.request().method() === 'GET' &&
      new URL(response.url()).pathname === `/api/v1/jobs/${job.id}`,
  )
  await page.reload()
  await restored

  // Back on the running job, not at file selection: the progress panel is
  // live and Cancel is offered, which is only rendered for a job that has
  // not reached a terminal state.
  await expect(workflow.phase).toHaveText('Separate')
  await expect(workflow.cancelButton).toBeVisible()

  // And the socket carries it the rest of the way *in the UI* — the whole
  // point of the capability. Before 033 this state was reachable only by
  // asking the backend directly.
  await expect(workflow.stage).toHaveText('Completed')
  await expect(workflow.viewResultsButton).toBeVisible()
  expect(jobsCreated, 'the reload resumed a job rather than starting one').toBe(
    0,
  )
  expect((await fetchJob(request, job.id)).state).toBe('completed')
})

test('reloading after completion returns to the results', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  const mode = await fourStemMode(request)

  await workflow.open()
  await workflow.uploadWithPicker(fixture.path)
  await workflow.choose(mode)
  await workflow.startSeparation()
  await expect(workflow.stage).toHaveText('Completed')

  await workflow.viewResultsButton.click()
  await expect(workflow.phase).toHaveText('Inspect')

  await page.reload()

  // The phase is stored because the records cannot imply it: a completed job
  // is equally consistent with the user reading the result summary and with
  // the user listening to the stems.
  await expect(workflow.phase).toHaveText('Inspect')
  await expect(workflow.player.locator('.stem-player-stem-name')).toHaveText([
    ...mode.stems,
  ])
  await expect(workflow.exportPanel).toBeVisible()
})

test('a stored job the backend has never heard of starts cleanly', async ({
  page,
}) => {
  const workflow = new Workflow(page)

  // A well-formed ULID that names nothing — exactly what the store holds
  // after the backend restarts, since jobs and uploads live in memory.
  const unknownJobId = '01HZZZZZZZZZZZZZZZZZZZZZZZ'
  await workflow.open()
  await page.evaluate(
    ([key, jobId]) => {
      sessionStorage.setItem(
        key,
        JSON.stringify({ jobId, audioId: null, phase: 'separate' }),
      )
    },
    [SESSION_KEY, unknownJobId] as const,
  )

  // The app really does go and ask about the id it was given, and really
  // does get told there is no such job. Without this the test would pass
  // just as well against an app that never reads storage at all.
  const refused = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/jobs/${unknownJobId}`,
  )
  await page.reload()
  expect((await refused).status(), 'the backend has no such job').toBe(404)

  // No error: an id the backend has never heard of is not something the user
  // can act on, so the workflow simply starts.
  await expect(workflow.phase).toHaveText('Select')
  await expect(workflow.dropZone).toBeVisible()
  await expect(page.getByRole('alert')).toHaveCount(0)

  // The dead id is not left behind to fail the same way on the next reload —
  // which also proves the app read the key this test planted.
  expect(await storedSession(page)).toBeNull()

  await workflow.uploadWithPicker(fixture.path)
  await expect(workflow.phase).toHaveText('Configure')
  await expect(
    workflow.options.getByRole('button', { name: 'Start separation' }),
  ).toBeEnabled()
})
