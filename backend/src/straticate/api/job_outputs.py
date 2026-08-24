"""Shared lookups for the routes that serve a completed job's outputs.

Two routers read the same three things — the job, its
:class:`~straticate.schemas.SeparationResult`, and one stem's file on disk:
:mod:`straticate.api.results` (feature 021: the result record and stem
streaming) and :mod:`straticate.api.export` (feature 022: transcoded
downloads). Feature 021's documentation asked that the lookup be promoted here
rather than re-derived, so the error codes ``job_not_found``,
``result_not_available``, ``stem_not_found`` and ``stem_file_missing`` have
exactly one definition and the two routers cannot drift apart.

Nothing in this module lists a directory or names a stem: **the result is the
authority on which stems exist** (AGENTS.md principles 1 and 6). A requested
name is checked against ``result.stems`` before any path is built, so a
two-stem and a six-stem job take identical code paths and a traversal attempt
is simply not one of the job's stem names — a clean 404 long before a path
exists.
"""

from pathlib import Path

from straticate.errors import ApplicationError
from straticate.inference import stem_path
from straticate.jobs import JobManager
from straticate.schemas import Job, SeparationResult
from straticate.schemas.jobs import JobState


def result_not_available(job: Job) -> ApplicationError:
    """Build the 409 for a job that exists but has produced no result.

    One code covers every non-``completed`` state — still running, cancelled
    and failed alike — because the client's situation is the same in all of
    them: there is nothing to fetch. The distinguishing information is the
    job's ``state``, carried in ``detail`` so the client can explain itself
    ("still separating" vs. "you cancelled it") without a second error code to
    branch on. ``GET /jobs/{job_id}`` remains the place to read the full
    record, including a failed job's ``error``.
    """
    return ApplicationError(
        "result_not_available",
        f"Job {job.id!r} has no result: it is in state {job.state.value!r}.",
        status_code=409,
        detail={"job_id": job.id, "state": job.state.value},
    )


def stem_not_found(job_id: str, stem_name: str, available: list[str]) -> ApplicationError:
    """Build the 404 for a stem name the job's result does not list."""
    return ApplicationError(
        "stem_not_found",
        f"Job {job_id!r} has no stem named {stem_name!r}.",
        status_code=404,
        detail={"job_id": job_id, "stem": stem_name, "available_stems": available},
    )


def stem_file_missing(job_id: str, stem_name: str) -> ApplicationError:
    """Build the 404 for a stem the result claims but whose file is gone.

    Job records are in-memory while stems are on disk, so the two can drift:
    the directory can be removed underneath a live job. That is a missing
    resource, not a server fault — a 404 with its own code, never a 500.
    """
    return ApplicationError(
        "stem_file_missing",
        f"The audio file for stem {stem_name!r} of job {job_id!r} is no longer on disk.",
        status_code=404,
        detail={"job_id": job_id, "stem": stem_name},
    )


def completed_job(manager: JobManager, job_id: str) -> tuple[Job, SeparationResult]:
    """Return the job and its result, or raise the documented failure.

    Raises:
        ApplicationError: ``job_not_found`` (404) for an unknown job (raised
            by the manager), ``result_not_available`` (409) for a job that has
            not completed.
    """
    job = manager.get(job_id)
    result = job.result
    if job.state is not JobState.COMPLETED or result is None:
        raise result_not_available(job)
    return job, result


def stem_source_path(data_dir: Path, job_id: str, stem_name: str, available: list[str]) -> Path:
    """Resolve one stem of a completed job to an existing file on disk.

    Args:
        data_dir: Application data directory.
        job_id: The job's own id (the manager's key, never the raw URL string).
        stem_name: Requested stem name, straight from the request.
        available: Stem names the job's result lists — the only authority.

    Returns:
        The stem's file path, which exists.

    Raises:
        ApplicationError: ``stem_not_found`` (404) when ``available`` does not
            list the name, ``stem_file_missing`` (404) when it does but the
            file is gone.
    """
    if stem_name not in available:
        raise stem_not_found(job_id, stem_name, available)
    try:
        path = stem_path(data_dir, job_id, stem_name)
    except ValueError as exc:  # pragma: no cover - a result never lists one
        raise stem_not_found(job_id, stem_name, available) from exc
    if not path.is_file():
        raise stem_file_missing(job_id, stem_name)
    return path


__all__ = [
    "completed_job",
    "result_not_available",
    "stem_file_missing",
    "stem_not_found",
    "stem_source_path",
]
