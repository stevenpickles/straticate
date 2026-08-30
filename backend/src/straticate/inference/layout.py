"""On-disk layout of a job's separation outputs.

The stems half of the job directory :mod:`straticate.jobs.layout` defines, so
the separator that writes them (feature 014), the result API that serves them
(feature 021) and the export path (feature 022) cannot drift apart::

    {data_dir}/jobs/{job_id}/stems/{stem}.wav

The directory itself, and the ``job.json`` record that makes it legible after a
restart (feature 057), belong to :mod:`straticate.jobs.layout`, which this
module re-exports :data:`JOBS_DIRECTORY` and :func:`job_output_dir` from. They
live there rather than here because the job store needs them and ``jobs`` must
not import ``inference`` — the dependency already runs the other way.

Directories are created lazily by whoever writes into them. **Since feature 057
a job directory is self-describing**: its ``job.json`` is what lets the next
process serve the stems beside it rather than orphaning them. A job directory
with no record is still possible — the leftovers of a run older than that
feature, or of one whose record never landed — and is simply ignored at
startup.
"""

from __future__ import annotations

from pathlib import Path

from straticate.inference.base import STEM_NAME_PATTERN
from straticate.jobs.layout import JOBS_DIRECTORY, job_output_dir

STEMS_DIRECTORY = "stems"
"""Name of the stem directory under a job's output directory."""

STEM_SUFFIX = ".wav"
"""File suffix of a written stem (16-bit PCM WAV; export formats are 022)."""


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
