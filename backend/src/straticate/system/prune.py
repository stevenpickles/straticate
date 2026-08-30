"""Bulk manual cleanup of ``data_dir``: what to remove, and removing it.

Feature 059 (:mod:`straticate.system.disk_usage`) made everything under
``data_dir`` **visible**; feature 058 made a single job **deletable**. This
module is the third piece: everything visible, deletable in one call, in
typed classes that are each safe by construction rather than by the caller
being careful. It is the write half of the read 059 performs, and it
deliberately reuses that module's walker and its debris vocabulary rather
than growing a second opinion about either — what the report calls an orphan
is exactly what a prune removes.

**Plan, then remove.** Nothing is deleted while the plan is being built, and
nothing is planned that was not first measured in full:

- :func:`plan_prune` walks the trees, resolves the overlaps between classes,
  and returns the concrete list of paths to remove with the file count and
  byte total for each. It removes nothing.
- :func:`execute_prune` removes exactly that list and reports what actually
  went, which on Windows is not always the same thing.

The split is what makes three otherwise-awkward properties fall out for
free. The report's per-class figures **partition** what was removed, because
overlaps (a job whose whole directory is going, and whose ``exports/`` and
debris would otherwise each be counted a second time) are resolved once, at
planning time, before anything is gone and can no longer be measured. The
counts are honest, because they are measurements of files that were there
rather than estimates of files that used to be. And the event loop is never
blocked, because both halves are plain synchronous functions the route
offloads with :func:`asyncio.to_thread` — the same discipline
``GET /system/disk-usage`` follows for a walk that only *reads* the same
directories.

**Never delete what you could not see.** A target whose measurement was
incomplete — a subtree this process cannot list — is not removed. It is
reported as a :class:`~straticate.schemas.PruneFailure` with reason
:data:`UNREADABLE` and left exactly as it was. This is the conscious call
feature 059 left to 060 (its "unreadable subtree reports zero,
indistinguishable from empty" limitation): for a *report*, an undercount is
a cosmetic flaw; for a *delete*, "there is nothing here" and "I could not
look" are opposite instructions, and only one of them is recoverable if it
is wrong.

**Never touch a job that has not finished.** A queued or running job's
directory is excluded from all three classes, unconditionally and before any
other rule applies. This is not merely politeness toward a progress bar:
:class:`~straticate.inference.FakeSeparator` (and the real separator) writes
each stem to ``{stem}.wav.part`` and renames it into place, so a running
job's directory contains files that *are* debris by name and are the live
output of work in progress by fact. Sweeping them would corrupt the
separation that is producing them.

**Never touch an upload that is still arriving.** Between
:meth:`~straticate.audio.AudioStore.prepare_original_path` and
:meth:`~straticate.audio.AudioStore.register`, an upload has a directory and
no record, which is 059's definition of an orphan. The store reserves those
IDs (:meth:`~straticate.audio.AudioStore.pending_ids`) and the route passes
them in among the live ones, so the one thing this module cannot distinguish
from the filesystem alone is settled by the component that knows.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from straticate.jobs.layout import JOBS_DIRECTORY, job_exports_dir, job_output_dir
from straticate.schemas import Job, PruneFailure, PruneRequest, ReclaimClass
from straticate.system.disk_usage import (
    AUDIO_DIRECTORY,
    DEBRIS_DIR_PREFIX,
    EXPORTS_DIRECTORY,
    is_debris,
    walk_files,
)

logger = logging.getLogger(__name__)

UNREADABLE = "unreadable"
"""``PruneFailure.reason``: the target could not be measured, so it was left alone."""

PARTIALLY_REMOVED = "partially_removed"
"""``PruneFailure.reason``: removal left something behind (a locked file on Windows)."""

FILESYSTEM_ERROR = "filesystem_error"
"""``PruneFailure.reason``: the removal itself failed outright."""


@dataclass(frozen=True)
class PruneTarget:
    """One measured thing a prune will remove: a directory, or a single file.

    Attributes:
        reclaim_class: Which class claimed this target. Every target belongs
            to exactly one, which is what makes the report's per-class
            figures a partition rather than three overlapping views.
        target: Path relative to ``data_dir``, POSIX-separated — what reaches
            a client in :class:`~straticate.schemas.PruneFailure`. Absolute
            server paths never go on the wire.
        path: The absolute path to remove. Internal; never serialized.
        is_directory: Whether ``path`` was a directory when it was measured.
            Recorded rather than re-checked so removal cannot be confused by
            something that changed underneath it.
        files: Files counted under ``path`` (``1`` for a file target). The
            same unit as :attr:`straticate.schemas.UsageBucket.count`.
        bytes: Total size of those files.
        orphan_key: ``"audio/{id}"`` or ``"jobs/{id}"`` for a whole-directory
            orphan; ``None`` for everything else. The route re-checks these
            against the live registries immediately before removal — see
            :func:`plan_prune` on the one race that check exists to close.
        job_id: The job this target belongs to, for the ``terminal_jobs``
            class only. That class's removal is not a plain ``rmtree``: the
            manager entry and the record have to come off first, through
            :func:`straticate.jobs.removal.detach_job`, which needs the id
            rather than the path.
    """

    reclaim_class: ReclaimClass
    target: str
    path: Path
    is_directory: bool
    files: int
    bytes: int
    orphan_key: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class PrunePlan:
    """Everything a prune will remove, and everything it already refused to.

    ``failures`` here are the planning-time refusals only (targets that could
    not be measured); :func:`execute_prune` adds its own for removals that
    did not fully succeed.
    """

    targets: tuple[PruneTarget, ...]
    failures: tuple[PruneFailure, ...]


def _relative(data_dir: Path, path: Path) -> str:
    """Return ``path`` relative to ``data_dir``, POSIX-separated.

    Windows separators would make the same target read differently on
    different hosts for no benefit, and the value is a client-facing
    identifier rather than a path anything opens.
    """
    return path.relative_to(data_dir).as_posix()


def _measure(
    reclaim_class: ReclaimClass,
    data_dir: Path,
    path: Path,
    *,
    orphan_key: str | None = None,
    job_id: str | None = None,
) -> PruneTarget | PruneFailure | None:
    """Measure one candidate: a target to remove, a refusal, or nothing at all.

    Returns ``None`` when the candidate is simply not there any more — it
    needs no removal and is not a failure. An unreadable directory is a
    :class:`~straticate.schemas.PruneFailure`, never a target: see the module
    docstring on why an undercount cannot be allowed to authorize a delete.
    """
    target = _relative(data_dir, path)
    if path.is_dir():
        walk = walk_files(path)
        if not walk.complete:
            logger.warning("Refusing to prune %s: it could not be read in full", path)
            return PruneFailure(reclaim_class=reclaim_class, target=target, reason=UNREADABLE)
        files, size = walk.totals()
        return PruneTarget(
            reclaim_class=reclaim_class,
            target=target,
            path=path,
            is_directory=True,
            files=files,
            bytes=size,
            orphan_key=orphan_key,
            job_id=job_id,
        )
    try:
        size = path.stat().st_size
    except OSError:
        return None  # gone between listing and measuring; nothing to remove
    return PruneTarget(
        reclaim_class=reclaim_class,
        target=target,
        path=path,
        is_directory=False,
        files=1,
        bytes=size,
        orphan_key=orphan_key,
        job_id=job_id,
    )


def _list_children(root: Path) -> tuple[list[Path], bool]:
    """List ``root``'s immediate children; the flag is whether the listing worked.

    A root that does not exist is not a failure — a fresh checkout has
    neither ``audio/`` nor ``jobs/``, and an empty listing is the exact
    answer. A root that exists and cannot be listed is, because every orphan
    decision under it depends on seeing the whole listing.
    """
    if not root.is_dir():
        return [], True
    try:
        return sorted(root.iterdir()), True
    except OSError as exc:
        logger.warning("Could not list %s while planning a prune: %s", root, exc)
        return [], False


def is_expired(job: Job, older_than_seconds: int | None, now: datetime) -> bool:
    """Whether ``job`` falls outside the retention window.

    ``older_than_seconds`` of ``None`` means "no window": every terminal job
    qualifies. Otherwise the job must have finished at least that long ago.

    **A terminal job with no ``finished_at`` never qualifies while a window
    is set.** Its age is unknown, and the whole point of asking for a window
    is to keep recent work; treating an unknown age as "old enough" would
    delete the one job the caller could not reason about. (The manager sets
    ``finished_at`` on every terminal transition, and
    :func:`straticate.jobs.interrupted_record` sets it on recovery, so this
    is a defensive branch for hand-written records rather than a state the
    application produces.)
    """
    if older_than_seconds is None:
        return True
    if job.finished_at is None:
        return False
    return (now - job.finished_at).total_seconds() >= older_than_seconds


def _plan_debris(
    data_dir: Path,
    directory: Path,
    *,
    skip_top_level: str | None,
    targets: list[PruneTarget],
    failures: list[PruneFailure],
) -> None:
    """Plan the removal of build debris inside a directory that is otherwise live.

    A ``*.tmp`` sidecar, a ``*.part`` artifact or a ``.build-*`` staging
    directory is proof of a write or build that never finished (see
    :func:`straticate.system.disk_usage.is_debris`), and is never the record
    or the output — so it is removable even here, inside a live upload or a
    finished job.

    Debris under a ``.build-*`` directory is collapsed to that **directory**
    rather than planned file by file: removing the members and leaving the
    empty staging directory behind would make a second prune report a
    different answer to the same question.

    ``skip_top_level`` excludes one immediate subdirectory (in practice
    ``exports/``, when the ``export_caches`` class is already taking the
    whole thing) so the two classes cannot both count the same bytes.
    """
    walk = walk_files(directory)
    if not walk.complete:
        logger.warning("Refusing to sweep debris in %s: it could not be read in full", directory)
        failures.append(
            PruneFailure(
                reclaim_class=ReclaimClass.ORPHANS,
                target=_relative(data_dir, directory),
                reason=UNREADABLE,
            )
        )
        return
    staging_dirs: dict[Path, None] = {}
    for parts, size in walk.entries:
        if skip_top_level is not None and parts[:1] == (skip_top_level,):
            continue
        if not is_debris(parts):
            continue
        depth = next(
            (index for index, part in enumerate(parts) if part.startswith(DEBRIS_DIR_PREFIX)),
            None,
        )
        if depth is not None:
            staging_dirs.setdefault(directory.joinpath(*parts[: depth + 1]))
            continue
        path = directory.joinpath(*parts)
        targets.append(
            PruneTarget(
                reclaim_class=ReclaimClass.ORPHANS,
                target=_relative(data_dir, path),
                path=path,
                is_directory=False,
                files=1,
                bytes=size,
            )
        )
    for staging in staging_dirs:
        outcome = _measure(ReclaimClass.ORPHANS, data_dir, staging)
        _collect(outcome, targets, failures)


def _collect(
    outcome: PruneTarget | PruneFailure | None,
    targets: list[PruneTarget],
    failures: list[PruneFailure],
) -> None:
    """File one :func:`_measure` outcome into the plan it belongs in."""
    if isinstance(outcome, PruneTarget):
        targets.append(outcome)
    elif isinstance(outcome, PruneFailure):
        failures.append(outcome)


def plan_prune(
    data_dir: Path,
    request: PruneRequest,
    *,
    live_audio_ids: Collection[str],
    jobs: Sequence[Job],
    now: datetime,
) -> PrunePlan:
    """Decide what a prune will remove, measuring every target first.

    **Pure and synchronous, and blocking**: it walks and stats but writes
    nothing, the same shape as :func:`straticate.system.disk_usage.
    disk_usage_report`. Callers on the event loop must offload it.

    Classes are resolved in the order ``terminal_jobs`` → ``export_caches``
    → ``orphans``, and each claims its targets exclusively:

    - A job selected by ``terminal_jobs`` has its **whole directory** as one
      target. Its ``exports/`` is therefore skipped by ``export_caches``, and
      its debris by ``orphans``, which would otherwise count the same bytes
      two or three times over and make the report's total exceed what was
      actually freed. A job whose directory could not be measured stays
      claimed too — the other classes leave alone what this one refused.
    - ``export_caches`` claims a terminal job's ``exports/`` directory, so
      ``orphans`` skips debris inside it.
    - ``orphans`` takes what is left: directories under ``audio/`` and
      ``jobs/`` with no live record, and debris inside the ones that have.

    Args:
        data_dir: :attr:`straticate.config.Settings.data_dir`.
        request: The classes to reclaim, and the retention window.
        live_audio_ids: Every upload ID that must be treated as live —
            registered records **and** in-flight uploads
            (:meth:`~straticate.audio.AudioStore.pending_ids`) **and** the
            audio of any unfinished job. The route assembles these; this
            function does not know which is which, and does not need to.
        jobs: Every job the manager currently knows about, in any state. The
            unfinished ones are excluded from all three classes here.
        now: The instant the retention window is measured against, passed in
            rather than read so a test can name it.

    Returns:
        The concrete list of targets, plus the refusals planning already
        decided on. Removing nothing (an empty request, an empty
        ``data_dir``) is an ordinary result, not a special case.

    One thing this function cannot do by itself: the live IDs it is given are
    read on the event loop *before* this walk starts, so a job submitted (or
    an upload begun) while the walk is running would appear on disk without
    appearing in ``jobs`` — and would be classified as an orphan. Whole-
    directory orphan targets therefore carry an ``orphan_key`` for the route
    to re-check against the live registries immediately before removal, when
    it is back on the loop and nothing can change underneath it.
    """
    targets: list[PruneTarget] = []
    failures: list[PruneFailure] = []

    live_audio = set(live_audio_ids)
    live_job_ids = {job.id for job in jobs}
    active_job_ids = {job.id for job in jobs if not job.state.is_terminal}
    terminal = [job for job in jobs if job.state.is_terminal]

    claimed_job_ids: set[str] = set()
    if request.terminal_jobs:
        for job in terminal:
            if not is_expired(job, request.older_than_seconds, now):
                continue
            claimed_job_ids.add(job.id)
            directory = job_output_dir(data_dir, job.id)
            outcome = _measure(
                ReclaimClass.TERMINAL_JOBS, data_dir, directory, job_id=job.id
            ) or PruneTarget(
                # A selected job whose directory is already gone still has a
                # manager entry to drop, and dropping it is the only way the
                # job ever leaves ``GET /jobs``. Removing nothing is the
                # right *disk* outcome, not a reason to skip the job.
                reclaim_class=ReclaimClass.TERMINAL_JOBS,
                target=_relative(data_dir, directory),
                path=directory,
                is_directory=True,
                files=0,
                bytes=0,
                job_id=job.id,
            )
            _collect(outcome, targets, failures)

    export_cache_job_ids: set[str] = set()
    if request.export_caches:
        for job in terminal:
            if job.id in claimed_job_ids:
                continue
            exports = job_exports_dir(data_dir, job.id)
            if not exports.is_dir():
                continue
            outcome = _measure(ReclaimClass.EXPORT_CACHES, data_dir, exports)
            if isinstance(outcome, PruneTarget):
                export_cache_job_ids.add(job.id)
            _collect(outcome, targets, failures)

    if request.orphans:
        audio_root = data_dir / AUDIO_DIRECTORY
        children, listed = _list_children(audio_root)
        if not listed:
            failures.append(
                PruneFailure(
                    reclaim_class=ReclaimClass.ORPHANS,
                    target=AUDIO_DIRECTORY,
                    reason=UNREADABLE,
                )
            )
        for child in children:
            if child.name in live_audio:
                _plan_debris(
                    data_dir, child, skip_top_level=None, targets=targets, failures=failures
                )
                continue
            outcome = _measure(
                ReclaimClass.ORPHANS,
                data_dir,
                child,
                orphan_key=f"{AUDIO_DIRECTORY}/{child.name}",
            )
            _collect(outcome, targets, failures)

        jobs_root = data_dir / JOBS_DIRECTORY
        children, listed = _list_children(jobs_root)
        if not listed:
            failures.append(
                PruneFailure(
                    reclaim_class=ReclaimClass.ORPHANS,
                    target=JOBS_DIRECTORY,
                    reason=UNREADABLE,
                )
            )
        for child in children:
            if child.name in active_job_ids or child.name in claimed_job_ids:
                # Unfinished work is never touched; a job the terminal_jobs
                # class claimed is removed there, whole, exports and all.
                continue
            if child.name in live_job_ids:
                _plan_debris(
                    data_dir,
                    child,
                    skip_top_level=(
                        EXPORTS_DIRECTORY if child.name in export_cache_job_ids else None
                    ),
                    targets=targets,
                    failures=failures,
                )
                continue
            outcome = _measure(
                ReclaimClass.ORPHANS,
                data_dir,
                child,
                orphan_key=f"{JOBS_DIRECTORY}/{child.name}",
            )
            _collect(outcome, targets, failures)

    return PrunePlan(targets=tuple(targets), failures=tuple(failures))


def _remove_one(target: PruneTarget) -> tuple[int, int, str | None]:
    """Remove one target; return ``(files removed, bytes freed, failure reason)``.

    A directory goes with ``shutil.rmtree(..., ignore_errors=True)``, the
    same tolerance ``DELETE /jobs/{job_id}`` and ``DELETE /audio/{audio_id}``
    already use — a file held open by an in-flight download cannot be
    unlinked on Windows, and one such file must not cost the caller
    everything else in the tree.

    But tolerating it silently would make the *report* wrong, and this
    report's whole value is that its numbers can be trusted against a
    disk-usage reading. So a directory that survives its own removal is
    re-walked and the survivors are **subtracted**: what went is counted,
    what stayed is not, and the difference is reported as
    :data:`PARTIALLY_REMOVED`.
    """
    path = target.path
    if target.is_directory:
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return target.files, target.bytes, None
        survivors = walk_files(path)
        remaining_files, remaining_bytes = survivors.totals()
        logger.warning("Pruning %s left %d file(s) behind", path, remaining_files)
        return (
            max(target.files - remaining_files, 0),
            max(target.bytes - remaining_bytes, 0),
            PARTIALLY_REMOVED,
        )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not prune %s: %s", path, exc)
        return 0, 0, FILESYSTEM_ERROR
    return target.files, target.bytes, None


def execute_prune(
    targets: Sequence[PruneTarget],
) -> tuple[dict[ReclaimClass, tuple[int, int]], list[PruneFailure]]:
    """Remove every planned target; report what actually went, per class.

    **Blocking**: ``rmtree`` over a job directory of separated stems is real
    filesystem work, and the caller offloads this exactly as it offloads the
    planning walk.

    Returns:
        A ``{class: (files removed, bytes freed)}`` mapping covering only the
        classes that had targets, and one
        :class:`~straticate.schemas.PruneFailure` per target that did not
        come away cleanly. The mapping's totals are what the response
        reports, so they describe removals that happened rather than
        removals that were planned.
    """
    totals: dict[ReclaimClass, tuple[int, int]] = {}
    failures: list[PruneFailure] = []
    for target in targets:
        files, size, reason = _remove_one(target)
        counted_files, counted_bytes = totals.get(target.reclaim_class, (0, 0))
        totals[target.reclaim_class] = (counted_files + files, counted_bytes + size)
        if reason is not None:
            failures.append(
                PruneFailure(
                    reclaim_class=target.reclaim_class, target=target.target, reason=reason
                )
            )
    return totals, failures


__all__ = [
    "FILESYSTEM_ERROR",
    "PARTIALLY_REMOVED",
    "UNREADABLE",
    "PrunePlan",
    "PruneTarget",
    "execute_prune",
    "is_expired",
    "plan_prune",
]
