"""Logging configuration for the Straticate backend.

Two entry points, because "configure logging" means two different things
depending on who is asking:

- :func:`configure_logging` — *I own this process.* Replaces the root logger's
  handlers outright. Exactly one caller: :func:`straticate.main.serve`.
- :func:`ensure_logging_configured` — *make sure application logs are readable,
  without overriding anyone.* Called from the application lifespan, so the
  documented ``uvicorn straticate.main:app`` command produces properly
  formatted ``straticate.*`` records too.

Neither is called at import time, and neither is called by
:func:`straticate.main.create_app`: building an application configures nothing
process-global, which is what keeps pytest's ``caplog`` working across a suite
that creates hundreds of applications.
"""

import logging

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str) -> None:
    """Configure stdlib logging with a concise console format, replacing any existing setup.

    ``force=True`` replaces **every** handler on the root logger, for the whole
    interpreter. That is right for a process entry point and wrong for anything
    a library or a test might call more than once, so exactly one caller
    exists: :func:`straticate.main.serve`, which owns its process.

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


def ensure_logging_configured(level: str) -> None:
    """Configure application logging **only if nobody else has**.

    This is the call the application lifespan makes, and the reason it is not
    :func:`configure_logging` is that the lifespan does not own the process.

    Why it is needed at all: uvicorn's ``LOGGING_CONFIG`` declares handlers for
    ``uvicorn``, ``uvicorn.error`` and ``uvicorn.access`` and leaves the root
    logger alone. Under the documented ``uvicorn straticate.main:app`` command
    every ``straticate.*`` record would therefore fall through to
    ``logging.lastResort``: WARNING and above only, bare ``%(message)s``, no
    timestamp and no logger name — and ``STRATICATE_LOG_LEVEL=DEBUG`` would
    produce nothing at all.

    Why it is safe: :func:`logging.basicConfig` without ``force`` is a no-op
    when the root logger already has handlers. So an embedding process keeps
    its own configuration, pytest keeps the ``caplog`` handler it attached, and
    :func:`serve`'s authoritative :func:`configure_logging` is never undone by
    the lifespan that follows it.

    Args:
        level: Log level name such as ``"DEBUG"`` or ``"INFO"``
            (case-insensitive).
    """
    logging.basicConfig(level=level.upper(), format=_FORMAT, datefmt=_DATE_FORMAT)


__all__ = ["configure_logging", "ensure_logging_configured"]
