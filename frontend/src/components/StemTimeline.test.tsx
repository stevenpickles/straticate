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
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StemTimeline, type StemTimelineStem } from './StemTimeline'
import type {
  AudioEngineBuffer,
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
  error: null,
}

/**
 * An engine that does nothing but hand out decoded buffers, which is the only
 * thing the timeline asks one for. Every other member is present because
 * `StemPlayerEngine` declares it, and inert because nothing here calls it.
 */
function bufferEngine(names: readonly string[]): StemPlayerEngine {
  const buffers = new Map<string, AudioEngineBuffer>(
    names.map((name) => [
      name,
      new FakeAudioBuffer(stemBytesWithSamples(LOUD)),
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
    currentTime: () => 0,
    getStemBuffer: (name: string) => buffers.get(name) ?? null,
    getSnapshot: () => EMPTY_SNAPSHOT,
    subscribe: () => () => undefined,
    dispose: () => undefined,
  }
}

/** A loaded, audible stem of `durationSeconds`. */
function loaded(name: string, durationSeconds: number): StemTimelineStem {
  return {
    name,
    status: 'loaded',
    muted: false,
    soloed: false,
    audible: true,
    durationSeconds,
  }
}

interface RenderOptions {
  readonly stems: readonly StemTimelineStem[]
  readonly durationSeconds?: number
  readonly positionSeconds?: number
  readonly ready?: boolean
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
      onScrub={() => undefined}
      onScrubCancel={() => undefined}
      onSeek={() => undefined}
      onTogglePlayback={() => undefined}
      onToggleMute={() => undefined}
      onToggleSolo={() => undefined}
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

beforeEach(() => {
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
        onScrub={() => undefined}
        onScrubCancel={() => undefined}
        onSeek={() => undefined}
        onTogglePlayback={() => undefined}
        onToggleMute={() => undefined}
        onToggleSolo={() => undefined}
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
        onScrub={() => undefined}
        onScrubCancel={() => undefined}
        onSeek={() => undefined}
        onTogglePlayback={() => undefined}
        onToggleMute={(name) => muted.push(name)}
        onToggleSolo={(name) => soloed.push(name)}
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
