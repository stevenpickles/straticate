"""Where a job's directory and its durable record live on disk.

One place defines the job directory, so everything that touches it agrees::

    {data_dir}/jobs/{job_id}/job.json          the job record (feature 057)
    {data_dir}/jobs/{job_id}/stems/{stem}.wav  the separated stems (014/021)
    {data_dir}/jobs/{job_id}/exports/…         built export artifacts (022)

``data_dir`` is :attr:`straticate.config.Settings.data_dir` — the same root
:class:`straticate.audio.AudioStore` writes uploads under
(``{data_dir}/audio/{audio_id}/original{ext}``), so uploads and job outputs sit
side by side and a single directory holds everything the application produced.

**This module deliberately imports nothing but** :mod:`pathlib`. The job
directory is a fact both halves of the application need: the job store
(:mod:`straticate.jobs.store`) writes the record, and the separation engine
(:mod:`straticate.inference.layout`, which re-exports
:func:`job_output_dir`) writes the stems inside it. ``inference`` already
imports ``jobs``, so putting the shared fact here — rather than importing
``inference`` from the store — is what keeps the dependency running one way.

Directories are created lazily by whoever writes into them.
"""

from __future__ import annotations

from pathlib import Path

JOBS_DIRECTORY = "jobs"
"""Name of the per-job artifact root under ``data_dir``."""

JOB_RECORD_FILENAME = "job.json"
"""Name of a job's durable record inside its own directory (feature 057)."""

EXPORTS_DIRECTORY = "exports"
"""Name of the export-artifact directory inside a job's own directory (feature 022).

Defined here rather than in :mod:`straticate.api.export` (which built every
export path itself before feature 058) so that a job's directory tree has
exactly one author. That is what makes the feature 058 guarantee possible: a
whole-job delete that removes ``{data_dir}/jobs/{job_id}`` in one ``rmtree``
takes the exports with it by construction — there is no second path-building
function anywhere that could still be pointing at a survivor.
"""


def jobs_root(data_dir: Path) -> Path:
    """Return ``{data_dir}/jobs`` — the directory holding every job's outputs."""
    return data_dir / JOBS_DIRECTORY


def job_output_dir(data_dir: Path, job_id: str) -> Path:
    """Return ``{data_dir}/jobs/{job_id}`` — everything one job produced."""
    return jobs_root(data_dir) / job_id


def job_record_path(data_dir: Path, job_id: str) -> Path:
    """Return ``{data_dir}/jobs/{job_id}/job.json`` — the job's durable record.

    The record sits *beside* the artifacts it describes rather than in a
    registry file of its own, which is what makes a job directory
    self-describing: one directory holds the record, the stems and the exports,
    so nothing can be half-deleted into a record that points at stems that are
    gone, or stems no record mentions. See :mod:`straticate.jobs.store`.
    """
    return job_output_dir(data_dir, job_id) / JOB_RECORD_FILENAME


def job_exports_dir(data_dir: Path, job_id: str) -> Path:
    """Return ``{data_dir}/jobs/{job_id}/exports`` — this job's built export artifacts.

    The sole authority on where a job's exports live (feature 058); see
    :data:`EXPORTS_DIRECTORY`. :mod:`straticate.api.export` reads and writes
    through this function rather than building the path itself, and
    ``DELETE /jobs/{job_id}`` never has to know exports exist at all — it
    removes the job's whole directory, this one included.
    """
    return job_output_dir(data_dir, job_id) / EXPORTS_DIRECTORY


__all__ = [
    "EXPORTS_DIRECTORY",
    "JOBS_DIRECTORY",
    "JOB_RECORD_FILENAME",
    "job_exports_dir",
    "job_output_dir",
    "job_record_path",
    "jobs_root",
]
