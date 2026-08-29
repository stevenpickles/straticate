/**
 * `StemTimeline` on its own, with the props the player would pass it.
 *
 * The integration — gestures reaching the engine, the readout, the playhead
 * following the audio clock — is exercised through the whole player in
 * `StemPlayer.test.tsx`. What is easier to pin down here is what each lane
 * *paints*: `installFakeCanvas` gives every canvas in the tree the same
 * recording context, so a suite that wants to attribute rectangles to a lane
 * renders one lane at a time.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  StemTimeline,
  type StemTimelineProps,
  type StemTimelineStem,
} from './StemTimeline'
import type {
  AudioEngineBuffer,
  LoopRegion,
  StemEngineSnapshot,
  StemPlayerEngine,
} from '../audio/engine'
import { FakeAudioBuffer, stemBytesWithSamples } from '../test/fakeAudioContext'
import {
  installFakeCanvas,
  type FakeCanvasContext2D,
} from '../test/fakeCanvasContext'

/** Width of the strip in every test here. */
const WIDTH = 400

/** The shared time axis: a minute, so the tick ladder lands on ten seconds. */
const AXIS_SECONDS = 60

/** A stem that is loud everywhere, so every column has something to draw. */
const LOUD = Array.from({ length: 256 }, (_, index) =>
  index % 2 === 0 ? 0.75 : -0.75,
)

const EMPTY_SNAPSHOT: StemEngineSnapshot = {
  status: 'ready',
  stems: [],
  playing: false,
  durationSeconds: AXIS_SECONDS,
  loopRegion: null,
  scrubbing: false,
  error: null,
}

/**
 * An engine that does nothing but hand out decoded buffers, which is the only
 * thing the timeline asks one for. Every other member is present because
 * `StemPlayerEngine` declares it, and inert because nothing here calls it.
 */
function bufferEngine(
  names: readonly string[],
  samples: readonly number[] = LOUD,
): StemPlayerEngine {
  const buffers = new Map<string, AudioEngineBuffer>(
    names.map((name) => [
      name,
      new FakeAudioBuffer(stemBytesWithSamples(samples)),
    ]),
  )
  return {
    load: () => Promise.resolve(),
    play: () => Promise.resolve(),
    pause: () => undefined,
    seek: () => undefined,
    setMuted: () => undefined,
    toggleMute: () => undefined,
    setSoloed: () => undefined,
    toggleSolo: () => undefined,
    setLevel: () => undefined,
    setLoopRegion: () => undefined,
    clearLoopRegion: () => undefined,
    beginScrubPreview: () => undefined,
    scrubPreview: () => undefined,
    endScrubPreview: () => undefined,
    currentTime: () => 0,
    getStemBuffer: (name: string) => buffers.get(name) ?? null,
    getSnapshot: () => EMPTY_SNAPSHOT,
    subscribe: () => () => undefined,
    dispose: () => undefined,
  }
}

/** A loaded, audible stem of `durationSeconds`, at full level. */
function loaded(name: string, durationSeconds: number): StemTimelineStem {
  return {
    name,
    status: 'loaded',
    muted: false,
    soloed: false,
    audible: true,
    level: 1,
    durationSeconds,
  }
}

interface RenderOptions {
  readonly stems: readonly StemTimelineStem[]
  readonly durationSeconds?: number
  readonly positionSeconds?: number
  readonly ready?: boolean
  readonly onSetLevel?: (name: string, value: number) => void
}

function renderTimeline(options: RenderOptions) {
  const engine = bufferEngine(options.stems.map((stem) => stem.name))
  const view = render(
    <StemTimeline
      stems={options.stems}
      durationSeconds={options.durationSeconds ?? AXIS_SECONDS}
      positionSeconds={options.positionSeconds ?? 0}
      ready={options.ready ?? true}
      engine={engine}
      onScrubStart={() => undefined}
      onScrub={() => undefined}
      onScrubCancel={() => undefined}
      onSeek={() => undefined}
      loopRegion={null}
      onSetLoopRegion={() => undefined}
      onClearLoopRegion={() => undefined}
      onTogglePlayback={() => undefined}
      onToggleMute={() => undefined}
      onToggleSolo={() => undefined}
      onSetLevel={options.onSetLevel ?? (() => undefined)}
    />,
  )
  return { ...view, engine }
}

/** Render and let the (chunked, therefore async) peak computation settle. */
async function renderPainted(options: RenderOptions) {
  const view = renderTimeline(options)
  await act(async () => {})
  return view
}

let canvas: FakeCanvasContext2D

// ---------------------------------------------------------------------------
// Animation frames
//
// Feature 051's high-resolution tiles are computed on a frame, so that a burst
// of wheel events costs one computation rather than one each. Tests deliver
// those frames by hand — nothing here waits on wall-clock time — exactly as
// `StemPlayer.test.tsx` does for the playhead's.
// ---------------------------------------------------------------------------

