"""What Straticate holds under ``data_dir``, and how much of it there is.

Feature 040 answers "is there room to install a model" for `models_dir`.
Nothing answers the companion question for `data_dir` — the directory
uploads, job stems and export artifacts accumulate under, none of which is
ever pruned (021, 022, 025, 040 all record the gap; 060 is the first feature
that acts on it). This module is the visibility half: report what is there,
in shapes a client can render and a future prune endpoint can act on,
without deleting anything itself.
"""

from pydantic import BaseModel, Field


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
