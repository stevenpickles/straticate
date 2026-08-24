/**
 * A recording fake of the slice of the Web Audio API the stem player uses.
 *
 * jsdom implements no Web Audio API at all — no `AudioContext`, no
 * `decodeAudioData`, no `AudioBufferSourceNode` — so the engine takes its
 * context from an injectable factory and tests supply this instead. Nothing
 * here makes a sound: every node records what it was asked to do, so tests
 * assert on **what was scheduled** (start times, offsets, gain values, node
 * connections) rather than on audible output.
 *
 * Buffer durations travel in the bytes: {@link stemBytes} encodes a duration
 * as a byte length and {@link FakeAudioContext.decodeAudioData} decodes it
 * back, so a fake loader can give each stem its own length with no shared
 * lookup table.
 */

import type {
  AudioEngineBuffer,
  AudioEngineContext,
  AudioEngineGainNode,
  AudioEngineNode,
  AudioEngineSourceNode,
} from '../audio/engine'

/** Bytes per second of "audio" in the fake encoding. */
const BYTES_PER_SECOND = 100

/** Fake stem bytes whose decoded duration is `durationSeconds`. */
export function stemBytes(durationSeconds: number): ArrayBuffer {
  return new ArrayBuffer(Math.round(durationSeconds * BYTES_PER_SECOND))
}

/** A recording `AudioParam`. */
export class FakeAudioParam {
  value = 1
}

/** A recording `AudioNode`: remembers what it was connected to. */
export class FakeAudioNode implements AudioEngineNode {
  /** Every node this one was connected to, in order. */
  readonly connections: AudioEngineNode[] = []
  /** How many times `disconnect()` was called. */
  disconnectCount = 0

  connect(destination: AudioEngineNode): void {
    this.connections.push(destination)
  }

  disconnect(): void {
    this.disconnectCount += 1
  }
}

/** A recording `GainNode`. */
export class FakeGainNode extends FakeAudioNode implements AudioEngineGainNode {
  readonly gain = new FakeAudioParam()
}

/** One `start(when, offset)` call, exactly as the engine scheduled it. */
export interface ScheduledStart {
  readonly when: number | undefined
  readonly offset: number | undefined
}

/** A recording `AudioBufferSourceNode`. */
export class FakeSourceNode
  extends FakeAudioNode
  implements AudioEngineSourceNode
{
  buffer: AudioEngineBuffer | null = null
  onended: ((event: Event) => void) | null = null
  /** The `start()` call this source received, or `null`. */
  started: ScheduledStart | null = null
  /** How many times `stop()` was called. */
  stopCount = 0

  start(when?: number, offset?: number): void {
    this.started = { when, offset }
  }

  stop(): void {
    this.stopCount += 1
  }
}

/** A recording `AudioContext`. */
export class FakeAudioContext implements AudioEngineContext {
  /** The audio clock; tests advance it directly to move the playhead. */
  currentTime = 0
  /** `running` unless a test starts it `suspended` to exercise autoplay. */
  state: string
  readonly destination = new FakeAudioNode()
  /** Every gain node created, in creation order. */
  readonly gains: FakeGainNode[] = []
  /** Every source node created, in creation order. */
  readonly sources: FakeSourceNode[] = []
  /** Every buffer handed to `decodeAudioData`, in order. */
  readonly decoded: ArrayBuffer[] = []
  /** How many times `resume()` was called. */
  resumeCount = 0
  /** How many times `close()` was called. */
  closeCount = 0

  constructor(state: string = 'running') {
    this.state = state
  }

  createGain(): FakeGainNode {
    const gain = new FakeGainNode()
    this.gains.push(gain)
    return gain
  }

  createBufferSource(): FakeSourceNode {
    const source = new FakeSourceNode()
    this.sources.push(source)
    return source
  }

  decodeAudioData(data: ArrayBuffer): Promise<AudioEngineBuffer> {
    this.decoded.push(data)
    return Promise.resolve({ duration: data.byteLength / BYTES_PER_SECOND })
  }

  resume(): Promise<void> {
    this.resumeCount += 1
    this.state = 'running'
    return Promise.resolve()
  }

  close(): Promise<void> {
    this.closeCount += 1
    this.state = 'closed'
    return Promise.resolve()
  }

  /** Every source created since `index`, i.e. one transport generation. */
  sourcesFrom(index: number): FakeSourceNode[] {
    return this.sources.slice(index)
  }
}
