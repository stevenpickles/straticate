/**
 * Type-level smoke test for the generated contract types.
 *
 * Building the typed sample objects below is the real test — it fails
 * `npm run typecheck` if the generated types drift from the documented
 * contract. The runtime assertions are trivial by design.
 */

import { describe, expect, it } from 'vitest'

import type {
  AudioFile,
  ComputeDevice,
  Job,
  JobState,
  RuntimeMetricsEvent,
  SeparationConfiguration,
  SeparationMode,
  WebSocketEvent,
} from './types'

const configuration: SeparationConfiguration = {
  audio_id: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
  mode_id: 'vocals',
  quality_id: 'high_quality',
  device_id: 'cuda:0',
  stereo_handling: 'as_is',
}

const job: Job = {
  id: '01BX5ZZKBKACTAV9WEVGEMMVRZ',
  audio_id: configuration.audio_id,
  configuration,
  model_id: 'vocals-hq-001',
  state: 'separating',
  progress: 0.65,
  created_at: '2026-08-23T12:00:00Z',
  started_at: '2026-08-23T12:00:05Z',
  finished_at: null,
  error: null,
  result: null,
}

const audioFile: AudioFile = {
  id: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
  filename: 'Midnight Train.flac',
  size_bytes: 44771328,
  uploaded_at: '2026-08-23T12:00:00Z',
  metadata: {
    duration_seconds: 227.4,
    container: 'flac',
    codec: 'flac',
    channels: 2,
    sample_rate_hz: 44100,
    bit_depth: 24,
    bit_rate_bps: 1411000,
  },
}

const device: ComputeDevice = {
  id: 'cuda:0',
  backend: 'cuda',
  name: 'NVIDIA GeForce RTX 5090',
  memory_total_bytes: 34359738368,
}

const mode: SeparationMode = {
  id: 'vocals',
  display_name: 'Vocal Isolation',
  stems: ['vocals', 'instrumental'],
  quality_options: [
    { id: 'fast', display_name: 'Fast', model_id: 'vocals-fast-001' },
    {
      id: 'high_quality',
      display_name: 'High Quality',
      model_id: 'vocals-hq-001',
    },
  ],
}

const metricsEvent: RuntimeMetricsEvent = {
  type: 'runtime_metrics',
  job_id: job.id,
  model: {
    id: 'vocals-hq-001',
    display_name: 'Vocals — High Quality',
    architecture: 'mel_band_roformer',
    version: '1.0',
    separation_mode: 'vocals',
    stem_count: 2,
  },
  gpu: null, // CPU run: the whole GPU block is null
  processing: {
    stage: 'separating',
    chunks_completed: 31,
    chunks_total: 48,
    elapsed_seconds: 18.2,
    audio_processed_seconds: 148.0,
    realtime_factor: 7.9,
  },
}

/** Exhaustive narrowing over the event union proves the discriminator works. */
function describeEvent(event: WebSocketEvent): string {
  switch (event.type) {
    case 'job_created':
      return `created ${event.job.id}`
    case 'job_started':
      return `started at ${event.started_at}`
    case 'job_stage_changed':
      return `${event.previous_stage} -> ${event.stage}`
    case 'job_progress':
      return `${String(event.chunks_completed)}/${String(event.chunks_total)}`
    case 'runtime_metrics':
      return `rtf ${String(event.processing.realtime_factor)}`
    case 'job_completed':
      return `completed with ${String(event.result.stems.length)} stems`
    case 'job_cancelled':
      return `cancelled during ${event.stage_at_cancellation}`
    case 'job_failed':
      return `failed: ${event.error.code}`
  }
}

describe('generated API types', () => {
  it('typed sample entities compile and hold their values', () => {
    const terminal: JobState[] = ['completed', 'cancelled', 'failed']
    expect(terminal).not.toContain(job.state)
    expect(audioFile.metadata.channels).toBe(2)
    expect(device.backend).toBe('cuda')
    expect(mode.quality_options).toHaveLength(2)
  })

  it('narrows WebSocket events by their type discriminator', () => {
    const progress: WebSocketEvent = {
      type: 'job_progress',
      job_id: job.id,
      stage: 'separating',
      progress: 0.65,
      chunks_completed: 31,
      chunks_total: 48,
      elapsed_seconds: 18.2,
      audio_processed_seconds: 148.0,
      audio_total_seconds: 227.4,
    }
    expect(describeEvent(progress)).toBe('31/48')
    expect(describeEvent(metricsEvent)).toBe('rtf 7.9')
    expect(describeEvent({ type: 'job_created', job_id: job.id, job })).toBe(
      `created ${job.id}`,
    )
  })
})
