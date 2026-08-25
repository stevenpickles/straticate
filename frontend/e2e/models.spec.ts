/**
 * The model library: the screen features 025, 032 and 035 each deferred to.
 *
 * Three things it has to prove, and one thing it must never do:
 *
 * 1. **Every catalogued model is listed** — state, size, requirements and,
 *    for the first time in this application's history, its licence and the
 *    attribution that licence requires. The first test asserts that against
 *    the **real** `GET /models`, so nothing about the catalog is hardcoded
 *    (AGENTS.md principle 6) and a catalog change cannot make this spec
 *    assert something the app never sees.
 * 2. **The terms are readable before the download**, including the ones that
 *    are more restrictive than the model's code licence or are stated in
 *    words rather than as a named licence. Those records are scripted,
 *    because the catalog on this machine has no such entry — and inventing
 *    one in `models/catalog.json` would be a lie about a real model.
 * 3. **Install, cancel and remove all work from the UI**, with cancel and
 *    remove legibly distinct even though they are the same request.
 *
 * And it **downloads nothing**. Every `/api/v1/models/...` request in the
 * install test is intercepted, so the real install route is never reached;
 * the spec asserts the counts itself. Following 030's discipline, every wait
 * is a condition — the DOM reaching a state the script put it in — and
 * nothing sleeps for a duration.
 */
import type { Locator, Page } from '@playwright/test'
import { FIXTURES } from './environment'
import { Workflow, expect, listModels, modelIdOf, test } from './app'
import type { InstallationRecord, ModelRecord } from './app'

const fixture = FIXTURES.tiny

/** Size of the scripted weights artifact: 870 MiB, which reads `870 MB`. */
const TOTAL_BYTES = 870 * 1024 * 1024
const TOTAL_LABEL = '870 MB'

