/**
 * Upload: both routes into the workflow, and the metadata the backend probes
 * on the way in.
 *
 * ROADMAP M1 opens with "drag/drop audio → upload succeeds → metadata
 * appears". Component tests cover the drop zone against a mocked upload; this
 * covers a real file crossing a real HTTP boundary into a real ffprobe.
 */
import { FIXTURES } from './environment'
import { Workflow, expect, separationModes, test } from './app'

const fixture = FIXTURES.tiny

/** The metadata rows ffprobe should produce for the generated fixture. */
const EXPECTED_METADATA: readonly (readonly [string, string])[] = [
  ['Duration', '0:02'],
  ['Format', 'WAV'],
  ['Channels', 'Stereo'],
  ['Sample Rate', '44.1 kHz'],
  ['Bit Depth', '16 bit'],
]

for (const route of ['file picker', 'drag and drop'] as const) {
  test(`uploading with the ${route} shows the probed metadata`, async ({
    page,
  }) => {
    const workflow = new Workflow(page)
    await workflow.open()

    if (route === 'file picker') {
      await workflow.uploadWithPicker(fixture.path)
    } else {
      await workflow.uploadWithDrop(fixture.path)
    }

    await expect(workflow.phase).toHaveText('Configure')
    await expect(workflow.summary.getByRole('heading')).toHaveText(
      'mixture-2s.wav',
    )
    for (const [label, value] of EXPECTED_METADATA) {
      const row = workflow.summary
        .locator('.audio-summary-field')
        .filter({ hasText: label })
      await expect(row.locator('.audio-summary-value')).toHaveText(value)
    }
  })
}

test('the configure phase renders the catalog the backend serves', async ({
  page,
  request,
}) => {
  const workflow = new Workflow(page)
  await workflow.open()
  await workflow.uploadWithPicker(fixture.path)

  // Assert the UI against the catalog rather than against literals: a mode,
  // a tier or a stem added to models/catalog.json must reach the picker
  // without a change here (AGENTS.md principle 6).
  const modes = await separationModes(request)
  expect(modes.length, 'the catalog serves at least one mode').toBeGreaterThan(
    0,
  )

  for (const mode of modes) {
    const radio = workflow.options.getByLabel(mode.display_name, {
      exact: true,
    })
    await expect(radio).toBeVisible()
    const stems = workflow.options.locator(
      `#separation-mode-${mode.id}-stems .separation-option-stem`,
    )
    await expect(stems).toHaveText([...mode.stems])
  }

  // Quality tiers belong to the selected mode, so they are asserted for the
  // one the app preselects: the catalog's first.
  const first = modes[0]
  expect(first).toBeDefined()
  if (first !== undefined) {
    for (const option of first.quality_options) {
      await expect(
        workflow.options.getByLabel(option.display_name, { exact: true }),
      ).toBeVisible()
    }
  }

  await expect(
    workflow.options.getByRole('button', { name: 'Start separation' }),
  ).toBeEnabled()
})
