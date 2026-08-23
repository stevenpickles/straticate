"""On-disk layout of a job's separation outputs.

One place defines where a job's artifacts live, so the separator that writes
them (feature 014), the result API that serves them (feature 021) and the
export path (feature 022) cannot drift apart::

    {data_dir}/jobs/{job_id}/stems/{stem}.wav

``data_dir`` is :attr:`straticate.config.Settings.data_dir` — the same root
:class:`straticate.audio.AudioStore` writes uploads under
(``{data_dir}/audio/{audio_id}/original{ext}``), so uploads and job outputs sit
side by side and a single directory holds everything the application produced.

Directories are created lazily by whoever writes into them. Nothing here
persists across restarts: like the audio registry, job records are in-memory
only, so files left behind by a previous run are orphaned (a known limitation
until a persistent registry exists).
"""

from __future__ import annotations

from pathlib import Path

from straticate.inference.base import STEM_NAME_PATTERN

JOBS_DIRECTORY = "jobs"
"""Name of the per-job artifact root under ``data_dir``."""

STEMS_DIRECTORY = "stems"
"""Name of the stem directory under a job's output directory."""

STEM_SUFFIX = ".wav"
"""File suffix of a written stem (16-bit PCM WAV; export formats are 022)."""


def job_output_dir(data_dir: Path, job_id: str) -> Path:
    """Return ``{data_dir}/jobs/{job_id}`` — everything a job produced."""
    return data_dir / JOBS_DIRECTORY / job_id


def job_stems_dir(data_dir: Path, job_id: str) -> Path:
    """Return ``{data_dir}/jobs/{job_id}/stems`` — the separator's output directory."""
    return job_output_dir(data_dir, job_id) / STEMS_DIRECTORY


def stem_path(data_dir: Path, job_id: str, stem: str) -> Path:
    """Return the file path of one stem of one job.

    Args:
        data_dir: Application data directory.
        job_id: ULID of the job.
        stem: Stem name (must match :data:`~straticate.inference.base.STEM_NAME_PATTERN`).

    Raises:
        ValueError: ``stem`` is not a valid stem name — it becomes a file
            name, so anything else is rejected rather than sanitized.
    """
    if not STEM_NAME_PATTERN.fullmatch(stem):
        raise ValueError(f"invalid stem name {stem!r}")
    return job_stems_dir(data_dir, job_id) / f"{stem}{STEM_SUFFIX}"


__all__ = [
    "JOBS_DIRECTORY",
    "STEMS_DIRECTORY",
    "STEM_SUFFIX",
    "job_output_dir",
    "job_stems_dir",
    "stem_path",
]
