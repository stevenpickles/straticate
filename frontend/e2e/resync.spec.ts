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
 */
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

test('reloading mid-run leaves the backend job alone and the app usable', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  const mode = await fourStemMode(request)

  await workflow.open()
  await workflow.uploadWithPicker(fixture.path)
  await workflow.choose(mode)
  const job = await workflow.startSeparation()
  await expect(workflow.chunks).toHaveText(
    new RegExp(`^\\d+ / ${String(fixture.chunks)}$`),
  )

  await page.reload()

  // Straticate keeps its workflow state in memory only — there is no
  // persistence layer, so a reload is a new session and the app comes back at
  // file selection. What matters for correctness is that it comes back
  // *clean* and usable rather than stuck, and that the run it left behind is
  // untouched. (The session does not survive a reload: recorded as a
  // limitation in docs/features/030-playwright-e2e.md, not fixed here.)
  await expect(workflow.phase).toHaveText('Select')
  await expect(workflow.dropZone).toBeVisible()

  await expect
    .poll(async () => (await fetchJob(request, job.id)).state, {
      message: 'the separation the reload abandoned still runs to completion',
    })
    .toBe('completed')

  await workflow.uploadWithPicker(fixture.path)
  await expect(workflow.phase).toHaveText('Configure')
  await expect(
    workflow.options.getByRole('button', { name: 'Start separation' }),
  ).toBeEnabled()
})
