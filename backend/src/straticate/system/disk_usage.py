"""What Straticate holds under ``data_dir``, classified and counted.

Feature 040 (:mod:`straticate.system.storage`) answers "is there room to
install a model" for ``Settings.models_dir``. Nothing answers the companion
question for ``Settings.data_dir`` — the directory uploads (056), job
records and stems (057), and export artifacts (022) accumulate under,
none of which anything ever prunes. This module is the read: classify every
file under ``{data_dir}/audio`` and ``{data_dir}/jobs`` into the four buckets
:class:`~straticate.schemas.DiskUsageReport` describes, plus the free/total
bytes of the filesystem holding ``data_dir``. It deletes nothing — a later
feature (060) is the prune that acts on what this makes visible.

**Pure and synchronous**, the same shape as :func:`straticate.system.storage.
storage_report`: it takes the live registries as *plain ID collections*
rather than an :class:`~straticate.audio.AudioStore` or
:class:`~straticate.jobs.JobManager`, so it has no application state to
import and a test can hand it a bare ``{"abc"}`` without constructing either.
The route (:func:`straticate.api.system.read_disk_usage`) is what reads those
IDs from the running application and offloads this call to a worker thread —
``os.walk`` and ``os.stat`` are filesystem calls with the same "an
unresponsive network mount blocks for as long as the mount does" shape 040's
docstring already argues, on the very directory every upload and every job
also writes through, so the discipline is at least as important here.

**Never raises.** A subtree that cannot be listed (a permissions failure, a
directory that vanished mid-walk) is logged at warning level and simply
contributes nothing further from that point — the same "one bad read must
not break the whole surface" discipline :mod:`straticate.system.devices` and
:mod:`straticate.system.storage` already follow. Every :class:`~straticate.
schemas.UsageBucket` count is a plain, non-nullable ``int``: unlike
``free_bytes``/``total_bytes`` there is no "unknown" state to express, so an
unreadable subtree is undercounted rather than represented as null — the
figures degrade toward zero, never toward an exception.

**Which is why the walk also says whether it saw everything.**
:attr:`~straticate.schemas.DiskUsageReport.complete` is feature 060's
addition (059 recorded the gap): an undercount and an empty directory are
the same four numbers, and that ambiguity stops being harmless the moment
``POST /system/prune`` deletes on the strength of them. The walker
(:func:`walk_files`) reports it, the report carries it, and
:mod:`straticate.system.prune` refuses to remove anything it could not first
measure in full — reusing this module's walker and its debris vocabulary
rather than growing a second opinion about either.
"""

import logging
import os
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from straticate.jobs.layout import EXPORTS_DIRECTORY, JOBS_DIRECTORY
from straticate.schemas import DiskUsageReport, UsageBucket
from straticate.system.storage import DiskUsageReader, storage_report

logger = logging.getLogger(__name__)

AUDIO_DIRECTORY = "audio"
"""Name of the upload root under ``data_dir`` (see :mod:`straticate.audio.storage`)."""

_DEBRIS_SUFFIXES = (".tmp", ".part")
"""File suffixes an interrupted atomic write or export build can leave behind.

``*.tmp`` is the audio (:mod:`straticate.audio.storage`) and job
(:mod:`straticate.jobs.store`) sidecar write pattern; ``*.part`` is the
export build pattern (:mod:`straticate.api.export`). Both are renamed into
place on success, so surviving under either name means the write or build
that produced them never finished.
"""

DEBRIS_DIR_PREFIX = ".build-"
"""Prefix of a staging directory an interrupted archive export build leaves.

:func:`straticate.api.export.build_artifact` stages a multi-stem zip in a
``TemporaryDirectory(prefix=".build-")`` beside the artifact it is building;
a crash mid-build leaves the directory (and its partially transcoded
members) behind.

Public alongside :func:`is_debris` because a prune must remove the staging
*directory*, not merely the files inside it — :func:`is_debris` answers
"is this debris", and this answers "how far up does the debris start".
"""


