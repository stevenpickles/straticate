"""Separation jobs: state machine, configuration, and results.

Every separation is an asynchronous job (see ARCHITECTURE.md §6). The
initiating HTTP request returns immediately with the created ``Job``; progress
flows over the WebSocket, and ``GET /jobs/{job_id}`` remains the source of
truth for reconnect/refresh.
"""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, Field

from straticate.schemas.common import ErrorInfo
from straticate.schemas.stems import StemName


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


class StereoHandling(StrEnum):
    """What the separator does with the input's stereo image before separating.

    A statement about the **user's audio**, not about the model. Every
    separation model in use is trained on centred, strongly correlated stereo,
    and a mix whose channels are near-independent is outside that distribution:
    feature 028 measured a 1968 stereo mix (full-band L/R correlation +0.23,
    low end hard-panned 5.8 dB left) whose ``bass`` stem came out 33 dB quieter
    than the same track folded to mono. This is the control that lets the
    person who owns the recording say which it is.

    ``AS_IS`` is the default and is **bit-for-bit** the previous behaviour: the
    decoded mixture is handed to the separator untouched. Nothing is ever
    applied without being asked for (feature 032's rule, and this feature's
    brief) — a wide mix is not detected and silently corrected.

    Feature 062 added the third value. 041 shipped the whole-spectrum fold and
    recorded the band-limited one as "the most promising unexplored option";
    062 measured it on the same single track against the same baselines,
    where it recovered at least as much ``bass`` as the full fold at a third
    of the cost to ``drums`` and ``other``, with the stems coming back stereo
    instead of mono. One track, not a survey — the table and its caveats are
    in ``docs/features/062-band-limited-fold.md``.
    """

    AS_IS = "as_is"
    """Separate the mixture exactly as it was decoded. The default."""

    MONO = "mono"
    """Fold the mixture to mono ``(L + R) / 2`` first; the stems come back mono."""

    MONO_BASS = "mono_bass"
    """Fold the low end to a shared centre; the image above it, and the stems, stay stereo.

    The crossover is a measured constant of the application
    (:data:`~straticate.inference.stereo.BASS_FOLD_CROSSOVER_HZ`), not a dial:
    see that docstring for why it is fixed and how the value was chosen.
    """


class SeparationConfiguration(BaseModel):
    """User-facing configuration of a separation job (the create-job request)."""

    audio_id: str = Field(description="ULID of the uploaded audio to separate.")
    mode_id: str = Field(description='Separation mode ID, e.g. "vocals".')
    quality_id: str = Field(description='Quality tier ID, e.g. "high_quality".')
    device_id: str | None = Field(
        default=None,
        description="Compute device to use; null lets the backend pick the best device.",
    )
    stereo_handling: StereoHandling = Field(
        default=StereoHandling.AS_IS,
        description=(
            "How to treat the input's stereo image before separating. "
            '"as_is" (the default) separates the mixture untouched; "mono" folds it '
            "down to a single channel first, which recovers stems a very wide stereo "
            'image can otherwise lose, at the cost of mono stems; "mono_bass" folds '
            "only the low end to a shared centre and keeps the image above it, so the "
            "stems stay stereo."
        ),
    )


class Stem(BaseModel):
    """One separated output stem of a completed job.

    ``name`` is constrained to :data:`~straticate.schemas.stems.STEM_NAME_REGEX`
    because it is not merely a label: it is the path segment
    ``GET /jobs/{job_id}/stems/{stem_name}`` accepts and the file name on disk.
    Unconstrained, a result could advertise a stem the stem route would then
    refuse — a 404 whose ``detail`` listed the very name it denied.
    """

    name: StemName = Field(description='Stem name, e.g. "vocals", "instrumental".')
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
