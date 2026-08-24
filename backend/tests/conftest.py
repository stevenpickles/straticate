"""Shared test fixtures."""

from collections.abc import AsyncIterator

import httpx2
import pytest
from fastapi import FastAPI

from straticate.main import create_app


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
