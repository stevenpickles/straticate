"""What Straticate holds under ``data_dir``, how much of it there is, and how to reclaim it.

Feature 040 answers "is there room to install a model" for `models_dir`.
Nothing answered the companion question for `data_dir` — the directory
uploads, job stems and export artifacts accumulate under, none of which was
ever pruned (021, 022, 025, 040 all record the gap). Feature 059 is the
visibility half: :class:`DiskUsageReport` reports what is there without
deleting anything. Feature 060 is the write half: :class:`PruneRequest` and
:class:`PruneReport` describe a **bulk manual** cleanup of exactly the
classes 059 makes visible, and nothing else.

The two halves share a unit deliberately. ``UsageBucket.count`` and
``PruneClassReport.items_removed`` are both **file** counts, so a
disk-usage report taken before a prune and the prune's own report are
directly comparable: what the report said was there is what the prune says
it removed.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UsageBucket(BaseModel):
    """How many files, and how many bytes, one classification accounts for.

    ``count`` is a **file** count, not a directory or upload count: an
    upload contributes its original media plus its ``audio.json`` sidecar as
    two files, a job with three stems and a record contributes four. That
    definition is what lets a test recompute the same numbers with a plain
    ``os.walk`` and compare them directly against this bucket, file for file
    and byte for byte, rather than reverse-engineering some other unit.
    """

    count: int = Field(ge=0, description="Number of files counted toward this bucket.")
    bytes: int = Field(ge=0, description="Total size in bytes of the files counted.")


class DiskUsageReport(BaseModel):
    """What ``GET /system/disk-usage`` answers: where `data_dir`'s bytes went.

    Four buckets partition every file under ``{data_dir}/audio`` and
    ``{data_dir}/jobs`` — nothing is counted twice and nothing is left out:

    - ``uploads``: files inside an audio directory the upload registry still
      recognises (feature 056).
    - ``job_stems``: a known job's own files (its ``job.json`` record and its
      separated stems) — everything in the job's directory *except* its
      ``exports/`` subtree, which is split out below.
    - ``job_exports``: built export artifacts under a known job's
      ``exports/`` subtree (feature 022).
    - ``orphans``: everything else — a directory whose ``audio_id`` or
      ``job_id`` no longer has a live record (an interrupted upload, output
      from a run older than the durable registries, or a deleted job's
      leftovers), *and* stray build debris found anywhere in the swept
      trees: a ``*.tmp`` sidecar an interrupted write never renamed into
      place, a ``*.part`` export an interrupted build never published, or a
      ``.build-*`` staging directory a crashed archive build left behind.
      Debris counts as orphaned even inside an otherwise-live upload or job
      directory — it is never the record or the output, only a leftover
      nothing has claimed.

    A job that is still queued or running is not an orphan: its directory is
    known to the job manager the moment it is submitted (feature 057), so it
    is classified exactly like a completed job's, just with less (or
    nothing) written under ``stems/`` yet.

    ``complete`` is the fifth thing the four buckets cannot say. Every bucket
    count is a plain, non-nullable ``int``, so a subtree this process could
    not read is *undercounted* — and ``{count: 0, bytes: 0}`` for an
    unreadable directory is indistinguishable from the same figures for an
    empty one. That ambiguity was harmless while this report only had to be
    rendered (feature 059), and stops being harmless the moment something
    **deletes** on the strength of it (feature 060): "there is nothing here"
    and "I could not look" must not be the same answer to a prune. So the
    walk reports whether it saw everything, and ``POST /system/prune``
    refuses to remove anything it could not first measure in full.

    ``free_bytes`` / ``total_bytes`` describe the filesystem holding
    ``data_dir`` — the same **null-means-unknown** doctrine as
    :class:`~straticate.schemas.storage.StorageReport` (feature 040):
    ``null`` is a documented "the host could not answer", never conflated
    with a real ``0``. Reused rather than duplicated because the underlying
    read (:func:`straticate.system.storage.storage_report`) already handles
    exactly this — a ``data_dir`` that does not exist yet reports on its
    nearest existing ancestor, and a permissions failure degrades to
    ``null`` instead of a ``500``.
    """

    uploads: UsageBucket = Field(description="Registered uploads under `{data_dir}/audio`.")
    job_stems: UsageBucket = Field(
        description="Known jobs' own files (records and stems) under `{data_dir}/jobs`."
    )
    job_exports: UsageBucket = Field(
        description="Known jobs' built export artifacts under `{data_dir}/jobs/{job_id}/exports`."
    )
    orphans: UsageBucket = Field(
        description="Files with no live record, plus stray build debris found while sweeping."
    )
    complete: bool = Field(
        default=True,
        description=(
            "Whether the walk could read everything under `data_dir`. False when a "
            "subtree could not be listed or a file could not be stat-ed, which means "
            "the buckets are an undercount of unknown size, not a measurement of "
            "an empty directory."
        ),
    )
    free_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Bytes available to the server on the filesystem holding `data_dir`, "
            "or null when the host cannot report it."
        ),
    )
    total_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Total size in bytes of that filesystem, or null when the host cannot report it."
        ),
    )


class ReclaimClass(StrEnum):
    """A class of disk space ``POST /system/prune`` can reclaim.

    Three classes, each **safe by construction** rather than by the caller
    being careful — that is the whole design of the endpoint. Nothing is ever
    removed unless the request names its class, and the job the server is
    running (or has queued) is excluded from all three:

    - :attr:`EXPORT_CACHES` — a terminal job's ``exports/`` directory. Export
      artifacts are pure caches: ``GET /jobs/{job_id}/export`` rebuilds any of
      them on demand from stems that are still there, so removing them costs a
      transcode and loses nothing.
    - :attr:`ORPHANS` — directories under ``audio/`` and ``jobs/`` with no
      live record, plus the ``*.tmp`` / ``*.part`` / ``.build-*`` debris an
      interrupted write or build left behind. Nothing in the API can reach any
      of it: it is exactly the ``orphans`` bucket of
      :class:`DiskUsageReport`.
    - :attr:`TERMINAL_JOBS` — a finished job in full (record, stems and
      exports), through the same path ``DELETE /jobs/{job_id}`` uses. This is
      the one class that removes something a client can still see, which is
      why it is also the one class with a retention window
      (``PruneRequest.older_than_seconds``).
    """

    EXPORT_CACHES = "export_caches"
    ORPHANS = "orphans"
    TERMINAL_JOBS = "terminal_jobs"


class PruneRequest(BaseModel):
    """Which classes of disk space to reclaim. Nothing is removed unless asked.

    Every flag defaults to ``False``, so an empty body (``{}``) is a valid
    request that deletes nothing and reports zeroes. That is deliberate: the
    dangerous default for a bulk delete is the one that does something, and a
    client that forgets a field must under-delete rather than over-delete.

    **Unknown fields are rejected** (``extra="forbid"``, a ``422``
    ``validation_error``). A misspelled class name — ``exports`` for
    ``export_caches`` — would otherwise be a silent no-op reported as a
    successful prune that freed nothing, which reads exactly like "there was
    nothing to reclaim". Refusing it says which of the two actually happened.
    """

    model_config = ConfigDict(extra="forbid")

    export_caches: bool = Field(
        default=False,
        description=(
            "Remove every terminal job's `exports/` directory. Export artifacts are "
            "caches rebuilt on demand by `GET /jobs/{job_id}/export`."
        ),
    )
    orphans: bool = Field(
        default=False,
        description=(
            "Remove `audio/` and `jobs/` directories with no live record, plus stray "
            "`*.tmp` / `*.part` / `.build-*` build debris — the `orphans` bucket of "
            "`GET /system/disk-usage`."
        ),
    )
    terminal_jobs: bool = Field(
        default=False,
        description=(
            "Remove finished jobs wholesale — record, stems and exports — through the "
            "same path as `DELETE /jobs/{job_id}`. Never touches a queued or running job."
        ),
    )
    older_than_seconds: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Retention window for `terminal_jobs` only: keep jobs that finished within "
            "this many seconds. Null removes every terminal job. A terminal job with no "
            "`finished_at` is kept whenever this is set, because its age is unknown."
        ),
    )

    @model_validator(mode="after")
    def _older_than_needs_terminal_jobs(self) -> Self:
        """Refuse a retention window that would silently apply to nothing.

        ``older_than_seconds`` is the only field that narrows a class rather
        than selecting one, and it narrows exactly one class. Accepting it
        without ``terminal_jobs`` would let a request that reads as cautious
        ("only remove things older than a week") mean something quite
        different from what it says — paired with ``orphans: true``, a caller
        could reasonably believe the window applied there too. A ``422``
        naming the mismatch is the only answer that cannot be misread.
        """
        if self.older_than_seconds is not None and not self.terminal_jobs:
            raise ValueError(
                "older_than_seconds applies to terminal_jobs only; "
                "set terminal_jobs=true or omit the window"
            )
        return self


class PruneClassReport(BaseModel):
    """What one :class:`ReclaimClass` actually reclaimed.

    ``items_removed`` is a **file** count, the same unit as
    :attr:`UsageBucket.count`: removing a job directory holding a record and
    three stems reports four items, not one. That is what makes a disk-usage
    report taken before the prune and this report comparable — the numbers
    are in the same currency, so a test (and a client) can subtract them.

    A class the request did not name always reports zeroes. So does a class
    that was named and found nothing, and so does a class whose targets were
    all refused — the difference between the last two is in
    :attr:`PruneReport.failures`, never in these two integers.
    """

    items_removed: int = Field(ge=0, description="Number of files removed for this class.")
    bytes_freed: int = Field(ge=0, description="Total size in bytes of the files removed.")


class PruneFailure(BaseModel):
    """One thing the prune did not remove, and why. Never fails the request.

    A prune is a bulk operation over independent targets: one unreadable
    directory, one locked file or one job a concurrent ``DELETE`` got to
    first must not cost the caller every other reclaimed byte. Each is
    recorded here instead, and the response is still ``200`` — the same
    "degrade, do not error" posture ``GET /system/disk-usage`` takes for the
    read.

    ``target`` is a path **relative to** ``data_dir`` (``jobs/{job_id}``,
    ``audio/{audio_id}``, ``jobs/{job_id}/exports/x.wav.ab12.part``), never
    an absolute one: absolute server paths carry the operator's directory
    layout and home directory, which no other error in this application puts
    on the wire (see :mod:`straticate.api.export`).

    ``reason`` is a short, stable classification, never an OS error string:

    - ``unreadable`` — the target could not be measured in full, so it was
      not removed. Deleting what it could not first see is the one thing a
      prune must never do (see :attr:`DiskUsageReport.complete`).
    - ``partially_removed`` — removal left something behind: typically a file
      held open by an in-flight download, which Windows refuses to unlink.
      What did go is counted; what stayed is not.
    - ``filesystem_error`` — the removal itself failed outright.
    - ``job_not_found`` / ``job_active`` — the job manager refused the job
      between planning and removal (a concurrent ``DELETE``, or a job that
      somehow left its terminal state). Reported, never forced.
    """

    reclaim_class: ReclaimClass = Field(description="Which class this target belonged to.")
    target: str = Field(description="Path relative to `data_dir`. Never an absolute path.")
    reason: str = Field(description="Short classification: why this target was not removed.")


class PruneReport(BaseModel):
    """What ``POST /system/prune`` reclaimed, per class and in total.

    The three class reports **partition** what was removed: nothing is
    counted twice, so ``items_removed`` and ``bytes_freed`` are the plain
    sums of their three counterparts. That is not a coincidence maintained by
    hand — the plan resolves the overlaps before anything is removed. A job
    selected by ``terminal_jobs`` has its whole directory counted there and
    is skipped by ``export_caches`` and ``orphans``, which would otherwise
    count its exports and its debris a second time.

    The report is **honest about what did not happen**: see
    :class:`PruneFailure`. A second, identical request is the strongest
    statement of the same idea — it reports zeroes, because the first one
    really did remove what it said it removed.
    """

    export_caches: PruneClassReport = Field(description="Terminal jobs' export caches.")
    orphans: PruneClassReport = Field(description="Record-less directories and build debris.")
    terminal_jobs: PruneClassReport = Field(description="Finished jobs removed wholesale.")
    items_removed: int = Field(
        ge=0, description="Total files removed across every class (the sum of the three)."
    )
    bytes_freed: int = Field(
        ge=0, description="Total bytes freed across every class (the sum of the three)."
    )
    failures: list[PruneFailure] = Field(
        default_factory=list[PruneFailure],
        description=(
            "Targets that were not removed, with a short reason each. Empty on a clean run."
        ),
    )
