"""Job endpoints: create, list, fetch, and cancel separation jobs.

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

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request

from straticate.api.audio import SettingsDep, StoreDep
from straticate.api.models import CatalogDep
from straticate.errors import ApplicationError
from straticate.inference import SeparatorJobExecutor, SeparatorRegistry
from straticate.jobs import JobManager, get_job_manager
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