def is_debris(relative_parts: tuple[str, ...]) -> bool:
    """Whether ``relative_parts`` (relative to its ``{id}`` root) names leftover build debris.

    Debris is recognised by name, not by location: a ``*.tmp``/``*.part``
    file, or anything nested under a ``.build-*`` staging directory, counts
    as debris wherever it sits — including inside an otherwise-live upload
    or job directory, since it is never the record or the output there
    either, only a write or build that never finished.

    **Public because prune must not hold a second opinion.**
    :mod:`straticate.system.prune` removes exactly what this function calls
    debris, so the classification a client sees under ``orphans`` and the
    set of files a prune deletes are the same set by construction — the same
    argument feature 058 made for moving the exports path into one function.
    """
    if not relative_parts:
        # A plain file sitting directly at the swept root whose name matches
        # a live id reaches here with an empty tuple (review finding) — it is
        # not debris, and indexing [-1] would break the never-raises contract.
        return False
    name = relative_parts[-1]
    if name.endswith(_DEBRIS_SUFFIXES):
        return True
    return any(part.startswith(DEBRIS_DIR_PREFIX) for part in relative_parts[:-1])


def _add(bucket: UsageBucket, size: int) -> UsageBucket:
    """Return ``bucket`` with one more file of ``size`` bytes counted in."""
    return UsageBucket(count=bucket.count + 1, bytes=bucket.bytes + size)


@dataclass(frozen=True)
class WalkResult:
    """Every file found under one root, and whether the walk saw all of them.

    Attributes:
        entries: ``(parts relative to the root, size in bytes)`` per file.
        complete: ``False`` if any directory could not be listed or any file
            could not be ``stat``-ed. The entries are still valid — they are
            simply not all of them, by an unknown amount.
    """

    entries: list[tuple[tuple[str, ...], int]]
    complete: bool

    def totals(self) -> tuple[int, int]:
        """Return ``(file count, total bytes)`` for the entries found."""
        return len(self.entries), sum(size for _parts, size in self.entries)


def walk_files(root: Path) -> WalkResult:
    """List every file under ``root`` as ``(parts relative to root, size)``.

    Never raises: a subtree ``os.walk`` cannot list (permissions, a
    directory that vanished mid-walk) is logged and simply yields nothing
    further from that point — the walk continues with siblings that are
    still readable. A file that vanishes between being listed and being
    ``stat``-ed (a concurrent upload or export finishing, a delete racing
    this read) is skipped the same way: it is no longer there to count.

    Either of those clears :attr:`WalkResult.complete`. **A vanished file
    does not actually hide anything** — it is gone, so nothing is missed by
    not counting it — yet it clears the flag too, because telling it apart
    from a permissions failure means trusting an ``errno`` to distinguish
    "no longer there" from "there and unreadable", and the cost of being
    wrong is asymmetric: an over-cautious flag defers a prune, an
    over-confident one deletes a directory nobody could see inside. A root
    that simply does not exist is not a failure — there is nothing to read,
    and the empty result is exact.
    """
    if not root.is_dir():
        return WalkResult(entries=[], complete=True)
    entries: list[tuple[tuple[str, ...], int]] = []
    complete = True

    def _on_error(exc: OSError) -> None:
        # exc.filename names the directory that actually failed, which may be
        # a subdirectory rather than the swept root (review finding).
        nonlocal complete
        complete = False
        logger.warning(
            "Could not list %s while measuring disk usage: %s", exc.filename or root, exc
        )

    for dirpath, _dirnames, filenames in os.walk(root, onerror=_on_error):
        directory = Path(dirpath)
        for filename in filenames:
            path = directory / filename
            try:
                size = path.stat().st_size
            except OSError as exc:
                complete = False
                logger.warning("Could not stat %s while measuring disk usage: %s", path, exc)
                continue
            entries.append((path.relative_to(root).parts, size))
    return WalkResult(entries=entries, complete=complete)


def _classify_audio(
    entries: list[tuple[tuple[str, ...], int]], audio_ids: Collection[str]
) -> tuple[UsageBucket, UsageBucket]:
    """Split ``{data_dir}/audio``'s files into ``(uploads, orphans)``."""
    live = set(audio_ids)
    uploads = UsageBucket(count=0, bytes=0)
    orphans = UsageBucket(count=0, bytes=0)
    for parts, size in entries:
        audio_id = parts[0]
        if audio_id in live and not is_debris(parts[1:]):
            uploads = _add(uploads, size)
        else:
            orphans = _add(orphans, size)
    return uploads, orphans


