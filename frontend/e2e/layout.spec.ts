/**
 * Feature 067: measurement, not inference.
 *
 * 054 measured the timeline's lane header at a real Chromium's default
 * (16 px) root font size and found ~0.9 px of slack — and separately measured
 * that at 17/18/20 px browser-level font settings (a real accessibility
 * setting, not a hypothetical one) the same rem-sized rows outgrew the fixed
 * `LANE_HEIGHT_PX` box by 1/3/7 px, clipped by `overflow: hidden`. The
 * fader's pointer target measured ~11 px, under WCAG 2.2 SC 2.5.8's 24 px
 * minimum at every root size.
 *
 * This spec pins both as regressions across the sizes 054 measured them at,
 * on the Inspect screen of a completed job — the same screen, the same
 * markup, driven the same way a browser's own font-size setting would drive
 * it: `document.documentElement.style.fontSize`, which every `rem` length on
 * the page resolves against without any application code needing to know it
 * changed. Most of what follows is about the layout box a `rem` height
 * produces, which CSS updates on its own — the one exception is the canvas
 * backing store, which does need to know, and which the last stage in this
 * file checks directly rather than assuming CSS carries it along too: see
 * that stage's own comment for the review finding (`resize` does not fire
 * for a font-size-only change) that makes it worth checking.
 */
import type { Locator, Page } from '@playwright/test'
import { FIXTURES } from './environment'
import {
  Workflow,
  expect,
  fourStemMode,
  test,
  type SeparationMode,
} from './app'

/**
 * The root font sizes 054 measured clipping at, plus the 16 px default it
 * measured the ~0.9 px of slack at. `16` is the size every other spec in
 * this tier implicitly runs at (nothing else here changes it), so including
 * it is what makes this spec a full regression pin rather than one that only
 * covers the sizes that were already known to fail.
 */
const ROOT_FONT_SIZES_PX = [16, 17, 18, 20]

/** Set the root font size the way a browser's own setting would. */
async function setRootFontPx(page: Page, px: number): Promise<void> {
  await page.evaluate((value) => {
    document.documentElement.style.fontSize = `${String(value)}px`
  }, px)
  // A real condition, not a duration: `rem` layout is recomputed as part of
  // the style mutation above, but this still gives one full render/paint
  // cycle to settle before anything below measures it — the same idiom
  // `renderedFrames` uses elsewhere in this tier.
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

/** One lane header's overflow state, read straight from the DOM. */
interface HeaderOverflow {
  readonly clientHeight: number
  readonly scrollHeight: number
}

/** Every `.stem-timeline-lane-header`, in document order. */
async function laneHeaderOverflow(page: Page): Promise<HeaderOverflow[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('.stem-timeline-lane-header')].map(
      (element) => ({
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
      }),
    ),
  )
}

/** Every `.stem-timeline-lane` row's rendered height, in document order. */
async function laneRowHeights(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('.stem-timeline-lane')].map(
      (element) => element.getBoundingClientRect().height,
    ),
  )
}

/** Every `.stem-timeline-lane-header`'s rendered height, in document order. */
async function laneHeaderHeights(page: Page): Promise<number[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('.stem-timeline-lane-header')].map(
      (element) => element.getBoundingClientRect().height,
    ),
  )
}

/**
 * A `<canvas>`'s actual backing-store height — `canvas.height`, the plain
 * integer the 2D context paints into, not the CSS box `getBoundingClientRect`
 * reports. This is the number 067's Fix 1 exists to keep in step with the
 * lane's rendered box on a mid-session root-font change.
 */
async function canvasBackingHeight(canvas: Locator): Promise<number> {
  return canvas.evaluate((element: HTMLCanvasElement) => element.height)
}

/**
 * Whichever element the browser would actually deliver a click to at
 * `(x, y)`, as its accessible name — the 050-era guard against an
 * overflowing sibling swallowing a click meant for the control underneath
 * it. `null` covers nothing being there at all.
 */
async function accessibleNameAtPoint(
  page: Page,
  x: number,
  y: number,
): Promise<string | null> {
  return page.evaluate(
    ([px, py]: readonly [number, number]) =>
      document.elementFromPoint(px, py)?.getAttribute('aria-label') ?? null,
    [x, y] as const,
  )
}

