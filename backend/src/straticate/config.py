"""Application settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS


def _default_models_dir() -> Path:
    """Resolve the repository's ``models/`` directory (cwd-independent).

    This file lives at ``backend/src/straticate/config.py``, so the repository
    root is three parents up. Installed non-editably (outside a checkout) the
    directory will not exist, and ``STRATICATE_MODELS_DIR`` must point at the
    catalog explicitly.
    """
    return Path(__file__).resolve().parents[3] / "models"


def _default_frontend_dist_dir() -> Path:
    """Resolve the repository's ``frontend/dist`` directory (cwd-independent).

    The same reasoning as :func:`_default_models_dir`, and for the same reason:
    a relative ``frontend/dist`` would resolve against whatever directory the
    server happened to be started from, so the app would appear or vanish
    depending on where you typed ``uvicorn``. Installed non-editably the
    directory will not exist, and ``STRATICATE_FRONTEND_DIST_DIR`` must point at
    the bundle explicitly.
    """
    return Path(__file__).resolve().parents[3] / "frontend" / "dist"


def _default_cors_origins() -> list[str]:
    """Origins the Vite dev server is reachable on (see DEVELOPMENT.md)."""
    return ["http://localhost:5173", "http://127.0.0.1:5173"]


class Settings(BaseSettings):
    """Runtime configuration for the Straticate backend.

    Every field can be overridden via an environment variable with the
    ``STRATICATE_`` prefix, e.g. ``STRATICATE_PORT=9000``. List-valued fields
    take a JSON array, e.g.
    ``STRATICATE_CORS_ORIGINS='["https://studio.example"]'``.

    Every field here is **consumed**: ``host`` and ``port`` by
    :func:`straticate.main.serve`, ``cors_origins`` and ``log_level`` by the
    application factory, ``data_dir`` by the audio store, the job output layout
    and the export cache, ``models_dir`` and ``include_development_models`` by
    the model catalog, ``frontend_dist_dir`` by
    :func:`straticate.frontend.mount_frontend`, ``max_upload_bytes`` by the
    upload route, and
    ``ffmpeg_timeout_seconds`` by :func:`straticate.audio.ffmpeg.run_ffmpeg`. A
    setting nothing reads is a documented promise the application does not
    keep, so it does not belong here.
    """

    model_config = SettingsConfigDict(env_prefix="STRATICATE_")

    host: str = "127.0.0.1"
    """Interface the server binds to (consumed by :func:`straticate.main.serve`)."""

    port: int = 8000
    """Port the server listens on (consumed by :func:`straticate.main.serve`)."""

    cors_origins: list[str] = Field(default_factory=_default_cors_origins)
    """Browser origins allowed to call the API cross-origin.

    Defaults to the Vite dev server's two loopback spellings. The dev server
    proxies ``/api`` to the backend, so in normal development the browser sees
    a same-origin request and this list is never consulted; it matters when a
    page talks to ``:8000`` directly. Override with ``STRATICATE_CORS_ORIGINS``
    (a JSON array).
    """

    data_dir: Path = Path("data")
    """Directory for application data (uploads, models, job artifacts, etc.).

    Relative paths resolve against the process working directory.
    Subdirectories are created lazily by their consumers.
    """

    models_dir: Path = Field(default_factory=_default_models_dir)
    """Directory holding the model catalog (``catalog.json``) and its schemas.

    Defaults to the repository's ``models/`` directory, resolved from this
    module's location so the server can be started from any working directory.
    Override with ``STRATICATE_MODELS_DIR``.
    """

    frontend_dist_dir: Path = Field(default_factory=_default_frontend_dist_dir)
    """Directory holding the built frontend bundle (``index.html`` and assets).

    Defaults to the repository's ``frontend/dist``, resolved from this module's
    location so the server serves the same app from any working directory
    (feature 042). Override with ``STRATICATE_FRONTEND_DIST_DIR`` — a packaged
    installation, or a bundle built somewhere other than the checkout, needs
    nothing else.

    **Its absence is a normal state.** A checkout that has not run
    ``npm run build`` has no such directory; the application starts and serves
    the whole API regardless, and only the root URL differs (it explains what
    to build). See :mod:`straticate.frontend`.
    """

    include_development_models: bool = False
    """Whether development fixtures appear in the user-facing model catalog.

    **Environment variable:** ``STRATICATE_INCLUDE_DEVELOPMENT_MODELS``
    (``1``/``true``/``yes`` to enable). Default ``False``.

    A catalog entry whose manifest declares ``development_only: true`` exists to
    exercise the application, not to separate audio — today that is the fake
    separator of ARCHITECTURE.md §8, a comb filter whose output is audibly wrong
    by design. Left visible it is offered as an ordinary quality tier and, since
    feature 011's UI preselects a mode's first option, is what an untouched
    "Start separation" runs. So it is hidden by default: the catalog is filtered
    once, at load, and every surface downstream of it (``GET /models``,
    ``GET /models/{model_id}``, ``GET /separation-modes``, ``POST /jobs``,
    install and removal) sees the same catalog and behaves as though the entry
    were not there.

    Turn it **on** wherever the fixtures are the point:

    - the backend test suite (``backend/tests/conftest.py`` sets the variable
      for the whole session);
    - CI jobs that run the application without downloading real weights;
    - the Playwright end-to-end tier (feature 030), which drives the real UI
      against the fake separator and therefore needs
      ``STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1`` in the environment of the
      backend process it launches.

    With it on, the catalog is served exactly as written — the behaviour every
    release up to feature 031 had.
    """

    max_upload_bytes: int = 1024**3
    """Maximum accepted audio upload size in bytes (default 1 GiB)."""

    ffmpeg_timeout_seconds: float = Field(default=DEFAULT_FFMPEG_TIMEOUT_SECONDS, gt=0)
    """Wall-clock ceiling for a single FFmpeg or ffprobe invocation.

    Every subprocess this application starts runs in a worker thread of
    asyncio's shared default executor, so a wedged FFmpeg does not merely stall
    one request: it holds a thread that audio probing, decoding and exporting
    all draw from. The bound turns "hangs forever" into a documented,
    per-surface timeout error (see :mod:`straticate.audio.ffmpeg`).

    It reaches the subprocess by being **passed down** from the application
    that was built with these settings — the upload route and the export route
    take it from ``app.state.settings``, and the separator is constructed with
    it — so ``create_app(Settings(ffmpeg_timeout_seconds=30))`` really governs
    the bound, not only the environment variable.
    """

    log_level: str = "INFO"
    """Root log level name (e.g. ``DEBUG``, ``INFO``, ``WARNING``).

    Applied by :func:`straticate.main.serve`, which owns process-global logging
    configuration; importing or building the application never touches it.
    """


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance (cached)."""
    return Settings()
