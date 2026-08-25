"""Shared test fixtures."""

from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
from fastapi import FastAPI

from straticate.config import get_settings
from straticate.main import create_app

DEVELOPMENT_MODELS_ENV = "STRATICATE_INCLUDE_DEVELOPMENT_MODELS"
"""Environment variable that puts the catalog's development fixtures back.

Feature 032 hides ``development_only`` catalog entries by default, and the fake
separator of ARCHITECTURE.md §8 is exactly such an entry — which is what most of
this suite separates audio with. Rather than rewrite those tests around a real
model that needs an 870 MiB download, the *setting* is flipped for the whole
session: with it on, the catalog is served precisely as it was before 032, so
every pre-existing test still asserts what it always did.

It is set as an environment variable, not as an argument to ``create_app``,
because the suite builds applications along a dozen different paths —
``create_app()``, ``create_app(Settings(data_dir=...))``,
``ModelCatalog.from_directory(Settings().models_dir, ...)`` — and every one of
them reads :class:`~straticate.config.Settings`, which reads the environment.
One fixture therefore covers all of them, and a test that wants the *default*
(hidden) behaviour opts out explicitly with
``Settings(include_development_models=False)``.
"""


@pytest.fixture(autouse=True, scope="session")
def include_development_models() -> Iterator[None]:
    """Enable development fixtures for the whole test session.

    See :data:`DEVELOPMENT_MODELS_ENV`. ``get_settings`` is cached process-wide,
    so its cache is cleared on the way in and on the way out; nothing may
    observe a ``Settings`` built before the variable was set.
    """
    patch = pytest.MonkeyPatch()
    patch.setenv(DEVELOPMENT_MODELS_ENV, "1")
    get_settings.cache_clear()
    try:
        yield
    finally:
        patch.undo()
        get_settings.cache_clear()


@pytest.fixture
def app() -> FastAPI:
    """A freshly created application instance."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """An HTTP client bound to the app via in-process ASGI transport.

    ``raise_app_exceptions`` is left at its default ``True``, and that default
    is load-bearing: an unexpected route exception must surface here as a
    failing test rather than as a quiet 500 envelope. It only does so because
    :class:`~straticate.errors.ErrorEnvelopeMiddleware` re-raises after sending
    the envelope — a test that wants to *inspect* a 500 body opts out
    explicitly with ``raise_app_exceptions=False`` (see ``test_errors.py``).
    """
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
