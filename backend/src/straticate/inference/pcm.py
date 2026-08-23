"""Planar 16-bit PCM buffers plus FFmpeg decoding and WAV encoding.

This is the separation engine's audio plumbing: FFmpeg is the single
compatibility layer (ARCHITECTURE.md §5), so a separator never parses a
container itself. Audio is carried as **planar** signed 16-bit samples — one
:class:`array.array` per channel — because per-channel processing (filters,
fades) is what separators actually do, and Python slicing on ``array`` is a
C-speed operation.

16-bit is deliberate: it is exactly what :mod:`wave` reads and writes, so the
placeholder stems of :mod:`straticate.inference.fake` are ordinary WAV files
any player (and any test) can open with the standard library. Feature 022 owns
higher-precision export formats.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from straticate.audio.probe import AudioProbeError, probe_audio

SAMPLE_WIDTH_BYTES = 2
"""Bytes per sample — the whole module is signed 16-bit PCM."""

INT16_MIN = -32768
INT16_MAX = 32767

MAX_OUTPUT_CHANNELS = 2
"""Channel ceiling for decoded audio (anything wider is downmixed to stereo)."""


class AudioDecodeError(Exception):
    """The input could not be decoded to PCM by FFmpeg."""


@dataclass(frozen=True, slots=True)
class PcmAudio:
    """An immutable handle on planar signed-16-bit PCM audio.

    Attributes:
        sample_rate: Sample rate in Hz.
        channels: One ``array("h")`` of samples per channel, all the same
            length. The arrays are in machine byte order (conversion to
            little-endian WAV bytes happens in :func:`write_wav`).
    """

    sample_rate: int
    channels: tuple[array[int], ...]

    @property
    def channel_count(self) -> int:
        """Number of channels."""
        return len(self.channels)

    @property
    def frame_count(self) -> int:
        """Number of sample frames (shortest channel wins; all are equal)."""
        return min((len(plane) for plane in self.channels), default=0)

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds, derived from the actual sample count."""
        return self.frame_count / self.sample_rate


async def decode_to_pcm(
    path: Path,
    *,
    sample_rate: int,
    max_channels: int = MAX_OUTPUT_CHANNELS,
) -> PcmAudio:
    """Decode ``path`` to planar 16-bit PCM at ``sample_rate``.

    The source is probed first (ffprobe, never the filename) to learn its
    channel layout; the decode then resamples to ``sample_rate`` — the model's
    native rate — and keeps at most ``max_channels`` channels (wider layouts
    are downmixed by FFmpeg). Both subprocesses run in worker threads so the
    event loop is never blocked.

    Args:
        path: Media file to decode.
        sample_rate: Target sample rate in Hz (the separator's native rate).
        max_channels: Channel ceiling; the result has
            ``min(source_channels, max_channels)`` channels.

    Returns:
        The decoded audio.

    Raises:
        AudioDecodeError: The file is not decodable audio, or decoded to no
            samples at all.
    """
    try:
        metadata = await probe_audio(path)
    except AudioProbeError as exc:
        raise AudioDecodeError(str(exc)) from exc

    channels = min(max(metadata.channels, 1), max(max_channels, 1))
    raw = await asyncio.to_thread(_decode_sync, path, sample_rate, channels)
    return _planar_from_interleaved(raw, sample_rate=sample_rate, channels=channels)


def write_wav(path: Path, audio: PcmAudio) -> None:
    """Write ``audio`` to ``path`` as a 16-bit PCM WAV file.

    Parent directories are created as needed. The file is written in full
    before this returns; callers that must never expose a truncated file
    should write to a temporary name and rename.

    Args:
        path: Destination file.
        audio: Audio to encode (must have at least one channel).

    Raises:
        ValueError: ``audio`` has no channels.
    """
    if audio.channel_count == 0:
        raise ValueError("cannot write a WAV file with zero channels")
    path.parent.mkdir(parents=True, exist_ok=True)
    interleaved = interleave(audio)
    if sys.byteorder != "little":  # pragma: no cover - little-endian CI/dev hosts
        interleaved.byteswap()
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(audio.channel_count)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(audio.sample_rate)
        writer.writeframes(interleaved.tobytes())


def interleave(audio: PcmAudio) -> array[int]:
    """Return ``audio``'s planar channels as one interleaved sample array."""
    count = audio.channel_count
    frames = audio.frame_count
    if count == 1:
        return array("h", audio.channels[0][:frames])
    interleaved = array("h", bytes(SAMPLE_WIDTH_BYTES * frames * count))
    for index, plane in enumerate(audio.channels):
        interleaved[index::count] = plane[:frames]
    return interleaved


def _decode_sync(path: Path, sample_rate: int, channels: int) -> bytes:
    """Blocking FFmpeg decode to raw interleaved little-endian 16-bit PCM."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-",
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise AudioDecodeError(f"FFmpeg could not decode the file: {message}")
    return result.stdout


def _planar_from_interleaved(raw: bytes, *, sample_rate: int, channels: int) -> PcmAudio:
    """Split interleaved little-endian 16-bit PCM bytes into per-channel arrays."""
    frame_bytes = SAMPLE_WIDTH_BYTES * channels
    usable = len(raw) - (len(raw) % frame_bytes)
    if usable == 0:
        raise AudioDecodeError("The file decoded to no audio samples.")
    interleaved = array("h")
    interleaved.frombytes(raw[:usable])
    if sys.byteorder != "little":  # pragma: no cover - little-endian CI/dev hosts
        interleaved.byteswap()
    planes = tuple(interleaved[index::channels] for index in range(channels))
    return PcmAudio(sample_rate=sample_rate, channels=planes)
