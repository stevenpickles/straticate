"""Audio ingestion: upload storage, the FFmpeg runner, ffprobe metadata, analysis."""

from straticate.audio.analysis import (
    WIDE_STEREO_THRESHOLD,
    AudioAnalysisError,
    StereoAnalysisCache,
    analyse_stereo,
)
from straticate.audio.ffmpeg import FFmpegTimeout, run_ffmpeg
from straticate.audio.probe import AudioProbeError, probe_audio
from straticate.audio.storage import AudioStore

__all__ = [
    "WIDE_STEREO_THRESHOLD",
    "AudioAnalysisError",
    "AudioProbeError",
    "AudioStore",
    "FFmpegTimeout",
    "StereoAnalysisCache",
    "analyse_stereo",
    "probe_audio",
    "run_ffmpeg",
]