def _classify_jobs(
    entries: list[tuple[tuple[str, ...], int]], job_ids: Collection[str]
) -> tuple[UsageBucket, UsageBucket, UsageBucket]:
    """Split ``{data_dir}/jobs``'s files into ``(job_stems, job_exports, orphans)``.

    A job counts as live from the moment it is submitted (feature 057 writes
    its record before any executor runs), so a queued or running job's
    directory is classified here exactly like a completed one's — never as
    an orphan — whatever it does or does not hold under ``stems/`` yet.
    """
    live = set(job_ids)
    job_stems = UsageBucket(count=0, bytes=0)
    job_exports = UsageBucket(count=0, bytes=0)
    orphans = UsageBucket(count=0, bytes=0)
    for parts, size in entries:
        job_id = parts[0]
        rest = parts[1:]
        if job_id not in live or is_debris(rest):
            orphans = _add(orphans, size)
        elif rest[:1] == (EXPORTS_DIRECTORY,):
            job_exports = _add(job_exports, size)
        else:
            job_stems = _add(job_stems, size)
    return job_stems, job_exports, orphans


def disk_usage_report(
    data_dir: Path,
    *,
    audio_ids: Collection[str],
    job_ids: Collection[str],
    read_usage: DiskUsageReader | None = None,
) -> DiskUsageReport:
    """Classify everything under ``data_dir`` and report the holding filesystem.

    Args:
        data_dir: :attr:`straticate.config.Settings.data_dir`. Need not
            exist — a fresh checkout with nothing uploaded and no job ever
            submitted reports all-zero buckets, and the free/total figures
            still describe the nearest existing ancestor (see
            :func:`straticate.system.storage.storage_report`).
        audio_ids: Every currently registered upload ID
            (:meth:`straticate.audio.AudioStore.ids`). A live registry, not
            the store: this function has no application state to import.
        job_ids: Every job the manager currently knows about, in any state
            (:meth:`straticate.jobs.JobManager.list_jobs`, mapped to
            ``.id``). Includes queued and running jobs, not only terminal
            ones — see :func:`_classify_jobs`.
        read_usage: Forwarded to :func:`~straticate.system.storage.
            storage_report`; tests inject platform failures through it.

    Returns:
        A report whose four buckets partition every file this walk could
        read (nothing counted twice, nothing silently invented), whose
        ``complete`` says whether "could read" was in fact everything, and
        whose ``free_bytes``/``total_bytes`` follow the null-means-unknown
        doctrine of :class:`~straticate.schemas.StorageReport`.

    **Never raises**, and blocking: see the module docstring. Callers on the
    event loop must offload this with :func:`asyncio.to_thread`, exactly as
    :func:`straticate.api.system.read_storage` already does for 040's report.
    """
    audio = walk_files(data_dir / AUDIO_DIRECTORY)
    uploads, audio_orphans = _classify_audio(audio.entries, audio_ids)

    jobs = walk_files(data_dir / JOBS_DIRECTORY)
    job_stems, job_exports, job_orphans = _classify_jobs(jobs.entries, job_ids)

    orphans = UsageBucket(
        count=audio_orphans.count + job_orphans.count,
        bytes=audio_orphans.bytes + job_orphans.bytes,
    )

    space = storage_report(data_dir, read_usage)

    return DiskUsageReport(
        uploads=uploads,
        job_stems=job_stems,
        job_exports=job_exports,
        orphans=orphans,
        complete=audio.complete and jobs.complete,
        free_bytes=space.free_bytes,
        total_bytes=space.total_bytes,
    )


__all__ = [
    "AUDIO_DIRECTORY",
    "DEBRIS_DIR_PREFIX",
    "EXPORTS_DIRECTORY",
    "WalkResult",
    "disk_usage_report",
    "is_debris",
    "walk_files",
]
