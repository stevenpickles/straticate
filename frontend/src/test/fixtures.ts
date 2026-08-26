/** Shared contract-shaped fixtures for tests. */
import type {
  AudioFile,
  Job,
  Model,
  ModelInstallation,
  ModelLicensing,
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
  stereo_handling: 'as_is',
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
 * A `runtime_metrics` payload from the development fake separator
 * (feature 014). Its device block is honestly labelled `backend: "fake"` —
 * `backend` is an open set (ARCHITECTURE.md §10), so the panel must render
 * it exactly like any other device and never assume "device" means "GPU".
 */
export const sampleFakeDeviceRuntimeMetrics: RuntimeMetricsEvent = {
  type: 'runtime_metrics',
  job_id: sampleJobId,
  model: {
    id: 'fake-vocals-001',
    display_name: 'Fake Vocals (development)',
    architecture: 'fake',
    version: '1.0',
    separation_mode: 'vocals',
    stem_count: 2,
  },
  gpu: {
    device_id: 'fake:0',
    name: 'Straticate Fake Accelerator',
    backend: 'fake',
    memory_allocated_bytes: 1503238553,
    memory_peak_bytes: 1610612736,
    memory_total_bytes: 8589934592,
    utilization: 0.42,
    temperature_celsius: 48,
  },
  processing: {
    stage: 'loading_model',
    chunks_completed: 0,
    chunks_total: 46,
    elapsed_seconds: 0.4,
    audio_processed_seconds: 0,
    realtime_factor: 0.5,
  },
}

/**
 * A `runtime_metrics` payload from a run with no compute device: the whole
 * `gpu` block is null (the "running on CPU" shape).
 */
export const sampleCpuRuntimeMetrics: RuntimeMetricsEvent = {
  ...sampleRuntimeMetrics,
  gpu: null,
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

/**
 * Size of the representative downloadable weights artifact: 870 MiB, which
 * `formatFileSize` renders as `870 MB`. Close to the real `vocals-hq-001`
 * artifact, so a test asserting "the UI names the download size" is asserting
 * something the size of the thing a user actually waits for.
 */
export const sampleWeightsBytes = 870 * 1024 * 1024

/**
 * A catalogued model whose weights are a download and are **not** installed —
 * exactly what a fresh checkout offers since feature 032.
 */
export const sampleInstallableModel: Model = {
  id: 'vocals-hq-001',
  display_name: 'Vocals — High Quality',
  architecture: 'mel_band_roformer',
  version: '1.0',
  development_only: false,
  separation_mode: 'vocals',
  quality_tier: 'high_quality',
  stems: ['vocals', 'instrumental'],
  sample_rate: 44100,
  requirements: { recommended_vram_mb: 8192, minimum_ram_mb: null },
  capabilities: { cuda: true, cpu: true },
  licensing: null,
  installation: {
    state: 'available',
    requires_download: true,
    total_bytes: sampleWeightsBytes,
    downloaded_bytes: null,
    progress: null,
    error: null,
  },
}

/**
 * A model that needs no weights at all — a built-in separator, which feature
 * 025 defines as `installed` by definition and never offers as a download.
 */
export const sampleBuiltInModel: Model = {
  ...sampleInstallableModel,
  id: 'fake-vocals-001',
  display_name: 'Fake Vocals (development)',
  architecture: 'fake',
  development_only: true,
  quality_tier: 'balanced',
  installation: {
    state: 'installed',
    requires_download: false,
    total_bytes: null,
    downloaded_bytes: null,
    progress: null,
    error: null,
  },
}

/**
 * {@link sampleInstallableModel} with its `installation` block overridden —
 * the four states of one model, without restating the rest of the manifest.
 */
export function modelInstalling(
  installation: Partial<ModelInstallation>,
  model: Model = sampleInstallableModel,
): Model {
  return {
    ...model,
    installation: {
      state: 'available',
      requires_download: true,
      total_bytes: sampleWeightsBytes,
      downloaded_bytes: null,
      progress: null,
      error: null,
      ...installation,
    },
  }
}

/**
 * Terms as permissive as this project has ever shipped: the real
 * `vocals-hq-001` entry, whose author relicensed the weights to MIT and asks
 * only for the credit the catalog carries.
 */
export const samplePermissiveLicensing: ModelLicensing = {
  code_license: 'MIT',
  weights_license: 'MIT',
  redistribution_permitted: true,
  commercial_use_permitted: true,
  attribution:
    'Weights: Kim Vocal 2 (Mel-Band RoFormer) by Kimberley Jensen. Architecture: vendored from openmirlab/melband-roformer-infer.',
}

/**
 * The shape feature 027 is blocked on: **MIT code, and nothing at all said
 * about the weights.** A UI that folds these two into one licence line tells
 * the user an 870 MB download is free to use on the strength of a fact about
 * the source code.
 */
export const sampleSilentWeightsLicensing: ModelLicensing = {
  code_license: 'MIT',
  weights_license: null,
  redistribution_permitted: null,
  commercial_use_permitted: null,
  attribution: null,
}

/**
 * Weights that are **more restrictive than their code and stated in words** —
 * the CC-BY-NC-ish case the project owner has cleared for personal use, plus
 * an attribution that is a binding condition rather than a request.
 */
export const sampleRestrictiveLicensing: ModelLicensing = {
  code_license: 'MIT',
  weights_license:
    'Research and personal use only; ask the authors before any commercial use.',
  redistribution_permitted: false,
  commercial_use_permitted: false,
  attribution: 'Weights: Example Separator by the Example Lab.',
}

/**
 * {@link sampleInstallableModel} with its `licensing` block overridden, so a
 * test can state exactly the terms it is about.
 */
export function modelLicensed(
  licensing: ModelLicensing | null,
  model: Model = sampleInstallableModel,
): Model {
  return { ...model, licensing }
}
