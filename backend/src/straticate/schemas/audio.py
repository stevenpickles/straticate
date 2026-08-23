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


class AudioFile(BaseModel):
    """An uploaded audio file registered with the backend."""

    id: str = Field(description="ULID identifying the uploaded audio.")
    filename: str = Field(description="Original filename as provided by the client.")
    size_bytes: int = Field(ge=0, description="Size of the uploaded file in bytes.")
    uploaded_at: AwareDatetime = Field(description="Upload timestamp (timezone-aware, ISO-8601).")
    metadata: AudioMetadata = Field(description="Probed technical metadata.")
