"""Application factory and ASGI entry point.

Run the development server with::

    uv run uvicorn straticate.main:app --port 8000
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from straticate import __version__
from straticate.api import audio, system, ws
from straticate.api import models as models_api
from straticate.audio import AudioStore
from straticate.config import Settings, get_settings
from straticate.errors import register_error_handlers
from straticate.jobs import EventHub, JobManager
from straticate.logging import configure_logging
from straticate.models import ModelCatalog
from straticate.system import DeviceDetector

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Run a job manager and a WebSocket event hub for the application's lifetime.

    A **fresh** :class:`JobManager` and :class:`EventHub` are created per
    lifespan cycle (neither can be restarted once closed, and an app object may
    go through several lifespans, e.g. under repeated ``TestClient`` usage).
    They are stored on ``app.state.job_manager`` / ``app.state.event_hub``
    (retrieved in endpoints via :func:`straticate.jobs.get_job_manager` and
    :func:`straticate.jobs.get_event_hub`).

    The hub's :meth:`~straticate.jobs.EventHub.publish` is registered as a job
    manager listener, so every job event is broadcast to connected browsers.
    Shutdown order matters: the manager is closed first so that it drains its
    event queue (including the cancellation of a job that was still running)
    into the hub — the listener therefore stays registered until that drain
    finishes — and only then are the connections closed (after the hub has
    flushed what it buffered). The hub is torn down even if closing the manager
    fails, so a failure there cannot leak sender tasks or leave sockets open.
    """
    manager = JobManager()
    hub = EventHub()
    app.state.job_manager = manager
    app.state.event_hub = hub
    manager.add_listener(hub.publish)
    manager.start()
    try:
        yield
    finally:
        try:
            await manager.aclose()
        finally:
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
    a warning), so it cannot break startup.

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
    app.include_router(ws.router, prefix=API_PREFIX)

    return app


app = create_app()