/** An `installation` block in whichever state a test needs. */
function installation(
  state: InstallationRecord['state'],
  progress: number | null = null,
): InstallationRecord {
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

/**
 * A scripted catalogue entry.
 *
 * `ModelRecord` is the reading shape — the handful of fields the suite asks
 * about. A record the app is going to *render* has to be a whole `Model`, so
 * this adds the manifest fields the library shows: a fragment would leave the
 * catalog serving something the contract says cannot exist.
 */
interface ScriptedModel extends ModelRecord {
  readonly architecture: string
  readonly version: string
  readonly separation_mode: string
  readonly quality_tier: string
  readonly stems: readonly string[]
  readonly sample_rate: number
  readonly requirements: { readonly recommended_vram_mb: number }
  readonly capabilities: Readonly<Record<string, boolean>>
}

/** A scripted catalogue entry, complete enough for the library to render. */
function model(
  id: string,
  displayName: string,
  licensing: ModelRecord['licensing'],
  state: InstallationRecord['state'] = 'available',
): ScriptedModel {
  return {
    id,
    display_name: displayName,
    development_only: false,
    architecture: 'example_architecture',
    version: '1.0',
    separation_mode: 'vocals',
    quality_tier: 'balanced',
    stems: ['vocals', 'instrumental'],
    sample_rate: 44100,
    requirements: { recommended_vram_mb: 4096 },
    capabilities: { cuda: true, cpu: true },
    licensing,
    installation: installation(state, state === 'installed' ? 1 : null),
  }
}

/** The library, as a page object. */
class Library {
  readonly page: Page
  readonly workflow: Workflow

  constructor(page: Page) {
    this.page = page
    this.workflow = new Workflow(page)
  }

  get toggle(): Locator {
    return this.page.getByRole('button', { name: 'Models', exact: true })
  }

  get region(): Locator {
    return this.page.getByRole('region', { name: 'Model library' })
  }

  card(displayName: string): Locator {
    return this.region.getByRole('article', { name: displayName })
  }

  /** Open the app and the library, from the file-selection phase. */
  async open(): Promise<void> {
    await this.workflow.open()
    await this.toggle.click()
    await expect(this.region).toBeVisible()
  }
}

test('the library lists every catalogued model, with its terms', async ({
  page,
  request,
}) => {
  const library = new Library(page)
  // The real catalog — whatever this server was started with.
  const models = await listModels(request)
  expect(
    models.length,
    'the server catalogues at least one model',
  ).toBeGreaterThan(0)

  await library.open()

  // Scoped to the library: the header carries a `role="status"` of its own.
  await expect(library.region.getByRole('status')).toContainText(
    `${String(models.length)} model`,
  )
  await expect(library.region.getByRole('article')).toHaveCount(models.length)

  for (const record of models) {
    const card = library.card(record.display_name)
    await expect(card).toBeVisible()
    // Its identity and its terms, both from the catalog rather than from a
    // literal in this file.
    await expect(card).toContainText(record.id)
    await expect(
      card.getByRole('region', {
        name: `Licensing for ${record.display_name}`,
      }),
    ).toBeVisible()

    const attribution = record.licensing?.attribution
    if (attribution !== undefined && attribution !== null) {
      await expect(
        card,
        'a credit nobody sees is not a credit given',
      ).toContainText(attribution)
    }

    if (record.development_only) {
      await expect(card).toContainText('Development fixture')
    }
  }
})

test('a model’s terms are readable before a byte is downloaded', async ({
  page,
}) => {
  const library = new Library(page)
  const restrictive = model('restricted-001', 'Restrictive Weights', {
    code_license: 'MIT',
    weights_license:
      'Research and personal use only; ask the authors before any commercial use.',
    redistribution_permitted: false,
    commercial_use_permitted: false,
    attribution: 'Weights: Example Separator by the Example Lab.',
  })
  const silent = model('silent-001', 'Silent Weights', {
    code_license: 'MIT',
    weights_license: null,
    redistribution_permitted: null,
    commercial_use_permitted: null,
    attribution: null,
  })
  const catalog = [restrictive, silent]

  await page.route(
    (url) => url.pathname === '/api/v1/models',
    async (route) => {
      await route.fulfill({ json: catalog })
    },
  )
  await page.route(
    (url) => modelIdOf(url) !== null,
    async (route) => {
      const id = modelIdOf(new URL(route.request().url()))
      const record = catalog.find((candidate) => candidate.id === id)
      if (record === undefined) {
        await route.fallback()
        return
      }
      await route.fulfill({ json: record })
    },
  )

  await library.open()

  // Weights more restrictive than the code, stated in words rather than as a
  // named licence: rendered in full, badged, and refusals stated as refusals.
  const strict = library.card('Restrictive Weights')
  await expect(strict).toContainText('Restricted use')
  await expect(strict).toContainText('Research and personal use only')
  await expect(strict).toContainText('stated in words, not as a named licence')
  await expect(strict).toContainText('Not permitted')
  await expect(strict).toContainText('Example Separator by the Example Lab')
  await expect(
    strict.getByRole('button', { name: 'Install' }),
    'and all of it is on screen while the weights are still a download',
  ).toBeEnabled()

  // MIT code, nothing said about the weights: the code licence must not stand
  // in for the download's terms. (Feature 027's blocker, exactly.)
  const unknown = library.card('Silent Weights')
  await expect(unknown).toContainText('Terms not stated')
  await expect(unknown).toContainText('does not cover the weights')
  await expect(unknown).toContainText('Not stated')
})

/**
 * Script `GET /system/storage` (feature 040).
 *
 * The real backend would answer with this machine's actual free space, which
 * is a different number on every CI runner and on every developer's laptop —
 * so a spec that asserts what the card *says* about it has to put the figure
 * there itself. `null` is the backend's documented "this host cannot tell
 * you", and reaches the UI as feature 037's honest sentence.
 */
async function scriptStorage(
  page: Page,
  free: number | null,
  total: number | null = 512 * 1024 ** 3,
): Promise<{ set: (free: number | null, total?: number | null) => void }> {
  const held: { free: number | null; total: number | null } = { free, total }
  await page.route(
    (url) => url.pathname === '/api/v1/system/storage',
    async (route) => {
      await route.fulfill({
        json: { free_bytes: held.free, total_bytes: held.total },
      })
    },
  )
  return {
    set: (next: number | null, nextTotal: number | null = total) => {
      held.free = next
      held.total = nextTotal
    },
  }
}

test('the disk cost is a comparison, and an unknown one is still honest', async ({
  page,
}) => {
  const library = new Library(page)
  const record = model('managed-001', 'Managed Model', {
    code_license: 'MIT',
    weights_license: 'MIT',
    redistribution_permitted: true,
    commercial_use_permitted: true,
    attribution: 'Weights: Example Model.',
  })
  const scripted: ScriptedModel = {
    ...record,
    installation: installation('available'),
  }

  // Nothing is downloaded and nothing is installed: the model is scripted in
  // the one state where an install is offered.
  await page.route(
    (url) => url.pathname === '/api/v1/models',
    async (route) => {
      await route.fulfill({ json: [scripted] })
    },
  )
  await page.route(
    (url) => modelIdOf(url) === record.id,
    async (route) => {
      await route.fulfill({ json: scripted })
    },
  )

  // A disk with room: the notice states the comparison rather than a
  // limitation, and the figure comes from the backend, not the browser.
  const disk = await scriptStorage(page, 4 * 1024 ** 3)
  await library.open()
  const card = library.card('Managed Model')
  await expect(card).toContainText(`${TOTAL_LABEL} will be written`)
  await expect(card).toContainText('4 GB is free there')
  await expect(card).not.toContainText('cannot check')

  // A host that cannot answer: 037's wording is back, the download is still
  // priced, and the button still works — unknown is cautious, never a block.
  disk.set(null, null)
  await page.reload()
  await library.open()
  const again = library.card('Managed Model')
  await expect(again).toContainText('cannot check')
  await expect(again.getByRole('button', { name: 'Install' })).toBeEnabled()
})

test('a download that cannot fit is warned about, not refused', async ({
  page,
}) => {
  const library = new Library(page)
  const record = model('managed-001', 'Managed Model', {
    code_license: 'MIT',
    weights_license: 'MIT',
    redistribution_permitted: true,
    commercial_use_permitted: true,
    attribution: 'Weights: Example Model.',
  })
  const scripted: ScriptedModel = {
    ...record,
    installation: installation('available'),
  }
  let installs = 0

  await page.route(
    (url) => url.pathname === '/api/v1/models',
    async (route) => {
      await route.fulfill({ json: [scripted] })
    },
  )
  await page.route(
    (url) => modelIdOf(url) === record.id,
    async (route) => {
      if (new URL(route.request().url()).pathname.endsWith('/install')) {
        installs += 1
        await route.fulfill({
          status: 202,
          json: { ...record, installation: installation('downloading', 0) },
        })
        return
      }
      await route.fulfill({ json: scripted })
    },
  )
  await scriptStorage(page, 128 * 1024 * 1024)

  await library.open()
  const card = library.card('Managed Model')
  await expect(card).toContainText('will not fit')

  // The decision stays the user's: a reading is one moment old, free space
  // moves, and a false refusal here is the worse failure. So the button works.
  await card.getByRole('button', { name: 'Install' }).click()
  await expect(card).toContainText('Downloading')
  expect(installs, 'the install was never refused by the UI').toBe(1)
})

test('install, cancel, install again and remove — downloading nothing', async ({
  page,
}) => {
  const library = new Library(page)
  const record = model('managed-001', 'Managed Model', {
    code_license: 'MIT',
    weights_license: 'MIT',
    redistribution_permitted: true,
    commercial_use_permitted: true,
    attribution: 'Weights: Example Model.',
  })

  let state: InstallationRecord['state'] = 'available'
  let installs = 0
  let deletes = 0
  // Flipped by the test between the two installs, so the first one can be
  // caught mid-download and the second one allowed to finish.
  let settle = false

  const current = (): ScriptedModel => {
    if (state === 'downloading' && settle) {
      state = 'installed'
    }
    return {
      ...record,
      installation: installation(
        state,
        state === 'downloading' ? 0.25 : state === 'installed' ? 1 : null,
      ),
    }
  }

  await page.route(
    (url) => url.pathname === '/api/v1/models',
    async (route) => {
      await route.fulfill({ json: [current()] })
    },
  )
  await page.route(
    (url) => modelIdOf(url) === record.id,
    async (route) => {
      const url = new URL(route.request().url())
      if (url.pathname.endsWith('/install')) {
        installs += 1
        state = 'downloading'
        await route.fulfill({
          status: 202,
          json: { ...record, installation: installation('downloading', 0) },
        })
        return
      }
      if (route.request().method() === 'DELETE') {
        deletes += 1
        state = 'available'
        settle = false
        await route.fulfill({
          json: { ...record, installation: installation('available') },
        })
        return
      }
      await route.fulfill({ json: current() })
    },
  )

  await scriptStorage(page, 4 * 1024 ** 3)
  await library.open()
  const card = library.card('Managed Model')

  // 1. Not installed: priced against the room the backend reports.
  await expect(card).toContainText('Not installed')
  await expect(card).toContainText(`${TOTAL_LABEL} will be written`)
  await expect(card).toContainText('4 GB is free there')

  // 2. Install: real progress, from the state the script put the model in.
  await card.getByRole('button', { name: 'Install' }).click()
  await expect(
    card.getByRole('progressbar', { name: 'Model download progress' }),
  ).toHaveAttribute('aria-valuenow', '25')

  // 3. Cancel is *not* "remove", and says what it throws away.
  await expect(card).toContainText('deletes the partly downloaded file')
  await expect(
    card.getByRole('button', { name: 'Remove weights' }),
  ).toHaveCount(0)
  await card.getByRole('button', { name: 'Cancel download' }).click()
  await expect(card).toContainText('Not installed')
  expect(deletes, 'cancelling took exactly one request').toBe(1)

  // 4. Install again, and let it finish this time.
  settle = true
  await card.getByRole('button', { name: 'Install' }).click()
  await expect(card).toContainText(`Installed — ${TOTAL_LABEL} on disk`)
  await expect(
    card.getByRole('button', { name: 'Cancel download' }),
  ).toHaveCount(0)

  // 5. Removing an installed model asks first — it throws away a download the
  //    user waited for, which cancelling never does.
  await card.getByRole('button', { name: 'Remove weights' }).click()
  const confirm = card.getByRole('group', { name: 'Confirm removal' })
  await expect(confirm).toContainText(TOTAL_LABEL)
  expect(deletes, 'and asking is not doing').toBe(1)

  await confirm.getByRole('button', { name: 'Keep them' }).click()
  await expect(confirm).toHaveCount(0)
  expect(deletes).toBe(1)

  await card.getByRole('button', { name: 'Remove weights' }).click()
  await card.getByRole('button', { name: 'Delete the weights' }).click()
  await expect(card).toContainText('Not installed')

  expect(deletes, 'one cancel and one removal, over the same route').toBe(2)
  expect(installs, 'exactly two installs were requested').toBe(2)
})

test('the library never disturbs the workflow it sits beside', async ({
  page,
}) => {
  const library = new Library(page)

  await library.workflow.open()
  await library.workflow.uploadWithPicker(fixture.path)
  await expect(library.workflow.phase).toHaveText('Configure')
  await expect(library.workflow.summary).toBeVisible()

  await library.toggle.click()
  await expect(library.region).toBeVisible()
  await expect(
    library.workflow.summary,
    'the workflow is hidden while the library is up',
  ).toBeHidden()

  await page.getByRole('button', { name: 'Close models' }).click()
  await expect(library.region).toHaveCount(0)
  await expect(
    library.workflow.phase,
    'and the user comes back to exactly where they were',
  ).toHaveText('Configure')
  await expect(library.workflow.summary).toBeVisible()
})
