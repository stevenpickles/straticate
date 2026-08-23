"""Application settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_models_dir() -> Path:
    """Resolve the repository's ``models/`` directory (cwd-independent).

    This file lives at ``backend/src/straticate/config.py``, so the repository
    root is three parents up. Installed non-editably (outside a checkout) the
    directory will not exist, and ``STRATICATE_MODELS_DIR`` must point at the
    catalog explicitly.
    """
    return Path(__file__).resolve().parents[3] / "models"


class Settings(BaseSettings):
    """Runtime configuration for the Straticate backend.

    Every field can be overridden via an environment variable with the
    ``STRATICATE_`` prefix, e.g. ``STRATICATE_PORT=9000``.
    """

    model_config = SettingsConfigDict(env_prefix="STRATICATE_")

    host: str = "127.0.0.1"
    """Interface the server binds to."""

    port: int = 8000
    """Port the server listens on."""

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

    max_upload_bytes: int = 1024**3
    """Maximum accepted audio upload size in bytes (default 1 GiB)."""

    log_level: str = "INFO"
    """Root log level name (e.g. ``DEBUG``, ``INFO``, ``WARNING``)."""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance (cached)."""
    return Settings()