let frames: Map<number, FrameRequestCallback>
let nextFrameId = 0

function stubAnimationFrames(): void {
  frames = new Map()
  nextFrameId = 0
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    nextFrameId += 1
    frames.set(nextFrameId, callback)
    return nextFrameId
  })
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    frames.delete(handle)
  })
}

/** Run every pending animation frame callback once. */
function flushFrame(): void {
  const pending = [...frames.values()]
  frames.clear()
  act(() => {
    for (const callback of pending) {
      callback(0)
    }
  })
}

beforeEach(() => {
  stubAnimationFrames()
  canvas = installFakeCanvas()
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    width: WIDTH,
    height: 200,
    top: 0,
    left: 0,
    right: WIDTH,
    bottom: 200,
    toJSON: () => ({}),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  // `installFakeCanvas` and the layout stub are `vi.spyOn`, which
  // `unstubAllGlobals` does not touch.
  vi.restoreAllMocks()
})

describe('StemTimeline lanes', () => {
  it('paints nothing before a stem has peaks', () => {
    renderTimeline({ stems: [loaded('vocals', AXIS_SECONDS)] })

    expect(canvas.fillRects).toHaveLength(0)
    // The lane exists all the same; only its content is pending.
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
  })

  it('draws one column per pixel of the strip for a full-length stem', async () => {
    await renderPainted({ stems: [loaded('vocals', AXIS_SECONDS)] })

    expect(canvas.fillRects).toHaveLength(WIDTH)
    expect(canvas.fillRects.at(-1)?.x).toBe(WIDTH - 1)
  })

  it('draws a short stem across only its own share of the shared axis', async () => {
    // Half as long as the axis, so it must stop at half the strip and leave
    // the rest of its lane empty — "no audio here", not "quiet here".
    await renderPainted({ stems: [loaded('drums', AXIS_SECONDS / 2)] })

    expect(canvas.fillRects).toHaveLength(WIDTH / 2)
    expect(canvas.fillRects.at(-1)?.x).toBe(WIDTH / 2 - 1)
  })

  it('gives a failed stem a placeholder rather than a canvas', async () => {
    await renderPainted({
      stems: [
        loaded('vocals', AXIS_SECONDS),
        { ...loaded('drums', AXIS_SECONDS), status: 'error' },
      ],
    })

    expect(screen.getByText('Unavailable')).toBeInTheDocument()
    expect(document.querySelectorAll('canvas')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Mute drums' })).toBeDisabled()
  })

  it('repaints only the lane whose audibility changed', async () => {
    const vocals = loaded('vocals', AXIS_SECONDS)
    const drums = loaded('drums', AXIS_SECONDS)
    const view = await renderPainted({ stems: [vocals, drums] })
    expect(canvas.fillRects).toHaveLength(2 * WIDTH)
    canvas.reset()

    view.rerender(
      <StemTimeline
        stems={[vocals, { ...drums, audible: false }]}
        durationSeconds={AXIS_SECONDS}
        positionSeconds={0}
        ready
        engine={view.engine}
        onScrubStart={() => undefined}
        onScrub={() => undefined}
        onScrubCancel={() => undefined}
        onSeek={() => undefined}
        loopRegion={null}
        onSetLoopRegion={() => undefined}
        onClearLoopRegion={() => undefined}
        onTogglePlayback={() => undefined}
        onToggleMute={() => undefined}
        onToggleSolo={() => undefined}
        onSetLevel={() => undefined}
      />,
    )

    // One lane's worth, in the muted token: the memoised sibling did not
    // repaint at all.
    expect(canvas.fillRects).toHaveLength(WIDTH)
    expect(canvas.fillRects.every((rect) => rect.fillStyle === '#9a9aa5')).toBe(
      true,
    )
  })
})

describe('StemTimeline ruler', () => {
  it('labels the axis with the coarsest step that still fits', async () => {
    await renderPainted({ stems: [loaded('vocals', AXIS_SECONDS)] })

    expect(
      [...document.querySelectorAll('.stem-timeline-tick')].map(
        (tick) => tick.textContent,
      ),
    ).toEqual(['0:00', '0:10', '0:20', '0:30', '0:40', '0:50', '1:00'])
  })

  it('is hidden from assistive technology, which reads the slider instead', async () => {
    await renderPainted({ stems: [loaded('vocals', AXIS_SECONDS)] })

    expect(document.querySelector('.stem-timeline-ruler')).toHaveAttribute(
      'aria-hidden',
      'true',
    )
    expect(document.querySelector('canvas')).toHaveAttribute(
      'aria-hidden',
      'true',
    )
  })
})

describe('StemTimeline lane headers', () => {
  it('forwards mute and solo by stem name', async () => {
    const muted: string[] = []
    const soloed: string[] = []
    const stems = [loaded('vocals', AXIS_SECONDS)]
    render(
      <StemTimeline
        stems={stems}
        durationSeconds={AXIS_SECONDS}
        positionSeconds={0}
        ready
        engine={bufferEngine(['vocals'])}
        onScrubStart={() => undefined}
        onScrub={() => undefined}
        onScrubCancel={() => undefined}
        onSeek={() => undefined}
        loopRegion={null}
        onSetLoopRegion={() => undefined}
        onClearLoopRegion={() => undefined}
        onTogglePlayback={() => undefined}
        onToggleMute={(name) => muted.push(name)}
        onToggleSolo={(name) => soloed.push(name)}
        onSetLevel={() => undefined}
      />,
    )
    await act(async () => {})

    await userEvent.click(screen.getByRole('button', { name: 'Mute vocals' }))
    await userEvent.click(screen.getByRole('button', { name: 'Solo vocals' }))

    expect(muted).toEqual(['vocals'])
    expect(soloed).toEqual(['vocals'])
  })

  it('says how far off a stem still is while it decodes', async () => {
    await renderPainted({
      stems: [{ ...loaded('vocals', AXIS_SECONDS), status: 'loading' }],
    })

    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Solo vocals' })).toBeDisabled()
  })
})

describe('StemTimeline level faders (feature 054)', () => {
  it('gives every stem its own fader, for two- and four-stem results', async () => {
    await renderPainted({
      stems: ['vocals', 'drums', 'bass', 'other'].map((name) =>
        loaded(name, AXIS_SECONDS),
      ),
    })

    for (const name of ['vocals', 'drums', 'bass', 'other']) {
      expect(
        screen.getByRole('slider', { name: `${name} level` }),
      ).toBeInTheDocument()
    }
  })

  it('forwards every change event by stem name and value — continuously, not once per gesture', async () => {
    const levels: { name: string; value: number }[] = []
    await renderPainted({
      stems: [loaded('vocals', AXIS_SECONDS)],
      onSetLevel: (name, value) => {
        levels.push({ name, value })
      },
    })
    const fader = screen.getByRole('slider', { name: 'vocals level' })

    // Two events from one drag must reach the engine as two calls: unlike a
    // seek, a level write has nothing to batch against.
    fireEvent.change(fader, { target: { value: '0.4' } })
    fireEvent.change(fader, { target: { value: '0.75' } })

    expect(levels.length).toBeGreaterThanOrEqual(2)
    expect(levels).toEqual([
      { name: 'vocals', value: 0.4 },
      { name: 'vocals', value: 0.75 },
    ])
  })

  it("reflects the stem's level from the snapshot", async () => {
    await renderPainted({
      stems: [{ ...loaded('vocals', AXIS_SECONDS), level: 0.65 }],
    })

    expect(screen.getByRole('slider', { name: 'vocals level' })).toHaveValue(
      '0.65',
    )
  })

  it('is disabled until the stem has loaded', async () => {
    await renderPainted({
      stems: [{ ...loaded('vocals', AXIS_SECONDS), status: 'loading' }],
    })

    expect(screen.getByRole('slider', { name: 'vocals level' })).toBeDisabled()
  })

  it('stays enabled and keeps showing the true level while the stem is muted', async () => {
    await renderPainted({
      stems: [
        {
          ...loaded('vocals', AXIS_SECONDS),
          muted: true,
          audible: false,
          level: 0.5,
        },
      ],
    })

    const fader = screen.getByRole('slider', { name: 'vocals level' })
    expect(fader).toBeEnabled()
    expect(fader).toHaveValue('0.5')
  })

  it('stays enabled and keeps showing the true level while soloed out', async () => {
    await renderPainted({
      stems: [
        loaded('vocals', AXIS_SECONDS),
        { ...loaded('drums', AXIS_SECONDS), soloed: false, audible: false },
      ],
    })

    const fader = screen.getByRole('slider', { name: 'drums level' })
    expect(fader).toBeEnabled()
  })
})

// ---------------------------------------------------------------------------
// Feature 051: zoom and pan
//
// Every case here reads the window off the strip's `data-zoom` /
// `data-scroll-seconds`, which is what the ruler, the lanes and every seek are
// derived from — so asserting on those two numbers is asserting on all of it,
// in seconds rather than in pixels. Where a case is about what the *user*
// sees, it asserts on the tick labels or on the rectangles instead.
// ---------------------------------------------------------------------------

/** The strip, which carries the window as data. */
function tracks(): HTMLElement {
  const element = document.querySelector<HTMLElement>('.stem-timeline-tracks')
  if (element === null) {
    throw new Error('the timeline has no track strip')
  }
  return element
}

/** The current zoom ratio. */
function zoomRatio(): number {
  return Number(tracks().dataset.zoom)
}

/** Seconds scrolled past the left edge. */
function scrollSeconds(): number {
  return Number(tracks().dataset.scrollSeconds)
}

/** The seek control, which is also the surface `+`/`-` are pressed on. */
function surface(): HTMLElement {
  return screen.getByRole('slider', { name: 'Seek' })
}

function scrollThumb(): HTMLElement {
  return screen.getByTestId('stem-timeline-scroll-thumb')
}

/** Every tick label on the ruler, left to right. */
function tickLabels(): (string | null)[] {
  return [...document.querySelectorAll('.stem-timeline-tick')].map(
    (tick) => tick.textContent,
  )
}

/** A Ctrl+wheel notch at `clientX`: negative deltas zoom in, as a mouse does. */
function ctrlWheel(deltaY: number, clientX: number, times = 1): void {
  for (let index = 0; index < times; index += 1) {
    fireEvent.wheel(tracks(), { deltaY, clientX, ctrlKey: true })
  }
}

interface FixtureOptions {
  readonly stems?: readonly StemTimelineStem[]
  readonly durationSeconds?: number
  readonly positionSeconds?: number
  readonly engine?: StemPlayerEngine
  /** The committed region, as the engine snapshot would carry it. */
  readonly loopRegion?: LoopRegion | null
}

/**
 * A rendered timeline with its props to hand, so a case can push a new
 * playhead position through it the way the player would.
 */
async function fixture(options: FixtureOptions = {}) {
  const stems = options.stems ?? [loaded('vocals', AXIS_SECONDS)]
  const engine = options.engine ?? bufferEngine(stems.map((stem) => stem.name))
  const seeks: number[] = []
  const scrubs: number[] = []
  /**
   * How many times a gesture asked for a preview session (feature 052). An
   * object rather than a `let`, so the caller reads the live count.
   */
  const preview = { starts: 0 }
  /** Every loop commit, in order: a region, or `null` for a clear. */
  const loopRegions: (LoopRegion | null)[] = []
  const props: StemTimelineProps = {
    stems,
    durationSeconds: options.durationSeconds ?? AXIS_SECONDS,
    positionSeconds: options.positionSeconds ?? 0,
    ready: true,
    engine,
    onScrubStart: () => {
      preview.starts += 1
    },
    onScrub: (seconds) => scrubs.push(seconds),
    onScrubCancel: () => undefined,
    onSeek: (seconds) => seeks.push(seconds),
    loopRegion: options.loopRegion ?? null,
    onSetLoopRegion: (start, end) => loopRegions.push({ start, end }),
    onClearLoopRegion: () => loopRegions.push(null),
    onTogglePlayback: () => undefined,
    onToggleMute: () => undefined,
    onToggleSolo: () => undefined,
    onSetLevel: () => undefined,
  }
  const view = render(<StemTimeline {...props} />)
  await act(async () => {})
  return {
    seeks,
    scrubs,
    preview,
    loopRegions,
    engine,
    /** Re-render with some props changed, as the player re-renders it. */
    show: (next: Partial<StemTimelineProps>) => {
      view.rerender(<StemTimeline {...props} {...next} />)
    },
  }
}

describe('StemTimeline zoom', () => {
  it('zooms about the cursor on Ctrl+wheel, and rescales the ruler', async () => {
    await fixture()

    // 100 px along a 400 px strip of a minute is 0:15, and one notch shows
    // two thirds of what was on screen — so the window becomes 40 s with 0:15
    // still a quarter of the way along it, i.e. starting at 0:05.
    ctrlWheel(-100, 100)

    expect(zoomRatio()).toBeCloseTo(1.5, 6)
    expect(scrollSeconds()).toBeCloseTo(5, 6)
    // The ruler follows: the same ten-second ladder, but starting inside the
    // file and positioned against the scroll rather than against zero.
    expect(tickLabels()).toEqual(['0:10', '0:20', '0:30', '0:40'])
    expect(
      document.querySelector<HTMLElement>('.stem-timeline-tick')?.style
        .transform,
    ).toBe('translateX(50px)')
  })

  it('finds a finer tick ladder the further in it goes', async () => {
    await fixture()

    ctrlWheel(-100, 0, 6)

    // 1.5^6 ≈ 11.4, so 5.27 s are on screen and the ruler drops from
    // ten-second marks to one-second ones.
    expect(tickLabels()).toEqual([
      '0:00',
      '0:01',
      '0:02',
      '0:03',
      '0:04',
      '0:05',
    ])
  })

  it('zooms about the playhead from the toolbar and from the keyboard', async () => {
    await fixture({ positionSeconds: 30 })

    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))

    // The playhead is at 0:30, which is the middle of the strip: the window
    // shrinks to 40 s around it, so it starts at 0:10.
    expect(zoomRatio()).toBeCloseTo(1.5, 6)
    expect(scrollSeconds()).toBeCloseTo(10, 6)

    fireEvent.keyDown(surface(), { key: '+' })
    expect(zoomRatio()).toBeCloseTo(2.25, 6)

    fireEvent.keyDown(surface(), { key: '-' })
    expect(zoomRatio()).toBeCloseTo(1.5, 6)

    await userEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    expect(zoomRatio()).toBeCloseTo(1, 6)
    expect(scrollSeconds()).toBeCloseTo(0, 6)
  })

  it('goes back to the whole file, from the start, on Fit', async () => {
    await fixture()

    ctrlWheel(-100, 400, 4)
    expect(zoomRatio()).toBeGreaterThan(4)
    expect(scrollSeconds()).toBeGreaterThan(0)

    await userEvent.click(screen.getByRole('button', { name: 'Zoom to fit' }))

    expect(zoomRatio()).toBe(1)
    expect(scrollSeconds()).toBe(0)
    expect(tickLabels()).toEqual([
      '0:00',
      '0:10',
      '0:20',
      '0:30',
      '0:40',
      '0:50',
      '1:00',
    ])
  })

  it('stops at the whole file one way and at a second of it the other', async () => {
    await fixture()

    // Already fitted: there is nothing to zoom out to, and the controls say so.
    expect(screen.getByRole('button', { name: 'Zoom out' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Zoom to fit' })).toBeDisabled()
    ctrlWheel(100, 200, 3)
    expect(zoomRatio()).toBe(1)
    expect(scrollSeconds()).toBe(0)

    // Twenty notches of 1.5 is a magnification of three thousand; the window
    // stops at one second, which for a minute-long mix is a zoom of 60.
    ctrlWheel(-100, 0, 20)

    expect(zoomRatio()).toBe(60)
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeDisabled()
  })

  it('leaves a fitted timeline alone for the page to scroll', async () => {
    await fixture()

    // No `preventDefault`: at fit zoom there is nothing to pan, so the wheel
    // still belongs to the page it is over.
    const wheel = new WheelEvent('wheel', {
      deltaY: 100,
      bubbles: true,
      cancelable: true,
    })
    tracks().dispatchEvent(wheel)

    expect(wheel.defaultPrevented).toBe(false)
    expect(scrollSeconds()).toBe(0)
  })
})

describe('StemTimeline pan', () => {
  it('pans on a plain or shifted wheel, and stops at the end of the file', async () => {
    await fixture()
    // 40 s across 400 px: ten pixels to the second, from the very start.
    ctrlWheel(-100, 0)

    fireEvent.wheel(tracks(), { deltaY: 100 })
    expect(scrollSeconds()).toBeCloseTo(10, 6)

    fireEvent.wheel(tracks(), { deltaY: -50, shiftKey: true })
    expect(scrollSeconds()).toBeCloseTo(5, 6)

    // A trackpad's own horizontal axis is used when it sends one.
    fireEvent.wheel(tracks(), { deltaX: 50, deltaY: 0 })
    expect(scrollSeconds()).toBeCloseTo(10, 6)

    // The window may not show past the end: 60 s of material minus the 40 s
    // on screen is as far as it goes.
    fireEvent.wheel(tracks(), { deltaY: 1000 })
    expect(scrollSeconds()).toBeCloseTo(20, 6)
  })

  it('scrolls by dragging the thumb, which mirrors the window', async () => {
    await fixture()
    ctrlWheel(-100, 0)

    // Two thirds of the file is on screen, from its start.
    expect(scrollThumb().style.width).toBe(`${String((40 / 60) * 100)}%`)
    expect(scrollThumb().style.left).toBe('0%')

    // The thumb's track is the whole file across 400 px, so 100 px of drag is
    // 15 s wherever the zoom happens to be.
    fireEvent.pointerDown(scrollThumb(), { clientX: 0, pointerId: 1 })
    fireEvent.pointerMove(scrollThumb(), { clientX: 40, pointerId: 1 })
    fireEvent.pointerMove(scrollThumb(), { clientX: 100, pointerId: 1 })
    fireEvent.pointerUp(scrollThumb(), { pointerId: 1 })

    expect(scrollSeconds()).toBeCloseTo(15, 6)
    expect(scrollThumb().style.left).toBe('25%')

    // The ref is cleared on release, so a stray move afterwards scrolls
    // nothing.
    fireEvent.pointerMove(scrollThumb(), { clientX: 300, pointerId: 1 })
    expect(scrollSeconds()).toBeCloseTo(15, 6)
  })

  it('follows a playhead that leaves the window, and only then', async () => {
    const view = await fixture({ positionSeconds: 0 })
    ctrlWheel(-100, 0, 2)
    // 1.5² is 2.25, so 26.667 s are on screen, from the start.
    expect(scrollSeconds()).toBe(0)

    // Playback carries the playhead past the right edge: the window flips so
    // it reappears a tenth of the way in.
    // (The window is read back rounded to the millisecond, which is all the
    // precision a scroll position has any use for.)
    view.show({ positionSeconds: 30 })
    expect(scrollSeconds()).toBeCloseTo(30 - (60 / 2.25) * 0.1, 3)

    // The user pans away from it — and is not fought over it, because the
    // playhead has not moved since.
    fireEvent.wheel(tracks(), { deltaY: -100 })
    const panned = scrollSeconds()
    expect(panned).toBeLessThan(27)
    view.show({ positionSeconds: 30 })
    expect(scrollSeconds()).toBe(panned)
  })
})

describe('StemTimeline seeking under a moved window', () => {
  it('seeks to the absolute second under the pointer, zoomed and panned', async () => {
    const view = await fixture()

    // 1.5× from the left edge is a 40 s window at ten pixels to the second;
    // one wheel notch of pan puts its left edge at 0:10.
    ctrlWheel(-100, 0)
    fireEvent.wheel(tracks(), { deltaY: 100 })
    expect(scrollSeconds()).toBeCloseTo(10, 6)

    // 300 px in is 30 s into that window, which is 0:40 of the file. A reading
    // that ignored the scroll would say 0:45, and one that ignored the zoom
    // would say 0:55 — neither of which is what is under the pointer.
    fireEvent.pointerDown(surface(), { clientX: 300, pointerId: 1 })
    fireEvent.pointerUp(surface(), { pointerId: 1 })
    expect(view.seeks).toEqual([40])

    fireEvent.pointerDown(surface(), { clientX: 100, pointerId: 2 })
    fireEvent.pointerUp(surface(), { pointerId: 2 })
    expect(view.seeks).toEqual([40, 20])
  })
})

// ---------------------------------------------------------------------------
// Feature 053: loop regions
//
// Every case asserts in *seconds of audio*: the gesture is pixels on a strip,
// but what it must produce is an absolute region, whatever window happens to
// be on screen when it is drawn.
// ---------------------------------------------------------------------------

/** The x offset that means `seconds` in a fitted view of the whole axis. */
function xFor(seconds: number): number {
  return (seconds / AXIS_SECONDS) * WIDTH
}

/** The ruler's row: the loop-region drag surface. */
function rulerRow(): HTMLElement {
  return screen.getByTestId('stem-timeline-ruler-row')
}

/** The loop band, or `null` when none is drawn. */
function loopBand(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.stem-timeline-loop-region')
}

/** One pointer gesture across `element`, in client x offsets. */
function drag(element: HTMLElement, from: number, ...path: number[]): void {
  fireEvent.pointerDown(element, { clientX: from, pointerId: 1 })
  for (const x of path) {
    fireEvent.pointerMove(element, { clientX: x, pointerId: 1 })
  }
  fireEvent.pointerUp(element, { clientX: path.at(-1) ?? from, pointerId: 1 })
}

describe('StemTimeline loop regions', () => {
  it('commits one region for a whole ruler drag, in absolute seconds', async () => {
    const view = await fixture()

    fireEvent.pointerDown(rulerRow(), { clientX: xFor(9), pointerId: 1 })
    fireEvent.pointerMove(rulerRow(), { clientX: xFor(20), pointerId: 1 })
    fireEvent.pointerMove(rulerRow(), { clientX: xFor(30), pointerId: 1 })
    // Nothing has been committed yet: the drag is only drawn.
    expect(view.loopRegions).toEqual([])
    expect(loopBand()).not.toBeNull()

    fireEvent.pointerUp(rulerRow(), { pointerId: 1 })

    expect(view.loopRegions).toHaveLength(1)
    expect(view.loopRegions[0]?.start).toBeCloseTo(9, 6)
    expect(view.loopRegions[0]?.end).toBeCloseTo(30, 6)
    // A drag across the ruler is not a seek.
    expect(view.seeks).toEqual([])
  })

  it('normalises a right-to-left drag', async () => {
    const view = await fixture()

    drag(rulerRow(), xFor(30), xFor(9))

    expect(view.loopRegions).toHaveLength(1)
    expect(view.loopRegions[0]?.start).toBeCloseTo(9, 6)
    expect(view.loopRegions[0]?.end).toBeCloseTo(30, 6)
  })

  it('treats a plain click on the ruler as clear-and-seek', async () => {
    const view = await fixture({ loopRegion: { start: 10, end: 20 } })

    // Two pixels of travel is a click, not a drag.
    drag(rulerRow(), xFor(18), xFor(18) + 2)

    expect(view.loopRegions).toEqual([null])
    expect(view.seeks).toHaveLength(1)
    expect(view.seeks[0]).toBeCloseTo(18, 6)
  })

  it('draws a region from a shifted drag over the lanes, and seeks nothing', async () => {
    const view = await fixture()

    fireEvent.pointerDown(surface(), {
      clientX: xFor(6),
      pointerId: 1,
      shiftKey: true,
    })
    fireEvent.pointerMove(surface(), { clientX: xFor(24), pointerId: 1 })
    fireEvent.pointerUp(surface(), { pointerId: 1 })

    expect(view.loopRegions).toHaveLength(1)
    expect(view.loopRegions[0]?.start).toBeCloseTo(6, 6)
    expect(view.loopRegions[0]?.end).toBeCloseTo(24, 6)
    // The playhead was never touched: a shifted drag is not a scrub.
    expect(view.seeks).toEqual([])
    expect(view.scrubs).toEqual([])
  })

  it('moves one edge by its handle, and commits once', async () => {
    const view = await fixture({ loopRegion: { start: 10, end: 20 } })

    drag(
      screen.getByTestId('stem-timeline-loop-handle-end'),
      xFor(20),
      xFor(35),
      xFor(45),
    )

    expect(view.loopRegions).toHaveLength(1)
    expect(view.loopRegions[0]?.start).toBeCloseTo(10, 6)
    expect(view.loopRegions[0]?.end).toBeCloseTo(45, 6)
  })

  it('clears when an edge is dragged onto the other one', async () => {
    const view = await fixture({ loopRegion: { start: 10, end: 20 } })

    drag(
      screen.getByTestId('stem-timeline-loop-handle-start'),
      xFor(10),
      xFor(20),
    )

    expect(view.loopRegions).toEqual([null])
  })

  it('commits nothing when the gesture is cancelled', async () => {
    const view = await fixture()

    fireEvent.pointerDown(rulerRow(), { clientX: xFor(9), pointerId: 1 })
    fireEvent.pointerMove(rulerRow(), { clientX: xFor(30), pointerId: 1 })
    expect(loopBand()).not.toBeNull()

    fireEvent.pointerCancel(rulerRow(), { pointerId: 1 })

    expect(view.loopRegions).toEqual([])
    expect(loopBand()).toBeNull()
  })

  it('ignores a second release of the same gesture', async () => {
    const view = await fixture()

    fireEvent.pointerDown(rulerRow(), { clientX: xFor(9), pointerId: 1 })
    fireEvent.pointerMove(rulerRow(), { clientX: xFor(30), pointerId: 1 })
    fireEvent.pointerUp(rulerRow(), { pointerId: 1 })
    fireEvent.pointerUp(rulerRow(), { pointerId: 1 })

    expect(view.loopRegions).toHaveLength(1)
  })

  it('drags an absolute region while zoomed and panned', async () => {
    const view = await fixture()

    // 1.5× from the left edge is a 40 s window at ten pixels to the second;
    // one notch of pan puts its left edge at 0:10 — the same window the seek
    // regression above uses.
    ctrlWheel(-100, 0)
    fireEvent.wheel(tracks(), { deltaY: 100 })
    expect(scrollSeconds()).toBeCloseTo(10, 6)

    drag(rulerRow(), 100, 300)

    // 100 px in is 0:20 and 300 px in is 0:40. A reading that dropped the
    // scroll would say 0:10–0:30, and one that dropped the zoom 0:15–0:45.
    expect(view.loopRegions).toHaveLength(1)
    expect(view.loopRegions[0]?.start).toBeCloseTo(20, 6)
    expect(view.loopRegions[0]?.end).toBeCloseTo(40, 6)
  })

  it('positions the band from the viewport, and moves it with the window', async () => {
    await fixture({ loopRegion: { start: 15, end: 30 } })

    // Fitted: 400 px over a minute, so 0:15 is 100 px in and the band is
    // 100 px wide.
    const fitted = loopBand()
    expect(Number.parseFloat(fitted?.style.left ?? '')).toBeCloseTo(100, 6)
    expect(Number.parseFloat(fitted?.style.width ?? '')).toBeCloseTo(100, 6)

    // One notch of zoom about 100 px: a 40 s window starting at 0:05, at ten
    // pixels to the second. The band is the same seconds, drawn wider.
    ctrlWheel(-100, 100)

    const zoomed = loopBand()
    expect(Number.parseFloat(zoomed?.style.left ?? '')).toBeCloseTo(100, 4)
    expect(Number.parseFloat(zoomed?.style.width ?? '')).toBeCloseTo(150, 4)
  })

  it('draws nothing for a region that is off the window entirely', async () => {
    await fixture({ loopRegion: { start: 1, end: 3 } })
    expect(loopBand()).not.toBeNull()

    // All the way in at the far end: the region is off the left edge.
    ctrlWheel(-100, 400, 20)

    expect(scrollSeconds()).toBeGreaterThan(3)
    expect(loopBand()).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Feature 052: the audible scrub preview
//
// The timeline knows nothing about audio — what it owes 052 is that a seek
// drag *announces itself* before it reports a position, exactly once, and that
// a loop-region gesture never does.
// ---------------------------------------------------------------------------

describe('StemTimeline scrub preview', () => {
  it('opens one session per drag, before the first position', async () => {
    const view = await fixture()

    fireEvent.pointerDown(surface(), { clientX: xFor(9), pointerId: 1 })

    // Once, on the press — and the press's own position arrives after it, so
    // the player always has a session open to sound it into.
    expect(view.preview.starts).toBe(1)
    expect(view.scrubs).toHaveLength(1)
    expect(view.scrubs[0]).toBeCloseTo(9, 6)

    fireEvent.pointerMove(surface(), { clientX: xFor(20), pointerId: 1 })
    fireEvent.pointerMove(surface(), { clientX: xFor(30), pointerId: 1 })

    // Every move is a position to audition; still one session.
    expect(view.preview.starts).toBe(1)
    expect(view.scrubs).toHaveLength(3)

    fireEvent.pointerUp(surface(), { pointerId: 1 })
    expect(view.seeks).toHaveLength(1)
    expect(view.preview.starts).toBe(1)
  })

  it.each([
    [
      'a ruler drag',
      () => {
        drag(rulerRow(), xFor(9), xFor(30))
      },
    ],
    [
      'a shifted lane drag',
      () => {
        fireEvent.pointerDown(surface(), {
          clientX: xFor(6),
          pointerId: 1,
          shiftKey: true,
        })
        fireEvent.pointerMove(surface(), { clientX: xFor(24), pointerId: 1 })
        fireEvent.pointerUp(surface(), { pointerId: 1 })
      },
    ],
  ])('opens no session for %s', async (_label, gesture) => {
    const view = await fixture()

    gesture()

    // Drawing a region is not auditioning a position: nothing is previewed,
    // and nothing is scrubbed.
    expect(view.preview.starts).toBe(0)
    expect(view.scrubs).toEqual([])
    expect(view.loopRegions).toHaveLength(1)
  })

  it('opens no session for an edge handle', async () => {
    const view = await fixture({ loopRegion: { start: 10, end: 20 } })

    drag(
      screen.getByTestId('stem-timeline-loop-handle-end'),
      xFor(20),
      xFor(35),
    )

    expect(view.preview.starts).toBe(0)
    expect(view.scrubs).toEqual([])
  })
})

describe('StemTimeline high-resolution tiles', () => {
  /**
   * A five-minute axis, which is what makes the base peaks *coarser* than the
   * pixels: 8192 buckets over 300 s of the fake encoding's 100 Hz is 3.66
   * sample frames a bucket, so a window of a few seconds asks for more detail
   * than they hold.
   */
  const TILE_AXIS = 300

  /** Silence with a single full-scale sample at 0.5 s. */
  const IMPULSE = Array.from({ length: TILE_AXIS * 100 }, (_, index) =>
    index === 50 ? 1 : 0,
  )

  /** An engine that records every buffer it is asked for. */
  function recordingEngine(): {
    engine: StemPlayerEngine
    reads: string[]
  } {
    const inner = bufferEngine(['vocals'], IMPULSE)
    const reads: string[] = []
    return {
      reads,
      engine: {
        ...inner,
        getStemBuffer: (name: string) => {
          reads.push(name)
          return inner.getStemBuffer(name)
        },
      },
    }
  }

  async function renderTiled(): Promise<{ reads: string[] }> {
    const { engine, reads } = recordingEngine()
    await fixture({
      stems: [loaded('vocals', TILE_AXIS)],
      durationSeconds: TILE_AXIS,
      engine,
    })
    reads.length = 0
    return { reads }
  }

  it('reads samples once per frame, not once per wheel event', async () => {
    const { reads } = await renderTiled()

    // 1.5^8 ≈ 25.6, so about 11.7 s are on screen — past the point where a
    // pixel covers less than a base bucket, and a tile is worth having.
    ctrlWheel(-100, 0, 8)
    expect(reads, 'the storm itself reads nothing').toEqual([])

    flushFrame()

    expect(reads, 'one tile for the window that was landed on').toEqual([
      'vocals',
    ])
  })

  it('keeps the last few tiles, so panning back costs nothing', async () => {
    const { reads } = await renderTiled()
    ctrlWheel(-100, 0, 8)
    flushFrame()
    reads.length = 0

    // Somewhere new: that window has to be read from the samples.
    fireEvent.wheel(tracks(), { deltaY: 100 })
    flushFrame()
    expect(reads).toEqual(['vocals'])

    // …and back to exactly where it was, which is still in the cache.
    reads.length = 0
    fireEvent.wheel(tracks(), { deltaY: -1000 })
    flushFrame()
    expect(scrollSeconds()).toBe(0)
    expect(reads).toEqual([])
  })

  it('draws the tile, which is sharper than the base peaks are', async () => {
    await renderTiled()

    // All the way in: one second across 400 px, a quarter of a sample frame
    // per column.
    ctrlWheel(-100, 0, 15)
    expect(zoomRatio()).toBe(TILE_AXIS)
    canvas.reset()

    flushFrame()

    // A column that has the impulse in it is drawn from near the top of the
    // lane; a silent one is a hairline through its middle. Four columns share
    // the one sample the tile puts them each a quarter of. Drawn from the base
    // peaks the same instant would have smeared it across fifteen, because one
    // base bucket is 3.66 sample frames wide and covers 15 columns at this
    // zoom.
    const loud = canvas.fillRects.filter((rect) => rect.y < 31)
    expect(loud).toHaveLength(4)
  })
})
