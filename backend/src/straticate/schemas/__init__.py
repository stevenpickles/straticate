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
    StereoHandling,
)
from straticate.schemas.maintenance import (
    DiskUsageReport,
    PruneClassReport,
    PruneFailure,
    PruneReport,
    PruneRequest,
    ReclaimClass,
    UsageBucket,
)
from straticate.schemas.models import (
    Model,
    ModelInstallation,
    ModelInstallState,
    ModelLicensing,
    ModelRequirements,
    QualityOption,
    QualityTier,
    SeparationMode,
)
from straticate.schemas.stems import STEM_NAME_PATTERN, STEM_NAME_REGEX, StemName
from straticate.schemas.storage import StorageReport

__all__ = [
    "STEM_NAME_PATTERN",
    "STEM_NAME_REGEX",
    "AudioFile",
    "AudioMetadata",
    "ComputeDevice",
    "DiskUsageReport",
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
    "ModelInstallState",
    "ModelInstallation",
    "ModelLicensing",
    "ModelRequirements",
    "ProcessingMetrics",
    "PruneClassReport",
    "PruneFailure",
    "PruneReport",
    "PruneRequest",
    "QualityOption",
    "QualityTier",
    "ReclaimClass",
    "RuntimeMetricsEvent",
    "SeparationConfiguration",
    "SeparationMode",
    "SeparationResult",
    "SeparationResultMetrics",
    "Stem",
    "StemName",
    "StereoHandling",
    "StorageReport",
    "UsageBucket",
    "VersionInfo",
    "WebSocketEvent",
]
