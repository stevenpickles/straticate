"""WebSocket event payloads (server → client push).

All messages on ``WS /api/v1/ws`` are JSON objects discriminated by ``type``;
``WebSocketEvent`` is the discriminated union over every event model. Events
are notifications, not the database — REST (``GET /jobs/{job_id}``) remains the
source of truth for reconnect/refresh. Clients must ignore unknown ``type``
values for forward compatibility.
"""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field

from straticate.schemas.common import ErrorInfo
from straticate.schemas.jobs import Job, JobState, SeparationResult


class ModelInfo(BaseModel):
    """Summary of the model in use, embedded in runtime metrics."""

    id: str = Field(description="Logical model ID.")
    display_name: str = Field(description="Human-readable model name.")
    architecture: str = Field(description="Implementation family (open set).")
    version: str = Field(description="Model version string.")
    separation_mode: str = Field(description="Logical mode ID this model serves.")
    stem_count: int = Field(ge=2, description="Number of stems this model produces.")


class GpuMetrics(BaseModel):
    """GPU statistics sampled while a job runs.

    The whole block is null in ``RuntimeMetricsEvent`` when running on CPU;
    ``utilization`` and ``temperature_celsius`` are null when NVML is
    unavailable (NVML is optional — basic operation never requires it).
    """

    device_id: str = Field(description='Logical device ID, e.g. "cuda:0".')
    name: str = Field(description="Human-readable device name.")
    backend: str = Field(description="Compute backend identifier (open set).")
    memory_allocated_bytes: int = Field(ge=0, description="Currently allocated memory in bytes.")
    memory_peak_bytes: int = Field(ge=0, description="Peak allocated memory in bytes.")
    memory_total_bytes: int = Field(ge=0, description="Total device memory in bytes.")
    utilization: Annotated[float, Field(ge=0.0, le=1.0)] | None = Field(
        description="GPU utilization in [0, 1]; null when NVML is unavailable."
    )
    temperature_celsius: float | None = Field(
        description="GPU temperature in °C; null when NVML is unavailable."
    )


class ProcessingMetrics(BaseModel):
    """Processing statistics sampled while a job runs."""

    stage: JobState = Field(description="Current processing stage.")
    chunks_completed: int = Field(ge=0, description="Chunks processed so far.")
    chunks_total: int = Field(ge=0, description="Total chunks to process.")
    elapsed_seconds: float = Field(ge=0, description="Elapsed processing time in seconds.")
    audio_processed_seconds: float = Field(ge=0, description="Audio processed so far in seconds.")
    realtime_factor: float = Field(ge=0, description="RTF = audio duration / processing duration.")


class JobCreatedEvent(BaseModel):
    """A new job entered the queue; carries the full job for immediate render."""

    type: Literal["job_created"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    job: Job = Field(description="The full job as created.")


class JobStartedEvent(BaseModel):
    """A queued job began processing."""

    type: Literal["job_started"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    started_at: AwareDatetime = Field(description="Processing start timestamp.")


class JobStageChangedEvent(BaseModel):
    """The job transitioned between processing stages."""

    type: Literal["job_stage_changed"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    stage: JobState = Field(description="Stage the job entered.")
    previous_stage: JobState = Field(description="Stage the job left.")


class JobProgressEvent(BaseModel):
    """Chunk-grained progress (throttled server-side, ≤ ~4 Hz)."""

    type: Literal["job_progress"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    stage: JobState = Field(description="Current processing stage.")
    progress: float = Field(
        ge=0.0, le=1.0, description="Progress in [0, 1] (chunks_completed / chunks_total)."
    )
    chunks_completed: int = Field(ge=0, description="Chunks processed so far.")
    chunks_total: int = Field(ge=0, description="Total chunks to process.")
    elapsed_seconds: float = Field(ge=0, description="Elapsed processing time in seconds.")
    audio_processed_seconds: float = Field(ge=0, description="Audio processed so far in seconds.")
    audio_total_seconds: float = Field(ge=0, description="Total audio duration in seconds.")


class RuntimeMetricsEvent(BaseModel):
    """Model/GPU/processing telemetry sampled ~1 Hz while a job runs."""

    type: Literal["runtime_metrics"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    model: ModelInfo = Field(description="Model in use.")
    gpu: GpuMetrics | None = Field(description="GPU statistics; null when running on CPU.")
    processing: ProcessingMetrics = Field(description="Processing statistics.")


class JobCompletedEvent(BaseModel):
    """The job finished successfully; carries the full separation result."""

    type: Literal["job_completed"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    result: SeparationResult = Field(description="The separation result.")


class JobCancelledEvent(BaseModel):
    """The job was cancelled by the user."""

    type: Literal["job_cancelled"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    stage_at_cancellation: JobState = Field(description="Stage the job was in when cancelled.")


class JobFailedEvent(BaseModel):
    """The job failed; carries the standard error information."""

    type: Literal["job_failed"] = Field(description="Event discriminator.")
    job_id: str = Field(description="ULID of the job.")
    error: ErrorInfo = Field(description="Failure information.")


WebSocketEvent = Annotated[
    JobCreatedEvent
    | JobStartedEvent
    | JobStageChangedEvent
    | JobProgressEvent
    | RuntimeMetricsEvent
    | JobCompletedEvent
    | JobCancelledEvent
    | JobFailedEvent,
    Field(discriminator="type"),
]
"""Discriminated union over every WebSocket event, keyed on ``type``."""
