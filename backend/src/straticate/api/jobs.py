"""Job endpoints: create, list, fetch, cancel, and delete separation jobs.

This router is the join between the two halves of the application. Creating a
job resolves the uploaded audio (feature 006), the model behind the requested
mode + quality tier (010), the compute device (018) and a
:class:`~straticate.inference.base.Separator` for that model (014), wraps them
in a :class:`~straticate.inference.executor.SeparatorJobExecutor` and hands it
to the :class:`~straticate.jobs.JobManager` (012). The request then returns
**immediately** with the ``queued`` job — no inference ever runs inside a
request handler (AGENTS.md principle 4) — while progress flows out through the
event hub (013) to connected clients (016).

Two rules the handlers here obey:

- **Every handler is ``async def``.** A sync handler would run in Starlette's
  threadpool, off the job manager's event loop, and the manager's mutating API
  is single-loop by contract (feature 012).
- **Nothing in this module names a model architecture, a stem, a mode or a
  device type.** All of it comes from the catalog and the device detector
  (AGENTS.md principles 1 and 6).
"""

import asyncio
import shutil
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from straticate.api.audio import SettingsDep, StoreDep
from straticate.api.models import CatalogDep
from straticate.errors import ApplicationError
from straticate.inference import SeparatorJobExecutor, SeparatorRegistry
from straticate.jobs import JobManager, get_job_manager
from straticate.jobs.layout import job_output_dir, job_record_path
from straticate.jobs.resolution import resolve_audio, resolve_device, resolve_model
from straticate.schemas import Job, SeparationConfiguration
from straticate.system import DeviceDetectorDep
from straticate.telemetry import TelemetrySampler, get_telemetry_sampler

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_separator_registry(request: Request) -> SeparatorRegistry:
    """Dependency accessor for the application's :class:`SeparatorRegistry`.

    The registry is built in :func:`straticate.main.create_app` and lives on
    ``app.state.separator_registry``; tests replace it there to inject
    zero-delay separators.
    """
    return cast(SeparatorRegistry, request.app.state.separator_registry)


RegistryDep = Annotated[SeparatorRegistry, Depends(get_separator_registry)]
ManagerDep = Annotated[JobManager, Depends(get_job_manager)]
SamplerDep = Annotated[TelemetrySampler, Depends(get_telemetry_sampler)]


def _shutting_down(exc: RuntimeError) -> ApplicationError:
    """Translate a closed job manager into a 503 instead of a leaked 500.

    :meth:`~straticate.jobs.JobManager.submit` and
    :meth:`~straticate.jobs.JobManager.cancel` raise ``RuntimeError`` once the
    manager has been closed — that is, while the application is shutting down.
    It is an expected condition for a request that arrives at that moment, not
    an internal error.
    """
    return ApplicationError(
        "service_unavailable",
        "The job manager is shutting down and is not accepting requests.",
        status_code=503,
        detail={"reason": str(exc)},
    )


