"""Removing one job: its manager entry, its record, and its directory.

Feature 058 established the ordering that makes a job deletion safe, inside
``DELETE /jobs/{job_id}``'s handler. Feature 060's ``POST /system/prune``
removes finished jobs in bulk and must not re-derive it: two implementations
of "delete a job" is exactly the shape 058 spent its own design removing for
export paths (``jobs/layout.py``), and the ordering here is subtle enough
that a second copy would eventually get it wrong in one of the two places.
So the route body moved here verbatim, and both callers use it.

The ordering, and why each step is where it is:

1. **Pop the manager entry first** (:meth:`~straticate.jobs.JobManager.remove`).
   It refuses a non-terminal job before anything on disk is touched, so a
   refused removal leaves *nothing* changed — not the entry, not a byte.
2. **Unlink the record next, synchronously, and abort if that fails.**
   :mod:`straticate.jobs.store`'s invariant is that a job directory is
   self-describing: never a record naming stems that are gone, never stems no
   record mentions. A bare ``rmtree(..., ignore_errors=True)`` can violate the
   second half by removing every stem and failing silently on a locked
   ``job.json`` — a ``completed`` record whose stems the next boot serves as
   404s. Making the record's removal the one unconditional step is what closes
   that: if it fails, the popped (necessarily terminal, hence restorable)
   entry is re-seeded and the ``OSError`` propagates, leaving the job exactly
   as it was.
3. **Remove the rest in a worker thread, best-effort.** A 1.17 GB job
   directory measured a 175 ms event-loop stall run inline. Offloading is safe
   *because* step 1 already popped the entry synchronously: no other request
   can submit, cancel or re-delete this job id while the thread runs.

The split into :func:`detach_job` and :func:`remove_job` is what lets a bulk
caller keep step 1 and 2 synchronous across *many* jobs before any ``await``
— see :func:`detach_job`.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from straticate.jobs.layout import job_output_dir, job_record_path
from straticate.jobs.manager import JobManager
from straticate.schemas import Job


def detach_job(manager: JobManager, data_dir: Path, job_id: str) -> Job:
    """Pop a terminal job's entry and unlink its record. **Synchronous, no awaits.**

    Steps 1 and 2 of the module docstring. After this returns, the job is
    gone from every endpoint's point of view — the entry is popped and the
    record a restart would read is unlinked — while its stems and exports are
    still on disk, waiting for :func:`remove_job_directory`.

    Containing no ``await`` is the point, not an implementation detail. It is
    what lets ``POST /system/prune`` detach a whole batch of jobs before the
    event loop can run anything else, so no concurrent request ever observes
    the batch half-detached, and it is what makes the offloaded removal that
    follows safe for every job in it at once.

    Args:
        manager: The application's job manager (call on its event loop).
        data_dir: :attr:`straticate.config.Settings.data_dir`.
        job_id: The job to detach.

    Returns:
        A snapshot of the job as it was, for the caller to report on.

    Raises:
        ApplicationError: ``job_not_found`` (404) for an unknown id;
            ``job_active`` (409) for a job that has not reached a terminal
            state. Nothing has been touched in either case.
        OSError: The record could not be unlinked. The entry is re-seeded via
            :meth:`~straticate.jobs.JobManager.restore` before this
            propagates, so the job is left served, listed and whole rather
            than half-deleted.
    """
    job = manager.remove(job_id)
    try:
        job_record_path(data_dir, job.id).unlink(missing_ok=True)
    except OSError:
        # The entry is already popped, and it is terminal (remove() only ever
        # succeeds for a terminal job), so restore() re-seeds it cleanly. The
        # caller reports the honest failure instead of a job whose record is
        # gone but whose entry, stems and exports are not.
        manager.restore([job])
        raise
    return job


def remove_job_directory(data_dir: Path, job_id: str) -> None:
    """Best-effort removal of everything left in a detached job's directory.

    Step 3 of the module docstring, and **blocking** — callers on the event
    loop must offload it (:func:`remove_job` does).

    ``ignore_errors=True`` for the same reason feature 058 chose it: a stem or
    export this job's own download route is still streaming through a
    :class:`~fastapi.responses.FileResponse` is an open file, and Windows
    refuses to unlink an open file. Failing a deletion because one file among
    a dozen is mid-download would make deletion undependable exactly when a
    user is most likely to attempt it. The job is already authoritatively gone
    (:func:`detach_job` removed the record); whatever a handle kept alive is
    debris, which ``POST /system/prune``'s ``orphans`` class is now the answer
    to rather than a permanent leak.
    """
    shutil.rmtree(job_output_dir(data_dir, job_id), ignore_errors=True)


async def remove_job(manager: JobManager, data_dir: Path, job_id: str) -> Job:
    """Remove one job wholesale: entry, record, stems and exports.

    The three steps of the module docstring in order, with the last one
    offloaded to a worker thread. This is ``DELETE /jobs/{job_id}``'s entire
    body (feature 058); ``POST /system/prune`` uses :func:`detach_job` and
    :func:`remove_job_directory` separately so it can batch step 1 and 2.

    A restarted server never resurrects the job: the record died first,
    synchronously, and startup only lists what :mod:`straticate.jobs.store`
    finds on disk.

    Raises:
        ApplicationError: ``job_not_found`` (404), ``job_active`` (409).
        OSError: The record could not be unlinked; the job is left intact.
    """
    job = detach_job(manager, data_dir, job_id)
    await asyncio.to_thread(remove_job_directory, data_dir, job.id)
    return job


__all__ = ["detach_job", "remove_job", "remove_job_directory"]
