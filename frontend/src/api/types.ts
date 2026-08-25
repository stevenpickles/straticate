/**
 * Friendly aliases over the generated OpenAPI types.
 *
 * App code imports contract types from this module — never from
 * `./generated/api` directly. The generated file is produced from the
 * backend's exported OpenAPI document (`npm run generate:api` after
 * `uv run python -m straticate.scripts.export_openapi` in `backend/`) and is
 * committed so frontend CI never needs the backend.
 */

import type { components } from './generated/api'

// System
export type HealthStatus = components['schemas']['HealthStatus']
export type VersionInfo = components['schemas']['VersionInfo']

// Errors
export type ErrorInfo = components['schemas']['ErrorInfo']
export type ErrorEnvelope = components['schemas']['ErrorEnvelope']

// Audio
export type AudioMetadata = components['schemas']['AudioMetadata']
export type AudioFile = components['schemas']['AudioFile']

// Models and modes
export type ModelRequirements = components['schemas']['ModelRequirements']
export type ModelInstallState = components['schemas']['ModelInstallState']
export type ModelInstallation = components['schemas']['ModelInstallation']
export type ModelLicensing = components['schemas']['ModelLicensing']
export type Model = components['schemas']['Model']
export type QualityOption = components['schemas']['QualityOption']
export type SeparationMode = components['schemas']['SeparationMode']

// Devices
export type ComputeDevice = components['schemas']['ComputeDevice']

// Jobs
export type JobState = components['schemas']['JobState']
export type SeparationConfiguration =
  components['schemas']['SeparationConfiguration']
export type Job = components['schemas']['Job']
export type Stem = components['schemas']['Stem']
export type SeparationResultMetrics =
  components['schemas']['SeparationResultMetrics']
export type SeparationResult = components['schemas']['SeparationResult']

// Export
export type ExportFormat = components['schemas']['ExportFormat']

// WebSocket events (discriminated on `type`)
export type ModelInfo = components['schemas']['ModelInfo']
export type GpuMetrics = components['schemas']['GpuMetrics']
export type ProcessingMetrics = components['schemas']['ProcessingMetrics']
export type JobCreatedEvent = components['schemas']['JobCreatedEvent']
export type JobStartedEvent = components['schemas']['JobStartedEvent']
export type JobStageChangedEvent = components['schemas']['JobStageChangedEvent']
export type JobProgressEvent = components['schemas']['JobProgressEvent']
export type RuntimeMetricsEvent = components['schemas']['RuntimeMetricsEvent']
export type JobCompletedEvent = components['schemas']['JobCompletedEvent']
export type JobCancelledEvent = components['schemas']['JobCancelledEvent']
export type JobFailedEvent = components['schemas']['JobFailedEvent']

/** Union of every WebSocket event, discriminated on `type`. */
export type WebSocketEvent = components['schemas']['WebSocketEvent']