@router.post("", status_code=201)
async def create_job(
    configuration: SeparationConfiguration,
    store: StoreDep,
    settings: SettingsDep,
    catalog: CatalogDep,
    detector: DeviceDetectorDep,
    registry: RegistryDep,
    manager: ManagerDep,
    sampler: SamplerDep,
) -> Job:
    """Create a separation job and return it immediately in state ``queued``.

    References are resolved in the order audio → model → device → separator, so
    the first thing that cannot be resolved is what the client is told about.
    The device is resolved *with* the model, because ``Model.capabilities`` says
    which compute backends the weights run on and a pairing that cannot work is
    better refused here than discovered mid-job (see
    :func:`~straticate.jobs.resolution.resolve_device`). The configuration
    recorded on the job is a copy with ``device_id`` set to the **resolved**
    device, so ``Job.configuration.device_id`` is always populated — in
    responses and in every event — even when the request omitted it.

    **Building the separator is awaited, not called.** A real inference backend
    reads hundreds of megabytes of weights on a cache miss;
    :meth:`~straticate.inference.registry.SeparatorRegistry.aget` runs that in a
    worker thread so the event loop keeps serving requests, dispatching job
    events and pushing WebSocket frames while it happens. The ``await`` sits
    *before* ``submit`` deliberately: what must stay atomic is submit → sampler
    registration, not resolution → submit.

    The separator is handed to the telemetry sampler (feature 019) **directly
    after** ``submit`` returns, with no ``await`` in between: the job ID does
    not exist until then, and any suspension point between the two would let
    the manager's worker start the job — and emit ``job_started`` — before the
    sampler knew which separator to poll.

    Errors (see ``docs/contracts/rest-api.md``): ``audio_not_found`` (404),
    ``separation_mode_not_found`` (404), ``quality_option_not_found`` (404),
    ``device_not_found`` (404), ``model_device_unsupported`` (409),
    ``model_weights_missing`` (409), ``separator_unavailable`` (501),
    ``service_unavailable`` (503) — and, because building the separator happens
    here, the two deployment faults that build can report:
    ``model_weights_invalid`` (500) and ``model_parameters_invalid`` (500).
    """
    _record, input_path = resolve_audio(store, configuration.audio_id)
    model = resolve_model(catalog, configuration.mode_id, configuration.quality_id)
    device = resolve_device(detector, configuration.device_id, model=model)
    separator = await registry.aget(model)
    executor = SeparatorJobExecutor(
        separator,
        input_path=input_path,
        data_dir=settings.data_dir,
    )
    resolved = configuration.model_copy(update={"device_id": device.id})
    try:
        job = manager.submit(resolved, executor, model_id=model.id)
    except RuntimeError as exc:
        raise _shutting_down(exc) from exc
    sampler.register(job.id, executor.separator)
    return job


@router.get("")
async def list_jobs(manager: ManagerDep) -> list[Job]:
    """List every known job in **submission order** (oldest first).

    Job records survive a restart (feature 057): the list a restarted server
    returns holds the jobs of previous runs too, in the same order, with the
    one repair described on ``GET /jobs/{job_id}``.
    """
    return manager.list_jobs()


@router.get("/{job_id}")
async def get_job(job_id: str, manager: ManagerDep) -> Job:
    """Fetch one job — the source of truth for reconnect and refresh.

    **A job outlives the process that ran it.** A completed job restarts
    identical, result and all, so its stems and exports stay reachable. A job
    that was ``queued`` or still running when the server stopped comes back
    ``failed`` with ``error.code == "job_interrupted"``: it is never silently
    re-run (the work nobody asked for twice) and never reported ``cancelled``
    (nobody cancelled it). See ``docs/contracts/rest-api.md``.

    Errors: ``job_not_found`` (404).
    """
    return manager.get(job_id)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: str, manager: ManagerDep) -> Job:
    """Request cooperative cancellation of a job; returns the job snapshot.

    Cancellation is a *request*, not a stop: a ``queued`` job is cancelled
    immediately, while a running one is asked to stop at its next cooperative
    checkpoint — so the returned job may still be in a processing state and the
    authoritative transition arrives as a ``job_cancelled`` event. Cancelling a
    job that already reached a terminal state is a no-op and still returns 200.

    Errors: ``job_not_found`` (404), ``service_unavailable`` (503).
    """
    try:
        return manager.cancel(job_id)
    except RuntimeError as exc:
        raise _shutting_down(exc) from exc


