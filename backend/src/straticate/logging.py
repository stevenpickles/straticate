"""Logging configuration for the Straticate backend."""

import logging

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str) -> None:
    """Configure stdlib logging with a concise console format.

    Args:
        level: Log level name such as ``"DEBUG"`` or ``"INFO"``
            (case-insensitive).
    """
    logging.basicConfig(
        level=level.upper(),
        format=_FORMAT,
        datefmt=_DATE_FORMAT,
        force=True,
    )
