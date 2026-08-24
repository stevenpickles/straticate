/** Shared contract-shaped fixtures for tests. */
import type {
  AudioFile,
  Job,
  RuntimeMetricsEvent,
  SeparationConfiguration,
  SeparationMode,
  SeparationResult,
} from '../api/types'

/** A representative backend-registered audio file. */
export const sampleAudioFile: AudioFile = {
  id: '01SAMPLEAUDIOULID0000000000',
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

/** ULID of the job used by the job fixtures below. */
export const sampleJobId = '01SAMPLEJOBULID00000000000'

/** A representative create-job request body. */
export const sampleConfiguration: SeparationConfiguration = {
  audio_id: sampleAudioFile.id,
  mode_id: 'vocals',
  quality_id: 'high_quality',
  device_id: 'cuda:0',
}

/** A representative queued job, as returned by `POST /jobs`. */
export const sampleJob: Job = {
  id: sampleJobId,
  audio_id: sampleAudioFile.id,
  configuration: sampleConfiguration,
  model_id: 'vocals-hq-001',
  state: 'queued',
  progress: 0,
  created_at: '2026-08-23T12:00:00Z',
  started_at: null,
  finished_at: null,
  error: null,
  result: null,
}

/** A representative separation result for a completed job. */
export const sampleResult: SeparationResult = {
  job_id: sampleJobId,
  model_id: 'vocals-hq-001',
  stems: [
    {
      name: 'vocals',
      duration_seconds: 227.4,
      sample_rate_hz: 44100,
      channels: 2,
    },
    {
      name: 'instrumental',
      duration_seconds: 227.4,
      sample_rate_hz: 44100,
      channels: 2,
    },
  ],
  metrics: { processing_seconds: 28.8, realtime_factor: 7.9 },
}

/** A representative `runtime_metrics` payload (GPU present, NVML available). */
export const sampleRuntimeMetrics: RuntimeMetricsEvent = {
  type: 'runtime_metrics',
  job_id: sampleJobId,
  model: {
    id: 'vocals-hq-001',
    display_name: 'Vocals — High Quality',
    architecture: 'mel_band_roformer',
    version: '1.0',
    separation_mode: 'vocals',
    stem_count: 2,
  },
  gpu: {
    device_id: 'cuda:0',
    name: 'NVIDIA GeForce RTX 5090',
    backend: 'cuda',
    memory_allocated_bytes: 9234179686,
    memory_peak_bytes: 10133099161,
    memory_total_bytes: 34359738368,
    utilization: 0.91,
    temperature_celsius: 63,
  },
  processing: {
    stage: 'separating',
    chunks_completed: 31,
    chunks_total: 48,
    elapsed_seconds: 18.2,
    audio_processed_seconds: 148.0,
    realtime_factor: 7.9,
  },
}

/**
 * Representative `GET /separation-modes` payload covering the shapes the
 * configure UI has to handle: a two-stem mode with several quality tiers
 * and a four-stem mode served by a single model (one tier).
 */
export const sampleSeparationModes: SeparationMode[] = [
  {
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
  },
  {
    id: 'standard_stems',
    display_name: 'Standard Stems',
    stems: ['vocals', 'drums', 'bass', 'other'],
    quality_options: [
      {
        id: 'balanced',
        display_name: 'Balanced',
        model_id: 'standard-balanced-001',
      },
    ],
  },
]
