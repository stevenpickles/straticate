"""Durable job records: one JSON sidecar per job, and what a restart makes of them.

Until feature 057 the job manager was pure memory, so a completed separation
became unreachable the moment the server restarted: ``GET /jobs`` was empty,
``GET /jobs/{id}/result`` was a ``404``, and the stems on disk were orphans
nothing could name. This module is the fix, and it is deliberately the
smallest one that works::

    {data_dir}/jobs/{job_id}/job.json

**The file is the wire shape.** It is ``Job.model_dump_json()`` — the very
model the API serves — so there is no second schema to keep in step with the
first, no migration surface of its own, and a record read back is validated by
the same Pydantic model that produced it (AGENTS.md principle 2: contracts are
generated, not duplicated). It sits inside the job's own output directory, next
to the stems and exports it describes, so a job directory is self-describing
and cannot be half-deleted into a record whose stems are gone.

Four rules this module keeps:

- **Atomic publication.** A record is written to a same-directory
  ``job.json.{uuid}.tmp`` and published with :func:`os.replace`, so no reader —
  including the next process — can ever observe a half-written record. The
  temporary name carries a UUID rather than a fixed suffix so two writers for
  the same job could never share one scratch file, and the ``finally`` removes
  it on every path.
- **``fsync`` for completed records only.** ``os.replace`` is atomic for the
  directory entry, not for the file's data: after a power loss a "written"
  record can come back empty or torn. What that costs differs by state, so the
  cost paid differs by state. A lost ``completed`` record strands real work —
  stems that took GPU-minutes to produce, sitting in a directory nothing can
  name any more — and it is unrecoverable, because nothing re-derives a result
  from a stems directory. A lost ``failed`` or ``cancelled`` record costs
  nothing: the job is normalized to ``job_interrupted`` on the next boot, which
  is a *true* statement about a job whose ending was not durably recorded. So
  the completed record is flushed and ``fsync``-ed before the rename (and the
  containing directory synced after it, where the platform has a handle),
  exactly as :mod:`straticate.models.installer` does for weights; the other two
  take the ordinary buffered write. This is the same trade that module
  documents, decided per state instead of once.
- **Two writes per job, and progress is never one of them.** The record is
  written when the job is submitted (so a job that was answered ``201`` can
  never vanish) and again when it reaches a terminal state. Intermediate
  stages and progress are deliberately not persisted: any non-terminal record
  found at startup becomes ``job_interrupted`` regardless of *which*
  non-terminal state it names, so persisting ``separating`` — four times a
  second, per job — would buy exactly nothing and turn a progress report into
  a disk write.
- **A bad record is skipped, never fatal.** :meth:`JobStore.load_all` logs and
  moves on for anything it cannot read: a directory with no ``job.json`` (the
  orphan of a run older than this feature), unparseable JSON, a record that
  fails validation, a record whose ``id`` disagrees with the directory holding
  it. One unreadable file may not stop a server from starting, and a stray
  ``*.tmp`` is simply not a directory entry it looks at.

Recovery is :meth:`JobStore.recover`, and its one repair is the subject of
feature 057's other half: a job that was ``queued`` or running when the process
stopped comes back **failed** with ``error.code == "job_interrupted"``. It is
never re-queued — the executor closure that would run it (the resolved
separator, the decoded input path) cannot be reconstructed from a record, and
silently re-running heavy inference at startup would be the application acting
unasked. It is never ``cancelled`` either: nobody cancelled it.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from straticate.jobs.layout import JOB_RECORD_FILENAME, job_record_path, jobs_root
from straticate.schemas.common import ErrorInfo
from straticate.schemas.jobs import Job, JobState

logger = logging.getLogger(__name__)

JOB_INTERRUPTED_CODE = "job_interrupted"
"""``ErrorInfo.code`` of a job the server stopped underneath.

