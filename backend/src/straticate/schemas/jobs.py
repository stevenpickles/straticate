"""Separation jobs: state machine, configuration, and results.

Every separation is an asynchronous job (see ARCHITECTURE.md §6). The
initiating HTTP request returns immediately with the created ``Job``; progress
flows over the WebSocket, and ``GET /jobs/{job_id}`` remains the source of
truth for reconnect/refresh.
"""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, Field

from straticate.schemas.common import ErrorInfo


class JobState(StrEnum):
    """States of the job state machine.

    Processing order::

        queued → preparing → decoding → loading_model → separating
               → post_processing → encoding → completed

    Any non-terminal state may transition to ``cancelled`` (user cancellation)
    or ``failed`` (error).
    """

    QUEUED = "queued"
    PREPARING = "preparing"
    DECODING = "decoding"
    LOADING_MODEL = "loading_model"
    SEPARATING = "separating"
    POST_PROCESSING = "post_processing"
    ENCODING = "encoding"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether this state is terminal (no further transitions allowed)."""
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED})


class SeparationConfiguration(BaseModel):
    """User-facing configuration of a separation job (the create-job request)."""

    audio_id: str = Field(description="ULID of the uploaded audio to separate.")
    mode_id: str = Field(description='Separation mode ID, e.g. "vocals".')
    quality_id: str = Field(description='Quality tier ID, e.g. "high_quality".')
    device_id: str | None = Field(
        default=None,
        description="Compute device to use; null lets the backend pick the best device.",
    )


class Stem(BaseModel):
    """One separated output stem of a completed job."""

    name: str = Field(description='Stem name, e.g. "vocals", "instrumental".')
    duration_seconds: float = Field(ge=0, description="Stem duration in seconds.")
    sample_rate_hz: int = Field(gt=0, description="Sample rate in Hz.")
    channels: int = Field(ge=1, description="Number of audio channels.")


class SeparationResultMetrics(BaseModel):
    """Performance metrics of a completed separation."""

    processing_seconds: float = Field(ge=0, description="Wall-clock processing time in seconds.")
    realtime_factor: float = Field(ge=0, description="RTF = audio duration / processing duration.")


class SeparationResult(BaseModel):
    """Outputs and metrics of a completed separation job."""

    job_id: str = Field(description="ULID of the job that produced this result.")
    model_id: str = Field(description="ID of the model that performed the separation.")
    stems: list[Stem] = Field(description="Separated output stems.")
    metrics: SeparationResultMetrics = Field(description="Performance metrics.")


class Job(BaseModel):
    """A separation job record.

    ``result`` is populated on ``completed``; ``error`` on ``failed``.
    ``started_at``/``finished_at`` are null until the job reaches the
    corresponding point of its lifecycle.
    """

    id: str = Field(description="ULID identifying the job.")
    audio_id: str = Field(description="ULID of the input audio.")
    configuration: SeparationConfiguration = Field(description="Requested configuration.")
    model_id: str = Field(description="ID of the model selected for this job.")
    state: JobState = Field(description="Current state in the job state machine.")
    progress: float = Field(
        ge=0.0,
        le=1.0,
        description="Overall progress in [0, 1] (real work: completed_chunks / total_chunks).",
    )
    created_at: AwareDatetime = Field(description="Creation timestamp (timezone-aware).")
    started_at: AwareDatetime | None = Field(description="Processing start; null while queued.")
    finished_at: AwareDatetime | None = Field(description="Terminal timestamp; null until then.")
    error: ErrorInfo | None = Field(description="Failure information; null unless failed.")
    result: SeparationResult | None = Field(description="Separation result; null until completed.")
