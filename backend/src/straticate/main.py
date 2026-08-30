"""Application factory and ASGI entry points.

**One process serves both halves of the product.** The API lives under
:data:`API_PREFIX`; everything else is the built frontend bundle, mounted by
:func:`straticate.frontend.mount_frontend` (feature 042), so running Straticate
is one command on one URL. A checkout with no bundle serves the API exactly as
before — see that module for both the ordering rule that keeps the fallback off
``/api/**`` and the no-bundle path. Development is untouched: Vite still serves
the app on ``:5173`` and proxies ``/api`` here.

Two ways in, and they differ in who owns the *bind address*:

- ``uv run uvicorn straticate.main:app --reload --port 8000`` (development)
  serves the module-level :data:`app`. The command line owns host and port, so
  ``Settings.host``/``Settings.port`` are not consulted.
- ``uv run python -m straticate`` (or :func:`serve`) takes host and port from
  :class:`~straticate.config.Settings`, so ``STRATICATE_PORT=9000 uv run python
  -m straticate`` really does what the settings docstring promises.

**Application logging is configured on both paths**, in the lifespan (see
:func:`lifespan`). Uvicorn's ``LOGGING_CONFIG`` declares handlers only for its
own ``uvicorn``/``uvicorn.error``/``uvicorn.access`` loggers and never touches
the root logger, so leaving it to uvicorn would drop every ``straticate.*``
record onto ``logging.lastResort`` — WARNING and above only, bare message, no
timestamp, no logger name, and ``STRATICATE_LOG_LEVEL=DEBUG`` silently doing
nothing.

**Everything that logs at startup therefore runs after that**, the compute
device probe included: it is warmed in the lifespan rather than in
:func:`create_app`, whose module-level call runs at import — earlier than
either entry path configures anything.

Neither path changes what the application *is*: :func:`create_app` builds the
same object either way, and **building** it has no process-global side effects
— configuration happens at startup, once per running application, not once per
import.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from straticate import __version__
from straticate.api import audio, export, jobs, results, system, ws
from straticate.api import models as models_api
from straticate.audio import AudioStore
from straticate.config import Settings, get_settings
from straticate.errors import ErrorEnvelopeMiddleware, register_error_handlers
from straticate.frontend import log_bundle_state, mount_frontend
from straticate.inference import SeparatorRegistry, default_separator_builders
from straticate.jobs import EventHub, JobManager, JobStore
from straticate.logging import configure_logging, ensure_logging_configured
from straticate.models import ModelCatalog, ModelInstaller
from straticate.system import DeviceDetector
from straticate.telemetry import TelemetrySampler

API_PREFIX = "/api/v1"

CORS_WILDCARD = "*"
"""``allow_origins`` value meaning "any origin" (Starlette's own spelling)."""

CORS_EXPOSED_HEADERS = [
    "Accept-Ranges",
    "Content-Disposition",
    "Content-Range",
    "ETag",
    "Last-Modified",
]
"""Response headers cross-origin JavaScript is allowed to read.

The CORS default exposes only the handful of "simple" response headers, which
does not include any of these — so without this list a cross-origin fetch of a
stem or an export can *receive* the bytes and still be unable to see the
``Content-Range`` telling it which bytes it got, the ``ETag``/``Last-Modified``
it needs to make ``If-Range`` work, or the ``Content-Disposition`` naming the
download. All five are part of the documented stem/export responses
(``docs/contracts/rest-api.md``), so all five are exposed.

Inert in normal development, where Vite proxies ``/api`` and the browser sees
same-origin requests; it matters the moment a page fetches ``:8000`` directly,
which is exactly what the Web Audio stem player does when it is not behind the
dev proxy.
"""


def allows_credentials(origins: list[str]) -> bool:
    """Whether credentialed cross-origin requests may be allowed for ``origins``.

    ``False`` exactly when the allowlist contains :data:`CORS_WILDCARD`, and the
    reason is the interaction of two Starlette behaviours. ``"*"`` means
    allow-all; and with ``allow_credentials=True`` Starlette stops sending the
    literal ``Access-Control-Allow-Origin: *`` and instead **echoes the caller's
    own ``Origin``** alongside ``Access-Control-Allow-Credentials: true``. The
    combination is the one CORS explicitly forbids for good reason: every origin
    on the internet could read credentialed responses, which the previously
    hardcoded two-entry allowlist made impossible.

    Now that the list is configurable — and documented as taking a JSON array,
    which invites ``'["*"]'`` — the credential flag follows from it rather than
    staying hardcoded. A wildcard therefore degrades to the safe, standard
    behaviour (``Access-Control-Allow-Origin: *``, no credentials) instead of
    silently becoming allow-any-origin-with-credentials. Naming origins
    explicitly keeps credentials enabled.

    There is no authentication in Straticate today (ARCHITECTURE.md §14), so
    nothing rides on cookies yet; this is about not leaving a trap for the
    feature that introduces one.
    """
    return CORS_WILDCARD not in origins


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Configure logging, then run the job manager, hub and sampler for the app's lifetime.

    **Logging is configured here**, at startup, because this is the earliest
    point that runs once per *running application* on every entry path —
    including ``uvicorn straticate.main:app``, which DEVELOPMENT.md documents
    as the primary way to run the backend and which configures only uvicorn's
    own loggers, never the root one. Doing it in :func:`create_app` instead
    would fire on every import and every test fixture, which is the global side
    effect this arrangement exists to avoid.

    The call is :func:`~straticate.logging.ensure_logging_configured`, which is
    deliberately **non-destructive**: it configures the root logger only when
    nothing else has, so an embedding process (or pytest, whose ``caplog``
    handler is attached to the root logger) keeps the configuration it chose.
    :func:`serve` still configures logging authoritatively before the server
    starts, and this call then finds the work already done.

    **Which frontend mode the server started in is logged here too**, for the
    same reason and immediately after the device probe: it is the one line that
    tells a user whether this process is serving the app or only the API, and
    written from :func:`create_app` it would be written at import, before
    logging exists.

    **Compute devices are probed immediately afterwards**, and the order is the
    whole point. :meth:`~straticate.system.DeviceDetector.refresh` is where
    "PyTorch is not installed" (DEBUG) and "Could not determine total system
    memory" (WARNING, with a traceback) are emitted, and probing from
    :func:`create_app` meant those records were written at *import* — before
    either entry path had configured anything, so they fell through to
    ``logging.lastResort``: the debug line dropped even under
    ``STRATICATE_LOG_LEVEL=DEBUG``, the warning printed bare with no timestamp,
    level or logger name. Startup is the earliest point that runs once per
    running application *after* logging exists, so it is where the probe
    belongs; devices still cannot change during a run, and detection still
    never raises (a failing probe only logs a warning), so this cannot break
    startup either. The detector object itself is still built in
    :func:`create_app`, so a test can substitute its probes before startup
    warms it.

    A **fresh** :class:`JobManager`, :class:`EventHub` and
    :class:`TelemetrySampler` are created per lifespan cycle (none can be
    restarted once closed, and an app object may go through several lifespans,
    e.g. under repeated ``TestClient`` usage). They are stored on
    ``app.state.job_manager`` / ``app.state.event_hub`` /
    ``app.state.telemetry_sampler`` (retrieved in endpoints via
    :func:`straticate.jobs.get_job_manager`,
    :func:`straticate.jobs.get_event_hub` and
    :func:`straticate.telemetry.get_telemetry_sampler`). The
    :class:`~straticate.jobs.store.JobStore` the manager persists through is
    created here too and left on ``app.state.job_store``, where a feature that
    removes a job's files can reach the same record paths the manager writes.
    It is ``None`` only when a bare ``FastAPI()`` drives this lifespan with no
    settings, which is how the wiring tests isolate it — and a manager without
    a store behaves exactly as it did before feature 057.

    **The job records of previous runs are loaded here** (feature 057), from
    ``{data_dir}/jobs/*/job.json`` via
    :meth:`~straticate.jobs.store.JobStore.recover`, and handed to
    :meth:`~straticate.jobs.JobManager.restore` before the worker starts. Three
    properties of that step are deliberate:

    - **Startup is the only place it can happen.** The manager is created per
      lifespan cycle, so this is the one moment at which a fresh manager exists
      and no job of this run has been submitted — which is what keeps
      ``GET /jobs`` in ULID order across the join.
    - **It emits no events**, and the listeners above are already registered
      when it runs, so that is a property of ``restore`` rather than of the
      ordering here. There is no WebSocket client at startup to receive them,
      and a replayed ``job_completed`` would claim on a live channel that a job
      from a previous run had just finished.
    - **It never fails startup.** A record that cannot be read is skipped with
      a warning (see :mod:`straticate.jobs.store`), and a job left ``queued``
      or running by a stopped process comes back ``failed`` with
      ``job_interrupted`` rather than being re-queued.

    The scan is synchronous, like the catalog read in :func:`create_app`: it is
    a few hundred bytes per job, once, before the application serves anything.

    Two listeners are registered with the manager, in this order: the hub's
    :meth:`~straticate.jobs.EventHub.publish`, so every job event is broadcast
    to connected browsers, and the sampler's
    :meth:`~straticate.telemetry.TelemetrySampler.on_job_event`, which starts
    telemetry sampling when a job starts and stops it at its terminal event.
    Registration order matters for the wire: a terminal event is handed to the
    hub before the sampler is asked to stop, so nothing can be interleaved
    between them.

    The :class:`~straticate.models.ModelInstaller` is built in
    :func:`create_app` (it holds no per-run state beyond the installs currently
    running) but **closed here**, last of all: a download in flight at shutdown
    is cancelled and unlinks its own ``.part``, so a restart never finds a
    half-written weights file.

    Shutdown order is **sampler → manager → hub → installer**:

    - the sampler first, so no telemetry sample can be published into a hub
      that is about to tear its connections down;
    - the manager second, so that it drains its event queue (including the
      cancellation of a job that was still running) into the hub — the hub's
      listener therefore stays registered until that drain finishes;
    - the hub last, in a ``finally``, so it is closed (and its sender tasks
      released, its sockets shut) even if closing the sampler or the manager
      raises.
    """
    # ``settings`` is absent only when a bare ``FastAPI()`` drives this
    # lifespan directly, which is how the job/hub/sampler wiring tests isolate
    # it. Nothing built by :func:`create_app` can reach here without it, so the
    # entry paths that matter always configure logging — and no global read is
    # smuggled in as a fallback.
    settings = cast(Settings | None, getattr(app.state, "settings", None))
    if settings is not None:
        ensure_logging_configured(settings.log_level)
    detector = cast(DeviceDetector | None, getattr(app.state, "device_detector", None))
    if detector is not None:
        detector.refresh()
    # Feature 056: restore audio records from a previous run's sidecars — see
    # AudioStore.load's docstring for why this runs here and not in __init__.
    audio_store = cast(AudioStore | None, getattr(app.state, "audio_store", None))
    if audio_store is not None:
        audio_store.load()
    log_bundle_state(app)
    installer = cast(ModelInstaller | None, getattr(app.state, "model_installer", None))
    store = JobStore(settings.data_dir) if settings is not None else None
    manager = JobManager(store=store)
    hub = EventHub()
    sampler = TelemetrySampler(hub)
    app.state.job_manager = manager
    app.state.event_hub = hub
    app.state.telemetry_sampler = sampler
    app.state.job_store = store
    manager.add_listener(hub.publish)
    manager.add_listener(sampler.on_job_event)
    if store is not None:
        manager.restore(store.recover())
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
            try:
                await hub.aclose()
            finally:
                if installer is not None:
                    await installer.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and wire the FastAPI application.

    Args:
        settings: Optional explicit settings (used by tests); defaults to the
            process-wide settings loaded from the environment.

    Returns:
        A fully configured :class:`FastAPI` instance with CORS, routers, error
        handlers, and the built frontend (when there is one) installed.

    **Building an application configures nothing process-global**, logging
    least of all. :func:`straticate.logging.configure_logging` calls
    ``logging.basicConfig(force=True)``, which replaces the root logger's
    handlers for the whole interpreter; doing that here meant every
    ``create_app()`` — one per test using the ``app`` fixture — tore down
    whatever the caller had installed, including pytest's ``caplog`` handler.
    Logging is configured once per *running* application, in :func:`lifespan`,
    which covers both entry paths without firing on import.

    The compute :class:`~straticate.system.DeviceDetector` is *constructed*
    here and **warmed in the lifespan**: probing writes log records, and
    ``create_app`` runs at import, before either entry path has configured
    logging (see :func:`lifespan`). Building the object here keeps
    ``app.state.device_detector`` available to substitute before startup. The
    separator registry is built here because it is silent — it holds no
    per-run state, only the
    architecture → builder map and the separator instances it lazily creates.
    It is built **from these settings**, so ``ffmpeg_timeout_seconds`` governs
    the separator's decode subprocesses and ``models_dir`` decides where a real
    backend reads its weights, on a per-application basis rather than being
    re-read from the environment deep in the call stack. It is also given the
    catalog's ``inference_parameters`` lookup, which is how a separator reaches
    its model's architecture-specific defaults — data the catalog keeps off the
    public model (ARCHITECTURE.md §9). The model
    installer is built here too, over the same catalog and ``models_dir``, and
    is closed in the lifespan so an install running at shutdown is cancelled
    rather than orphaned.

    The catalog is built with ``settings.include_development_models``, which is
    off by default: an application built from stock settings offers no
    development fixture on any surface, and
    ``STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1`` is what CI, the test suite and
    the end-to-end tier set to get them back (feature 032).

    **An empty catalog is not a startup failure; an invalid one is.** The
    refusal is about *unreadable* data — a missing file, malformed JSON, models
    of one mode disagreeing on stems — because serving a silently truncated set
    of separation choices would be worse than not starting. A catalog that is
    valid and simply offers this server nothing is a different situation, and
    since feature 032 it is a reachable one: a checkout whose every entry is a
    development fixture (which is what this repository shipped before feature
    026, and what any fork carrying only fixtures still has) loads cleanly with
    the default settings and serves empty ``/models`` and ``/separation-modes``
    lists. That is deliberate — the honest answer to "what can you separate?" is
    "nothing yet", and a client can render it — and it is pinned by
    ``tests/test_models_api.py``.

    Raises:
        ModelCatalogError: If ``settings.models_dir`` holds no *valid* model
            catalog — missing, unreadable, malformed, or internally
            inconsistent. Never merely because the catalog turned out to offer
            no models.
    """
    settings = settings or get_settings()

    app = FastAPI(title="Straticate", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.audio_store = AudioStore(settings.data_dir)
    catalog = ModelCatalog.from_directory(
        settings.models_dir, include_development=settings.include_development_models
    )
    app.state.model_catalog = catalog
    app.state.model_installer = ModelInstaller(catalog, settings.models_dir)

    app.state.device_detector = DeviceDetector()

    app.state.separator_registry = SeparatorRegistry(
        default_separator_builders(
            ffmpeg_timeout_seconds=settings.ffmpeg_timeout_seconds,
            models_dir=settings.models_dir,
            inference_parameters=catalog.inference_parameters,
        )
    )

    # Middleware order matters and reads backwards: ``add_middleware``
    # *prepends*, so the LAST one added is the OUTERMOST layer. CORS must be
    # outermost of the two, so that the envelope middleware's 500 response
    # travels back out through it and arrives with
    # ``Access-Control-Allow-Origin``. Reversing these two lines silently
    # restores the bug they exist to fix — hence the test in
    # ``tests/test_errors.py`` that sends an ``Origin`` at a route that raises.
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allows_credentials(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=CORS_EXPOSED_HEADERS,
    )

    register_error_handlers(app)
    app.include_router(system.router, prefix=API_PREFIX)
    app.include_router(audio.router, prefix=API_PREFIX)
    app.include_router(models_api.router, prefix=API_PREFIX)
    app.include_router(jobs.router, prefix=API_PREFIX)
    app.include_router(results.router, prefix=API_PREFIX)
    app.include_router(export.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)

    # The frontend is installed as the router's ``default``, not as a route,
    # which is what keeps it from shadowing anything: it is consulted only
    # after every route, every 405-producing partial match and every
    # ``redirect_slashes`` redirect, and it refuses ``/api/**`` (in any
    # spelling), every non-GET method and every non-HTTP scope on top of that.
    # See ``straticate.frontend`` and ``tests/test_frontend_mount.py``.
    app.state.frontend_dist_dir = settings.frontend_dist_dir
    app.state.frontend_index = mount_frontend(app, settings.frontend_dist_dir)

    return app


app = create_app()
"""The ASGI application ``uvicorn straticate.main:app`` serves.

**Deliberately a module-level instance, not a factory.** DEVELOPMENT.md, CI and
day-to-day development all name ``straticate.main:app``, and ``--factory``
would be a documented-interface change for no gain: now that
:func:`create_app` has no process-global side effects, building it at import
time costs a catalog read, writes no log record, and changes nothing outside
the returned object. Tests that want an isolated instance call
:func:`create_app` themselves (see ``tests/conftest.py``).
"""


def serve() -> None:
    """Run the application with uvicorn, bound and logging per :class:`Settings`.

    This is what makes ``STRATICATE_HOST``, ``STRATICATE_PORT`` and
    ``STRATICATE_LOG_LEVEL`` real: the settings are read here and applied, so
    ``STRATICATE_PORT=9000 uv run python -m straticate`` listens on 9000.

    Logging is configured **here** rather than in :func:`create_app` because
    this function owns the process, and ``logging.basicConfig(force=True)``
    is a process-global act (see :func:`create_app`).

    The already-built module-level :data:`app` is passed as an object rather
    than as the ``"straticate.main:app"`` import string: the string form makes
    uvicorn re-import this module in the worker, which would build a *second*
    application (a second model catalog load, a second device probe) and
    discard the first. Passing the object also means ``--reload``-style
    supervision is deliberately not offered here; development reload is
    uvicorn's own command line (see DEVELOPMENT.md).
    """
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)