Not a new :class:`~straticate.schemas.jobs.JobState`: "the server went away" is
a *reason* a job failed, and ``failed`` plus an error code is how every other
reason is already expressed. Adding a state would have changed the shape of the
job contract — and every client's exhaustive switch over it — to say something
the existing shape says perfectly well.
"""

JOB_INTERRUPTED_MESSAGE = "The server stopped while this job was queued or running."
"""``ErrorInfo.message`` paired with :data:`JOB_INTERRUPTED_CODE`."""

TEMPORARY_SUFFIX = ".tmp"
"""Suffix of the scratch file a record is written to before it is published."""


class JobStore:
    """Reads and writes the durable ``job.json`` records under one ``data_dir``.

    Args:
        data_dir: Application data directory
            (:attr:`straticate.config.Settings.data_dir`); records live under
            its ``jobs/`` subtree and nothing outside it is ever touched.

    All methods are synchronous and are called from the job manager's event
    loop. That is a deliberate choice rather than an oversight: a record is a
    few hundred bytes, it is written twice per job, and the alternative —
    :func:`asyncio.to_thread` — would put the write in an uncancellable thread
    racing the manager's own shutdown for the same file. The one measurable
    cost is the ``fsync`` of a *completed* record, once per job, which is the
    trade the module docstring argues for.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    @property
    def data_dir(self) -> Path:
        """The data directory this store reads and writes records under."""
        return self._data_dir

    def save(self, job: Job) -> None:
        """Write ``job``'s record atomically, creating its directory if needed.

        A ``completed`` record is flushed to stable storage before it is
        published; see the module docstring for why the other states are not.

        Raises:
            OSError: The record could not be written. Callers that must not
                fail because of it (the job manager, which will not turn a
                finished separation into an error because a disk write failed)
                catch this themselves.
        """
        path = job_record_path(self._data_dir, job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid4().hex}{TEMPORARY_SUFFIX}")
        durable = job.state is JobState.COMPLETED
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as sink:
                sink.write(job.model_dump_json(indent=2))
                sink.flush()
                if durable:
                    os.fsync(sink.fileno())
            os.replace(temporary, path)
            if durable:
                _sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def load_all(self) -> list[Job]:
        """Return every readable record under ``{data_dir}/jobs``, in ULID order.

        Job IDs are ULIDs and a job's directory is named by its ID, so sorting
        the directory entries *is* submission order — the order ``GET /jobs``
        promises, restored without consulting a timestamp that a hand-edited
        record could disagree with.

        Unreadable records are logged and skipped rather than raised (see the
        module docstring): the caller is application startup.
        """
        root = jobs_root(self._data_dir)
        if not root.is_dir():
            return []
        records: list[Job] = []
        for directory in sorted(root.iterdir()):
            record = _load_one(directory)
            if record is not None:
                records.append(record)
        return records

    def recover(self) -> list[Job]:
        """Load every record, repairing the ones a stopped server left behind.

        Terminal records are returned verbatim. A record in any other state
        belonged to a job that was queued or running when the process stopped:
        it is rewritten as ``failed`` with :data:`JOB_INTERRUPTED_CODE`,
        persisted back so the repair is not re-derived on every boot, and
        returned in that form. Nothing is re-queued and nothing is deleted.

        A repair that cannot be written is logged and returned anyway — the
        API then tells the truth about the job for this run, and the next boot
        simply repairs it again.
        """
        recovered: list[Job] = []
        for record in self.load_all():
            if record.state.is_terminal:
                recovered.append(record)
                continue
            interrupted = interrupted_record(record)
            try:
                self.save(interrupted)
            except OSError:
                logger.warning(
                    "Could not rewrite the interrupted record of job %s", record.id, exc_info=True
                )
            else:
                logger.info(
                    "Job %s was %s when the server stopped; it is now failed (%s)",
                    record.id,
                    record.state.value,
                    JOB_INTERRUPTED_CODE,
                )
            recovered.append(interrupted)
        return recovered


def interrupted_record(job: Job) -> Job:
    """Return ``job`` as the ``failed`` record a restart turns it into.

    ``finished_at`` is the moment the interruption was *noticed* — the previous
    process cannot have recorded when it died — and everything else the record
    said is kept, so the configuration, model and creation time of a job that
    never finished are still there to look at.
    """
    return job.model_copy(
        update={
            "state": JobState.FAILED,
            "error": ErrorInfo(code=JOB_INTERRUPTED_CODE, message=JOB_INTERRUPTED_MESSAGE),
            "finished_at": datetime.now(UTC),
        },
        deep=True,
    )


def _load_one(directory: Path) -> Job | None:
    """Load one job directory's record, or ``None`` with a logged reason."""
    if not directory.is_dir():
        return None  # a stray file (including a `*.tmp` left by a crash)
    path = directory / JOB_RECORD_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # An orphan: outputs from a run older than feature 057, or one whose
        # record never landed. A later feature surfaces these; startup does not.
        logger.debug("Job directory %s holds no %s; ignoring it", directory, JOB_RECORD_FILENAME)
        return None
    except OSError:
        logger.warning("Could not read the job record %s; ignoring it", path, exc_info=True)
        return None
    try:
        record = Job.model_validate_json(raw)
    except ValidationError:
        logger.warning("Job record %s is not a valid job; ignoring it", path, exc_info=True)
        return None
    if record.id != directory.name:
        # The ID is what every other path is built from (stems, exports), so a
        # record that disagrees with its own directory describes files that are
        # somewhere else. Refuse it rather than serve it.
        logger.warning(
            "Job record %s claims id %r but lives in %r; ignoring it",
            path,
            record.id,
            directory.name,
        )
        return None
    return record


def _sync_directory(directory: Path) -> None:
    """Make a published record's directory entry durable, where the platform allows.

    ``fsync`` on the containing directory is what persists a new directory
    entry on POSIX; Windows exposes no directory handle to sync and does not
    need one, so a refusal to open it is expected rather than an error. The
    same helper :mod:`straticate.models.installer` uses, for the same reason —
    duplicated rather than shared because the two modules have no dependency on
    each other and this is four lines of platform behaviour, not a policy.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - platform-dependent
        logger.debug("Could not fsync the job directory %s", directory, exc_info=True)
    finally:
        os.close(descriptor)


__all__ = [
    "JOB_INTERRUPTED_CODE",
    "JOB_INTERRUPTED_MESSAGE",
    "JobStore",
    "interrupted_record",
]