@router.delete("/{job_id}", status_code=204)
async def delete_job(job_id: str, manager: ManagerDep, settings: SettingsDep) -> None:
    """Delete a terminal job wholesale: its record, stems and exports together.

    This answers A1's worst case (see ``docs/features/058-job-deletion.md``):
    before this endpoint existed, nothing could remove the stems and exports a
    completed job produced — only the audio *upload* it was separated from
    could be deleted, leaving derived output as orphaned disk usage forever.
    ``manager.remove()`` drops the in-memory (and, since it is terminal, only)
    entry first — refusing a non-terminal job before anything on disk is
    touched. Because :func:`straticate.jobs.layout.job_output_dir` is the one
    place a job's directory is built, and
    :func:`~straticate.jobs.layout.job_exports_dir` (feature 058) is the one
    place exports live inside it, removing that whole directory reaches the
    record, every stem and every built export — there is no second
    path-building function anywhere that could still be pointing at a
    survivor (barring the export race described below).

    **The record is unlinked first, synchronously, and its failure aborts the
    delete.** :mod:`straticate.jobs.store`'s module docstring invariant is
    that a job directory is self-describing: it must never hold a record that
    names stems which are gone, nor stems that no record mentions. A bare
    ``shutil.rmtree(..., ignore_errors=True)`` over the whole directory can
    violate the second half of that: it may remove every stem while failing
    silently on a locked ``job.json``, leaving a ``completed`` record on disk
    whose stems a restart would then serve as 404s. Unlinking
    :func:`~straticate.jobs.layout.job_record_path` *before* the ``rmtree``
    makes the record the thing whose removal is unconditional: if it cannot be
    removed, ``manager.remove()``'s entry is re-seeded via
    ``manager.restore()`` (safe — the popped job is terminal, which is the one
    state ``restore()`` accepts) and the ``OSError`` is re-raised, so the
    client gets an honest 500 and the job is left exactly as it was — served,
    listed, and untouched on disk — rather than half-deleted. This all happens
    before the first ``await`` below, so no concurrent request can ever
    observe the job as briefly gone.

    Once the record is gone, ``shutil.rmtree(..., ignore_errors=True)`` best-
    effort removes whatever is left (stems, exports, the now-empty directory)
    in a worker thread — a 1.17 GB job directory measured a 175 ms event-loop
    stall run inline, which every other connected client would feel. Offloading
    it to :func:`asyncio.to_thread` is safe *because* ``manager.remove()``
    already popped the entry synchronously, before this or any other await: no
    other request can submit, cancel or re-delete this job id while the thread
    runs, so nothing races the removal except, as ever, a download already
    holding a file open.

    A restarted server never resurrects a deleted job: the record died first,
    synchronously, and startup only lists what :mod:`straticate.jobs.store`
    finds on disk.

    **Best-effort on Windows** for everything past the record. A stem or
    export file this job's own ``export`` route just streamed out via
    :class:`~fastapi.responses.FileResponse` can still be open when this
    handler runs, and Windows refuses to unlink an open file (POSIX does not:
    an unlink there detaches the directory entry regardless of open handles).
    ``ignore_errors=True`` tolerates that rather than turning a real deletion
    into a 500: the job is gone from the API — ``GET`` on it is
    ``job_not_found`` from the record's removal on — and whatever a locked
    handle left behind is debris a later pruning feature (060) is responsible
    for sweeping, not a defect of this endpoint. Every other supported
    platform removes the directory in full.

    **Known limitation: an export build racing this delete can leave an empty
    orphan directory.** ``build_artifact`` (feature 022) calls
    ``artifact.parent.mkdir(parents=True, exist_ok=True)`` before it writes; if
    that runs after this handler's ``rmtree`` has already removed the job
    directory, it recreates an empty ``exports/`` (and job) directory that
    nothing then populates, because the stems it needs to transcode are gone.
    This is not a resurrection — the record is already gone by the time the
    ``rmtree`` even starts, so the job stays deleted from every endpoint's
    point of view — it is the same debris category as a locked file, left for
    060 to prune, and the racing export answers its own request with
    ``export_failed`` rather than serving something stale.

    Errors: ``job_not_found`` (404) for an unknown job, ``job_active`` (409,
    with the job's current ``state`` in ``detail``) for a job that has not
    reached a terminal state — cancel it and wait for the terminal event
    before deleting; deleting underneath a running executor is exactly the
    corruption this endpoint exists to prevent, not a case it introduces.
    """
    job = manager.remove(job_id)
    try:
        job_record_path(settings.data_dir, job.id).unlink(missing_ok=True)
    except OSError:
        # The entry is already popped, and it is terminal (remove() only ever
        # succeeds for a terminal job), so restore() re-seeds it cleanly. The
        # client sees the honest failure instead of a job whose record is gone
        # but whose entry, stems and exports are not.
        manager.restore([job])
        raise
    # manager.remove() popped the entry synchronously above, before this first
    # await, so no concurrent request can submit, cancel or re-delete this job
    # id while the thread below runs — offloading the (possibly large) tree
    # removal is safe.
    await asyncio.to_thread(
        shutil.rmtree, job_output_dir(settings.data_dir, job.id), ignore_errors=True
    )
