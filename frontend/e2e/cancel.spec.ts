/**
 * Cancellation, and staying cancelled.
 *
 * Cancelling is a *request*, not a stop: `POST /jobs/{id}/cancel` answers with
 * the job as it was when the handler ran — possibly still separating — and the
 * authoritative transition arrives later as a `job_cancelled` event. So the
 * test asserts the **terminal** state the app settles in, not the click, and
 * then proves the state survives a reconnect's REST resync.
 */
import { FIXTURES } from './environment'
import {
  Workflow,
  expect,
  fetchJob,
  fourStemMode,
  resyncOverRest,
  test,
} from './app'

const fixture = FIXTURES.standard

test('a job cancelled mid-run reaches cancelled and stays there', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  const mode = await fourStemMode(request)

  await workflow.open()
  await workflow.uploadWithPicker(fixture.path)
  await workflow.choose(mode)
  const job = await workflow.startSeparation()

  // Cancel once the run is demonstrably under way — waiting for a real chunk
  // count rather than for a moment on the clock.
  await expect(workflow.chunks).toHaveText(
    new RegExp(`^\\d+ / ${String(fixture.chunks)}$`),
  )
  await workflow.cancelButton.click()

  await expect(workflow.stage).toHaveText('Cancelled')
  await expect(workflow.progress).toContainText('Separation cancelled')
  // A terminal job offers no cancel and no progress bar, and does offer a way
  // onward — the same "no dead ends" rule the inspect phase is held to.
  await expect(workflow.cancelButton).toHaveCount(0)
  await expect(
    workflow.progress.getByRole('progressbar', { name: 'Separation progress' }),
  ).toHaveCount(0)
  await expect(
    workflow.progress.getByRole('button', { name: 'Start another separation' }),
  ).toBeVisible()

  // The backend agrees, which is what makes the UI's word for it true.
  const record = await fetchJob(request, job.id)
  expect(record.state).toBe('cancelled')

  // And it stays cancelled across a dropped connection: the reconnect
  // refetches the job over REST and applies the answer on top of the state
  // the events already produced.
  await resyncOverRest(page, job.id)
  await expect(workflow.stage).toHaveText('Cancelled')
  await expect(workflow.cancelButton).toHaveCount(0)
})
