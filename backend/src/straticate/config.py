"""Application settings loaded from the environment."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    """Directory for application data (models, job artifacts, etc.)."""

    log_level: str = "INFO"
    """Root log level name (e.g. ``DEBUG``, ``INFO``, ``WARNING``)."""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` instance (cached)."""
    return Settings()
