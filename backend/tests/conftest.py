"""Shared test fixtures."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from straticate.main import create_app


@pytest.fixture
def app() -> FastAPI:
    """A freshly created application instance."""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the app via in-process ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
