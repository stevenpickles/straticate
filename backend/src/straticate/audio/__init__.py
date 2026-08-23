"""Audio ingestion: upload storage and ffprobe-based metadata extraction."""

from straticate.audio.probe import AudioProbeError, probe_audio
from straticate.audio.storage import AudioStore

__all__ = [
    "AudioProbeError",
    "AudioStore",
    "probe_audio",
]
