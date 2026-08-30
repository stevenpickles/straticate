"""Uploaded audio files and their probed technical metadata."""

from pydantic import AwareDatetime, BaseModel, Field


class AudioMetadata(BaseModel):
    """Technical metadata probed from the actual media (never from the filename).

    ``bit_depth`` and ``bit_rate_bps`` are null when the container/codec does
    not define them (e.g. lossy formats without a fixed bit depth).
    """

    duration_seconds: float = Field(ge=0, description="Duration of the audio in seconds.")
    container: str = Field(description='Container format reported by ffprobe (e.g. "flac").')
    codec: str = Field(description='Audio codec reported by ffprobe (e.g. "flac", "mp3").')
    channels: int = Field(ge=1, description="Number of audio channels.")
    sample_rate_hz: int = Field(gt=0, description="Sample rate in Hz.")
    bit_depth: int | None = Field(description="Bits per sample; null for lossy formats.")
    bit_rate_bps: int | None = Field(description="Bit rate in bits per second; null when unknown.")


class StereoAnalysis(BaseModel):
    """What measuring an upload's stereo image found (feature 063).

    A **measurement, not a decision**. It says what the recording is like; it
    never says what to do about it, and nothing in the separation path reads it.
    Feature 041's rule for any detection built on this signal is that it may
    *suggest* and must never apply, and the contract keeps that true by having no
    field that could be applied: there is no `stereo_handling` here, and a job
    runs identically whether or not this was ever requested.

    ``wide_stereo`` is derived **server-side** from ``l_r_correlation`` and the
    threshold, so no client has to know the number to agree with the server about
    what the server measured. The two are therefore not independent, with two
    documented exceptions where the correlation does not exist:

    - a **single-channel** upload has no image to measure:
      ``{null, wide_stereo: false}``;
    - a channel with **zero variance** — one side silent, or a constant — has no
      defined correlation but is the extreme of the very failure mode this
      detects: ``{null, wide_stereo: true}``.
    """

    l_r_correlation: float | None = Field(
        ge=-1,
        le=1,
        description=(
            "Pearson correlation of the left and right channels, full band, over "
            "the whole track. Null when there is none to report: a mono source, "
            "a channel with zero variance, or audio too short to correlate."
        ),
    )
    wide_stereo: bool = Field(
        description=(
            "Whether the channels are independent enough that a stem may come "
            "back near-silent. Derived from the correlation server-side; the "
            "threshold is not part of the contract."
        )
    )


class AudioFile(BaseModel):
    """An uploaded audio file registered with the backend."""

    id: str = Field(description="ULID identifying the uploaded audio.")
    filename: str = Field(description="Original filename as provided by the client.")
    size_bytes: int = Field(ge=0, description="Size of the uploaded file in bytes.")
    uploaded_at: AwareDatetime = Field(description="Upload timestamp (timezone-aware, ISO-8601).")
    metadata: AudioMetadata = Field(description="Probed technical metadata.")
