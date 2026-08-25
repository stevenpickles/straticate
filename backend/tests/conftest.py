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


FAKE_QUALITY_TIERS = {"vocals": "balanced", "standard_stems": "fast"}
"""Quality tier the shipped development fixture of each mode backs.

Both fixtures declare their tier explicitly in ``models/catalog.json`` (feature
032): relying on the manifest default meant an untiered fixture silently
occupied ``balanced``, the tier a *real* model gets by saying nothing, which
would have blocked feature 028's four-stem model at load for every user. The
mapping lives here because three test modules build create-job bodies and a
fixture's tier is a coordinate of the mode, not the subject of any of them.
"""


def fake_quality_id(mode_id: str) -> str:
    """The ``quality_id`` a job must ask for to get ``mode_id``'s fake model.

    Unknown modes fall back to ``balanced``, so a test probing a mode that does
    not exist still sends a well-formed body.
    """
    return FAKE_QUALITY_TIERS.get(mode_id, "balanced")


@pytest.fixture(autouse=True, scope="session")
def include_development_models() -> Iterator[None]:
    """Enable development fixtures for the whole test session.

    See :data:`DEVELOPMENT_MODELS_ENV`. ``get_settings`` is cached process-wide,
    so its cache is cleared on the way in and on the way out.

    **One object escapes this fixture: ``straticate.main.app``.** It is built at
    module import — ``app = create_app()`` at the bottom of ``main.py`` — which
    happens when this very file imports ``create_app``, before any fixture runs.
    It therefore permanently holds a catalog with the fixtures *hidden*, while
    every application built during a test has them visible. Nothing depends on
    that today (the tests that touch ``main.app`` assert its identity and title,
    not its catalog), and it is not repaired here because reassigning a module
    global from a fixture is a worse trap than the one it would close. A future
    test that drives ``main.app`` through a client must therefore build its own
    application with :func:`~straticate.main.create_app`, or it will see one
    model where the rest of the suite sees three.
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
