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
    """An HTTP client bound to the app via in-process ASGI transport."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
