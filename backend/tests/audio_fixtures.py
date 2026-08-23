"""Tiny generated WAV fixtures for the inference tests.

Audio binaries are never committed: every fixture is synthesised at test time
with the standard library :mod:`wave` module.
"""

import math
import wave
from array import array
from pathlib import Path

INT16_SCALE = 32767


def write_tone_wav(
    path: Path,
    *,
    seconds: float = 0.5,
    channels: int = 2,
    sample_rate: int = 44100,
    frequency: float = 440.0,
    amplitude: float = 0.5,
) -> Path:
    """Write a deterministic multi-channel sine sweep-free tone to ``path``.

    Each channel gets a different frequency multiple so channel handling
    (deinterleave/interleave) is actually exercised.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    peak = int(amplitude * INT16_SCALE)
    samples: array[int] = array("h")
    for index in range(frames):
        for channel in range(channels):
            angle = 2.0 * math.pi * frequency * (channel + 1) * index / sample_rate
            samples.append(int(peak * math.sin(angle)))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.tobytes())
    return path


def read_wav(path: Path) -> tuple[int, int, int, array[int]]:
    """Return ``(channels, sample_rate, frame_count, interleaved_samples)``."""
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_rate = reader.getframerate()
        width = reader.getsampwidth()
        frames = reader.getnframes()
        raw = reader.readframes(frames)
    assert width == 2, "fixtures and stems are 16-bit PCM"
    samples: array[int] = array("h")
    samples.frombytes(raw)
    return channels, sample_rate, frames, samples


def peak_amplitude(samples: array[int]) -> int:
    """Peak absolute sample value (``0`` for digital silence)."""
    if not samples:
        return 0
    return max(abs(min(samples)), abs(max(samples)))
