"""Application factory and ASGI entry point.

Run the development server with::

    uv run uvicorn straticate.main:app --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from straticate import __version__
from straticate.api import audio, system
from straticate.audio import AudioStore
from straticate.config import Settings, get_settings
from straticate.errors import register_error_handlers
from straticate.logging import configure_logging

API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and wire the FastAPI application.

    Args:
        settings: Optional explicit settings (used by tests); defaults to the
            process-wide settings loaded from the environment.

    Returns:
        A fully configured :class:`FastAPI` instance with logging, CORS,
        routers, and error handlers installed.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="Straticate", version=__version__)
    app.state.settings = settings
    app.state.audio_store = AudioStore(settings.data_dir)

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

    return app


app = create_app()
