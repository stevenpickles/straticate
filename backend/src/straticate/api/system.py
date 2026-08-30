"""System endpoints: health, version, compute devices, storage, disk usage, prune."""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter

from straticate import __version__
from straticate.api.audio import SettingsDep, StoreDep
from straticate.api.results import ManagerDep
from straticate.errors import ApplicationError
from straticate.jobs.layout import JOBS_DIRECTORY
from straticate.jobs.removal import detach_job
from straticate.schemas import (
    ComputeDevice,
    DiskUsageReport,
    HealthStatus,
    PruneClassReport,
    PruneFailure,
    PruneReport,
    PruneRequest,
    ReclaimClass,
    StorageReport,
    VersionInfo,
)
from straticate.system import (
    AUDIO_DIRECTORY,
    DeviceDetectorDep,
    disk_usage_report,
    storage_report,
)
from straticate.system.prune import (
    FILESYSTEM_ERROR,
    PruneTarget,
    execute_prune,
    plan_prune,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> HealthStatus:
    """Report service liveness."""
    return HealthStatus(status="ok")


@router.get("/version")
async def version() -> VersionInfo:
    """Report the running application version."""
    return VersionInfo(version=__version__)


@router.get("/system/devices")
async def list_devices(detector: DeviceDetectorDep) -> list[ComputeDevice]:
    """List the logical compute devices available for separation jobs.

    Detected once at startup and cached — devices do not change during a run.
    NVIDIA CUDA devices come first (when a CUDA-capable PyTorch installation
    is present); the CPU device is always last and always present, so the list
    is never empty. ``memory_total_bytes`` is ``0`` when the host does not
    report a total (CPU only, on exotic platforms).
    """
    return detector.devices()


@router.get("/system/storage")
async def read_storage(settings: SettingsDep) -> StorageReport:
    """Report free and total bytes for the filesystem holding the models directory.

    This is the figure a client needs before offering an install: the weights
    are written by **this** process, to ``Settings.models_dir``, on **this**
    machine — a disk the browser cannot see (``navigator.storage.estimate()``
    describes the page origin's quota inside the browser profile, which is a
    different number about a different disk).

    Read fresh on every request rather than cached: free space changes
    constantly, and the underlying call is one syscall. Unlike
    ``/system/devices`` there is nothing here that could be probed once at
    startup and still be true.

    **It runs in a worker thread.** ``stat`` and ``shutil.disk_usage`` are
    filesystem calls, and on an unresponsive network mount — exactly the case
    this feature's warn-rather-than-refuse reasoning anticipates — they block
    for as long as the mount does. On the event loop that would stall *every*
    REST request, the feature 013 WebSocket hub and job progress delivery
    behind one `stat`, which is what AGENTS.md principle 4 and ARCHITECTURE.md
    §14 forbid; features 022 and 025 offload their blocking work for the same
    reason. Caching would also have hidden it, and caching is the wrong answer
    here (see above), so the read is offloaded instead.

    :func:`asyncio.to_thread` is not cancellable, which costs nothing here:
    this is a pure read that writes no state, so a client that disconnects
    mid-request simply leaves a worker thread to finish a ``stat`` and discard
    the answer. (025's export path has to *shield* its offloaded work because
    that work publishes an artifact; nothing here does.)

    **Both fields are ``null`` when the host cannot answer** — a models
    directory whose whole path is missing, a permissions failure, a filesystem
    the platform has no answer for. That is a documented state, not an error:
    the response is still ``200``, and a client renders "unknown" rather than a
    failure (see :mod:`straticate.system.storage`). ``free_bytes: 0`` says
    something quite different — a full disk — which is why unknown is not
    spelled ``0`` here the way an unknown device memory total is.
    """
    return await asyncio.to_thread(storage_report, settings.models_dir)


@router.get("/system/disk-usage")
async def read_disk_usage(
    settings: SettingsDep, store: StoreDep, manager: ManagerDep
) -> DiskUsageReport:
    """Report what Straticate holds under the data directory, and where.

    Four buckets partition every file under ``{data_dir}/audio`` and
    ``{data_dir}/jobs``: ``uploads`` (registered audio, feature 056),
    ``job_stems`` (a known job's own record and stems), ``job_exports`` (its
    built export artifacts, feature 022), and ``orphans`` (everything with
    no live record, plus stray build debris an interrupted write or export
    build left behind — see :mod:`straticate.system.disk_usage`). This is
    the visibility that makes manual-only retention livable before a prune
    endpoint (060) exists to act on it: nothing here deletes anything.

    "Live" is read from the running application at request time — the audio
    store's registry and the job manager's list, in whatever state each job
    is currently in — so a job still queued or running is classified exactly
    like a completed one's, never as an orphan.

    ``free_bytes``/``total_bytes`` describe the filesystem holding
    ``data_dir`` and follow the same null-means-unknown doctrine as
    ``GET /system/storage`` (feature 040): both fields are ``null`` together
    when the host cannot answer, and a missing ``data_dir`` (nothing has ever
    been uploaded or separated) reports on its nearest existing ancestor
    rather than as unknown — see :mod:`straticate.system.storage`.

    **It runs in a worker thread.** ``os.walk`` and ``os.stat`` over
    ``data_dir`` are filesystem calls with the same blocking-on-a-network-
    mount shape ``GET /system/storage`` already offloads (see its
    docstring), on the very directory every upload and every job also writes
    through — so this endpoint follows the same discipline.

    **Degrades rather than errors.** A ``data_dir`` that does not exist yet
    reports all-zero buckets (nothing has ever written under it) alongside
    the real free/total figures for its nearest existing ancestor; a subtree
    this process cannot read is logged and simply undercounted rather than
    failing the request — every bucket count is a plain, non-nullable
    integer, so there is no "unknown" to express there the way there is for
    the free/total figures. The response is always ``200``.
    """
    audio_ids = store.ids()
    job_ids = manager.ids()
    return await asyncio.to_thread(
        disk_usage_report, settings.data_dir, audio_ids=audio_ids, job_ids=job_ids
    )


@router.post("/system/prune")
async def prune_data_dir(
    request: PruneRequest, settings: SettingsDep, store: StoreDep, manager: ManagerDep
) -> PruneReport:
    """Reclaim disk space in the classes the request names — and only those.

    This is the write half of ``GET /system/disk-usage`` and the bulk form of
    ``DELETE /jobs/{job_id}``: everything that endpoint made visible, and that
    one made deletable, removable in one call. Three classes, each opt-in and
    each safe by construction (see
    :class:`~straticate.schemas.ReclaimClass` and
    :mod:`straticate.system.prune`):

    - ``export_caches`` — every terminal job's ``exports/`` directory. Export
      artifacts are pure caches: ``GET /jobs/{job_id}/export`` rebuilds any of
      them from stems that are still there, and feature 022's build locks,
      unique ``.part`` names and skip-the-rename-if-already-published rule
      make a rebuild racing another rebuild safe already. Removing them costs
      a transcode.
    - ``orphans`` — directories under ``audio/`` and ``jobs/`` with no live
      record, plus ``*.tmp`` / ``*.part`` / ``.build-*`` build debris. This is
      exactly the ``orphans`` bucket of the disk-usage report, and it is what
      finally sweeps the debris features 022 and 058 documented as
      permanently un-collectable.
    - ``terminal_jobs`` — finished jobs wholesale, through
      :func:`straticate.jobs.removal.detach_job`, which is
      ``DELETE /jobs/{job_id}``'s own implementation rather than a second copy
      of its ordering. ``older_than_seconds`` keeps recent ones.

    **Nothing is removed unless it is asked for.** Every flag defaults to
    ``False``, so ``{}`` is a valid request that frees nothing and reports
    zeroes. And a second identical request frees nothing either, which is the
    honest form of idempotence for a bulk delete: the resource-level statement
    ``DELETE`` makes with a ``404``, made with numbers instead.

    **A queued or running job is excluded from all three classes**, always.
    Its directory holds ``*.part`` files that are live output rather than
    debris — the separator writes each stem to one and renames it into place —
    so sweeping it would corrupt the work in progress. The same reasoning
    covers an upload still streaming: it has a directory and no record yet,
    which is indistinguishable from an orphan on disk, so the audio store
    reserves those IDs (``AudioStore.pending_ids``) and they are passed in
    here as live.

    **The work is offloaded, but the manager is not.** Planning (an
    ``os.walk`` of the whole data directory) and removal (``rmtree`` over job
    directories) both run in worker threads, the same discipline
    ``GET /system/disk-usage`` follows for the read. Between them, on the
    event loop and with no ``await`` in between, this handler drops the
    manager entry and unlinks the record for **every** selected job — feature
    058's ordering, applied to a batch: by the time the removal thread starts,
    no concurrent request can submit, cancel or delete any job in it, and no
    client can observe the batch half-detached.

    **It degrades rather than errors.** One unreadable directory, one locked
    file or one job a concurrent ``DELETE`` reached first is reported in
    ``failures`` and costs the caller nothing else; the response is always
    ``200``. In particular, a target that could not be *measured* is never
    removed: prune refuses to delete on the strength of an incomplete picture
    (see :attr:`~straticate.schemas.DiskUsageReport.complete`).

    Errors: an unknown field or a negative ``older_than_seconds`` is the
    standard ``validation_error`` (422), as is ``older_than_seconds`` without
    ``terminal_jobs`` — a retention window that would silently apply to
    nothing.
    """
    jobs = manager.list_jobs()
    live_audio_ids = (
        set(store.ids())
        | set(store.pending_ids())
        | {job.audio_id for job in jobs if not job.state.is_terminal}
    )
    plan = await asyncio.to_thread(
        plan_prune,
        settings.data_dir,
        request,
        live_audio_ids=live_audio_ids,
        jobs=jobs,
        now=datetime.now(UTC),
    )

    failures = list(plan.failures)
    # Everything from here to the to_thread below is synchronous on purpose:
    # the live sets are re-read *after* the walk, so a job submitted or an
    # upload begun while it ran is live by the time its directory is judged,
    # and every selected job is detached before the event loop can run
    # anything else (feature 058's ordering, applied to a batch).
    live_keys = {f"{AUDIO_DIRECTORY}/{audio_id}" for audio_id in store.ids()}
    live_keys |= {f"{AUDIO_DIRECTORY}/{audio_id}" for audio_id in store.pending_ids()}
    live_keys |= {f"{JOBS_DIRECTORY}/{job_id}" for job_id in manager.ids()}

    removable: list[PruneTarget] = []
    for target in plan.targets:
        if target.orphan_key is not None and target.orphan_key in live_keys:
            continue  # it acquired a record while we walked: not an orphan
        if target.job_id is not None:
            try:
                detach_job(manager, settings.data_dir, target.job_id)
            except ApplicationError as exc:
                failures.append(
                    PruneFailure(
                        reclaim_class=target.reclaim_class,
                        target=target.target,
                        reason=exc.code,
                    )
                )
                continue
            except OSError:
                logger.warning("Could not remove the record of job %s while pruning", target.job_id)
                failures.append(
                    PruneFailure(
                        reclaim_class=target.reclaim_class,
                        target=target.target,
                        reason=FILESYSTEM_ERROR,
                    )
                )
                continue
        removable.append(target)

    totals, removal_failures = await asyncio.to_thread(execute_prune, removable)
    failures.extend(removal_failures)

    def reclaimed(reclaim_class: ReclaimClass) -> PruneClassReport:
        files, freed = totals.get(reclaim_class, (0, 0))
        return PruneClassReport(items_removed=files, bytes_freed=freed)

    export_caches = reclaimed(ReclaimClass.EXPORT_CACHES)
    orphans = reclaimed(ReclaimClass.ORPHANS)
    terminal_jobs = reclaimed(ReclaimClass.TERMINAL_JOBS)
    report = PruneReport(
        export_caches=export_caches,
        orphans=orphans,
        terminal_jobs=terminal_jobs,
        items_removed=(
            export_caches.items_removed + orphans.items_removed + terminal_jobs.items_removed
        ),
        bytes_freed=export_caches.bytes_freed + orphans.bytes_freed + terminal_jobs.bytes_freed,
        failures=failures,
    )
    logger.info(
        "Pruned %d file(s), %d byte(s) from the data directory (%d failure(s))",
        report.items_removed,
        report.bytes_freed,
        len(report.failures),
    )
    return report
