"""Pydantic schemas — the shared API contract.

These models (plus the generated OpenAPI document) are the single source of
truth for the backend/frontend boundary. Frontend TypeScript types are
generated from the exported OpenAPI document; nothing here is ever duplicated
by hand on the other side. See docs/contracts/rest-api.md and
docs/contracts/websocket-events.md.
"""

from straticate.schemas.audio import AudioFile, AudioMetadata
from straticate.schemas.common import ErrorEnvelope, ErrorInfo, HealthStatus, VersionInfo
from straticate.schemas.devices import ComputeDevice
from straticate.schemas.events import (
    GpuMetrics,
    JobCancelledEvent,
    JobCompletedEvent,
    JobCreatedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
    JobStartedEvent,
    ModelInfo,
    ProcessingMetrics,
    RuntimeMetricsEvent,
    WebSocketEvent,
)
from straticate.schemas.jobs import (
    Job,
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)
from straticate.schemas.models import (
    Model,
    ModelRequirements,
    QualityOption,
    QualityTier,
    SeparationMode,
)

__all__ = [
    "AudioFile",
    "AudioMetadata",
    "ComputeDevice",
    "ErrorEnvelope",
    "ErrorInfo",
    "GpuMetrics",
    "HealthStatus",
    "Job",
    "JobCancelledEvent",
    "JobCompletedEvent",
    "JobCreatedEvent",
    "JobFailedEvent",
    "JobProgressEvent",
    "JobStageChangedEvent",
    "JobStartedEvent",
    "JobState",
    "Model",
    "ModelInfo",
    "ModelRequirements",
    "ProcessingMetrics",
    "QualityOption",
    "QualityTier",
    "RuntimeMetricsEvent",
    "SeparationConfiguration",
    "SeparationMode",
    "SeparationResult",
    "SeparationResultMetrics",
    "Stem",
    "VersionInfo",
    "WebSocketEvent",
]