test.describe('lane layout across browser root font sizes (feature 067)', () => {
  let page: Page
  let workflow: Workflow
  let mode: SeparationMode

  test.beforeAll(async ({ browser, request }) => {
    page = await browser.newPage()
    workflow = new Workflow(page)
    mode = await fourStemMode(request)

    // The tiny fixture: layout does not depend on how much audio there is,
    // only on a completed job with every stem's lane and header rendered.
    await workflow.open()
    await workflow.uploadWithPicker(FIXTURES.tiny.path)
    await workflow.choose(mode)
    await workflow.startSeparation()
    await expect(workflow.stage).toHaveText('Completed')
    await workflow.viewResultsButton.click()
    await expect(workflow.phase).toHaveText('Inspect')

    // Wait for the real condition the rest of this spec depends on: every
    // stem's lane header and canvas are in the DOM, not still loading.
    await expect(workflow.player.locator('.stem-player-stem-name')).toHaveText([
      ...mode.stems,
    ])
    await expect(workflow.timeline.locator('canvas')).toHaveCount(
      mode.stems.length,
    )
  })

  test.afterAll(async () => {
    await page.close()
  })

  for (const rootFontPx of ROOT_FONT_SIZES_PX) {
    test(`at a ${String(rootFontPx)}px root: headers don't clip, faders reach 24px, columns stay aligned, and faders stay clickable`, async () => {
      await setRootFontPx(page, rootFontPx)

      // 1. No lane header clips its own content — the 17/18/20 px v0.2.0
      // regression this spec exists to pin, plus the 16 px default 054 found
      // only ~0.9 px of slack at.
      const headers = await laneHeaderOverflow(page)
      expect(headers, 'one header per stem').toHaveLength(mode.stems.length)
      for (const [index, header] of headers.entries()) {
        expect(
          header.scrollHeight,
          `${mode.stems[index] ?? '?'}'s header does not clip at ${String(rootFontPx)}px`,
        ).toBeLessThanOrEqual(header.clientHeight)
      }

      // 2. Every fader's pointer target reaches WCAG 2.2 SC 2.5.8's 24 px
      // minimum, and 3. lands a click on itself rather than an overflowing
      // neighbour swallowing it (the 050-era guard, generalised to a real
      // pointer-target size rather than the ~11 px 054 shipped with).
      for (const name of mode.stems) {
        const fader = workflow.player.getByRole('slider', {
          name: `${name} level`,
        })
        const box = await fader.boundingBox()
        expect(box, `${name}'s fader is laid out`).not.toBeNull()
        if (box === null) {
          continue
        }
        expect(
          box.height,
          `${name}'s fader reaches the 24px pointer-target minimum at ${String(rootFontPx)}px`,
        ).toBeGreaterThanOrEqual(24)

        // The centre alone does not distinguish this box from the pre-067
        // ~11 px one — both have a centre, and both land a click there. The
        // point of a 24 px *hit box* is the edges: a real pointer lands
        // anywhere inside it, not just in the middle. `y + 1` and
        // `y + height - 1` sample one pixel inside the top and bottom edges
        // of the grown box (review-probed: both land on the fader), which
        // the old ~11 px box could not have passed either — its own edges
        // were the ~11 px box's edges, not this one's.
        const centerX = box.x + box.width / 2
        const topY = box.y + 1
        const centerY = box.y + box.height / 2
        const bottomY = box.y + box.height - 1
        for (const [label, y] of [
          ['top edge', topY],
          ['centre', centerY],
          ['bottom edge', bottomY],
        ] as const) {
          expect(
            await accessibleNameAtPoint(page, centerX, y),
            `a click at ${name}'s fader's ${label} lands on the fader itself at ${String(rootFontPx)}px`,
          ).toBe(`${name} level`)
        }
      }

      // 4. The header column and the lane column stay aligned to the pixel:
      // every stem's header row is exactly as tall as its lane row. Counts
      // come from the mode's own stems, never a literal, so a mode with a
      // different stem count is covered the same way.
      const headerHeights = await laneHeaderHeights(page)
      const rowHeights = await laneRowHeights(page)
      expect(headerHeights, 'one header row per stem').toHaveLength(
        mode.stems.length,
      )
      expect(rowHeights, 'one lane row per stem').toHaveLength(
        mode.stems.length,
      )
      for (const [index, name] of mode.stems.entries()) {
        expect(
          headerHeights[index],
          `${name}'s header row is as tall as its lane row at ${String(rootFontPx)}px`,
        ).toBeCloseTo(rowHeights[index] ?? -1, 1)
      }
    })
  }

  test('a mid-session root font-size change keeps the canvas backing store in step (review Fix 1)', async () => {
    // Review finding: `document.documentElement.style.fontSize` — the same
    // mechanism a browser's own font-size setting drives, and what
    // `setRootFontPx` above uses — does NOT fire `resize` on `window` (a
    // controlled Chromium probe found `innerWidth`/`innerHeight` unchanged
    // by a font-size-only reflow). 067's first mechanism
    // (`useRootFontSize`) refreshed the canvas backing store only on
    // `resize`, so a mid-session change like this one left it stale: the
    // lane's `rem`-sized CSS box grew for free while the canvas kept
    // painting at the old pixel height, stretching the waveform. This stage
    // pins the fix directly — the canvas backing store must follow the
    // lane's *actual* rendered box, not a proxy signal — with no manual
    // `resize` dispatch anywhere in it (dispatching one by hand would mask
    // exactly the bug this exists to catch).
    await setRootFontPx(page, 16)

    const canvas = workflow.timeline.locator('canvas').first()
    const dpr = await page.evaluate(() => window.devicePixelRatio)

    const laneBefore = (await laneRowHeights(page))[0] ?? -1
    const backingBefore = await canvasBackingHeight(canvas)
    expect(
      backingBefore,
      'canvas backing height matches the lane box at the baseline root font',
    ).toBeCloseTo(Math.round(laneBefore * dpr), 0)

    await setRootFontPx(page, 20)

    const laneAfter = (await laneRowHeights(page))[0] ?? -1
    const backingAfter = await canvasBackingHeight(canvas)
    expect(
      laneAfter,
      'the lane box itself grew with the root font — plain CSS, no JS involved',
    ).toBeGreaterThan(laneBefore)
    expect(
      backingAfter,
      'the canvas backing store actually changed, rather than staying at the stale baseline',
    ).toBeGreaterThan(backingBefore)
    expect(
      backingAfter,
      'the canvas backing store followed the lane box to its new height',
    ).toBeCloseTo(Math.round(laneAfter * dpr), 0)
  })
})
