"""Logging configuration for the Straticate backend."""

import logging

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str) -> None:
    """Configure stdlib logging with a concise console format.

    ``force=True`` replaces **every** handler on the root logger, for the whole
    interpreter. That is right for a process entry point and wrong for anything
    a library or a test might call more than once, so exactly one caller exists:
    :func:`straticate.main.serve`. Building an application does not call this
    (see :func:`straticate.main.create_app`), which is what lets pytest's
    ``caplog`` keep working across a suite that creates hundreds of apps.

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
