"""Application factory and ASGI entry point.

Run the development server with::

    uv run uvicorn straticate.main:app --port 8000
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from straticate import __version__
from straticate.api import audio, jobs, results, system, ws
from straticate.api import models as models_api
from straticate.audio import AudioStore
from straticate.config import Settings, get_settings
from straticate.errors import register_error_handlers
from straticate.inference import SeparatorRegistry
from straticate.jobs import EventHub, JobManager
from straticate.logging import configure_logging
from straticate.models import ModelCatalog
from straticate.system import DeviceDetector
from straticate.telemetry import TelemetrySampler

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Run the job manager, event hub and telemetry sampler for the app's lifetime.

    A **fresh** :class:`JobManager`, :class:`EventHub` and
    :class:`TelemetrySampler` are created per lifespan cycle (none can be
    restarted once closed, and an app object may go through several lifespans,
    e.g. under repeated ``TestClient`` usage). They are stored on
    ``app.state.job_manager`` / ``app.state.event_hub`` /
    ``app.state.telemetry_sampler`` (retrieved in endpoints via
    :func:`straticate.jobs.get_job_manager`,
    :func:`straticate.jobs.get_event_hub` and
    :func:`straticate.telemetry.get_telemetry_sampler`).

    Two listeners are registered with the manager, in this order: the hub's
    :meth:`~straticate.jobs.EventHub.publish`, so every job event is broadcast
    to connected browsers, and the sampler's
    :meth:`~straticate.telemetry.TelemetrySampler.on_job_event`, which starts
    telemetry sampling when a job starts and stops it at its terminal event.
    Registration order matters for the wire: a terminal event is handed to the
    hub before the sampler is asked to stop, so nothing can be interleaved
    between them.

    Shutdown order is **sampler → manager → hub**:

    - the sampler first, so no telemetry sample can be published into a hub
      that is about to tear its connections down;
    - the manager second, so that it drains its event queue (including the
      cancellation of a job that was still running) into the hub — the hub's
      listener therefore stays registered until that drain finishes;
    - the hub last, in a ``finally``, so it is closed (and its sender tasks
      released, its sockets shut) even if closing the sampler or the manager
      raises.
    """
    manager = JobManager()
    hub = EventHub()
    sampler = TelemetrySampler(hub)
    app.state.job_manager = manager
    app.state.event_hub = hub
    app.state.telemetry_sampler = sampler
    manager.add_listener(hub.publish)
    manager.add_listener(sampler.on_job_event)
    manager.start()
    try:
        yield
    finally:
        try:
            try:
                await sampler.aclose()
            finally:
                await manager.aclose()
        finally:
            manager.remove_listener(sampler.on_job_event)
            manager.remove_listener(hub.publish)
            await hub.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and wire the FastAPI application.

    Args:
        settings: Optional explicit settings (used by tests); defaults to the
            process-wide settings loaded from the environment.

    Returns:
        A fully configured :class:`FastAPI` instance with logging, CORS,
        routers, and error handlers installed.

    Compute devices are detected here rather than in the lifespan: they cannot
    change during a run, and detection never raises (a failing probe only logs
    a warning), so it cannot break startup. The separator registry is built
    here for the same reason — it holds no per-run state, only the
    architecture → builder map and the separator instances it lazily creates.

    Raises:
        ModelCatalogError: If ``settings.models_dir`` holds no valid model
            catalog. The application deliberately refuses to start rather than
            serve an empty set of separation choices.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Straticate", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.audio_store = AudioStore(settings.data_dir)
    app.state.model_catalog = ModelCatalog.from_directory(settings.models_dir)

    device_detector = DeviceDetector()
    device_detector.refresh()
    app.state.device_detector = device_detector

    app.state.separator_registry = SeparatorRegistry()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(audio.router, prefix=API_PREFIX)
    app.include_router(models_api.router, prefix=API_PREFIX)
    app.include_router(jobs.router, prefix=API_PREFIX)
    app.include_router(results.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)

    return app


app = create_app()
