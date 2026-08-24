"""ffprobe-based technical metadata extraction.

Metadata always comes from probing the actual media bytes — the filename
extension is never consulted. Anything ffprobe cannot decode, or that
contains no audio stream, raises :class:`AudioProbeError`.

A probe that never *finishes* is a different event and raises
:class:`~straticate.audio.ffmpeg.FFmpegTimeout` instead — see
:mod:`straticate.audio.ffmpeg`. Folding it into :class:`AudioProbeError` would
tell the user their file is not decodable, which is a claim ffprobe never made.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from straticate.audio.ffmpeg import run_ffmpeg
from straticate.schemas import AudioMetadata


class AudioProbeError(Exception):
    """The file could not be decoded as audio by ffprobe."""


async def probe_audio(path: Path, *, timeout_seconds: float) -> AudioMetadata:
    """Probe ``path`` with ffprobe and return its technical metadata.

    Runs the ffprobe subprocess in a worker thread so the event loop is
    never blocked.

    Args:
        path: Media file to probe.
        timeout_seconds: Bound for the ffprobe invocation, from the caller's
            ``Settings.ffmpeg_timeout_seconds``. Required, so no caller can
            silently probe without a bound (see :mod:`straticate.audio.ffmpeg`).

    Raises:
        AudioProbeError: The file is not decodable audio (ffprobe failed,
            produced no audio stream, or reported unusable fields).
        FFmpegTimeout: ffprobe exceeded ``timeout_seconds``.
    """
    return await asyncio.to_thread(_probe_sync, path, timeout_seconds)


def _probe_sync(path: Path, timeout_seconds: float) -> AudioMetadata:
    """Blocking implementation of :func:`probe_audio`."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run_ffmpeg(command, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise AudioProbeError(f"ffprobe could not decode the file: {message}")

    try:
        report: dict[str, Any] = json.loads(result.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise AudioProbeError("ffprobe produced unparseable output.") from exc

    return _metadata_from_report(report)


def _metadata_from_report(report: dict[str, Any]) -> AudioMetadata:
    """Map an ffprobe JSON report onto :class:`AudioMetadata`."""
    fmt: dict[str, Any] = report.get("format") or {}
    streams: list[dict[str, Any]] = report.get("streams") or []
    stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise AudioProbeError("The file contains no audio stream.")

    duration = _as_float(fmt.get("duration"))
    if duration is None:
        duration = _as_float(stream.get("duration"))
    format_name = str(fmt.get("format_name", ""))
    container = format_name.split(",")[0].strip()
    codec = str(stream.get("codec_name", ""))
    channels = _as_int(stream.get("channels"))
    sample_rate_hz = _as_int(stream.get("sample_rate"))

    if duration is None or not container or not codec or not channels or not sample_rate_hz:
        raise AudioProbeError("ffprobe did not report usable audio metadata.")

    bit_depth = _as_int(stream.get("bits_per_raw_sample")) or _as_int(stream.get("bits_per_sample"))
    bit_rate_bps = _as_int(stream.get("bit_rate")) or _as_int(fmt.get("bit_rate"))

    return AudioMetadata(
        duration_seconds=duration,
        container=container,
        codec=codec,
        channels=channels,
        sample_rate_hz=sample_rate_hz,
        bit_depth=bit_depth or None,
        bit_rate_bps=bit_rate_bps or None,
    )


def _as_int(value: Any) -> int | None:
    """Coerce an ffprobe field (int or numeric string) to ``int``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce an ffprobe field (number or numeric string) to ``float``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
