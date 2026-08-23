"""Tests for the consistent error envelope."""

from typing import Any

import httpx
from fastapi import FastAPI

from straticate.errors import ApplicationError


def _envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Assert the standard envelope shape and return its ``error`` object."""
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "detail"}
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)
    assert isinstance(error["detail"], dict)
    return error


async def test_unknown_route_returns_enveloped_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    error = _envelope(response.json())
    assert error["code"] == "not_found"


async def test_validation_error_returns_envelope(app: FastAPI) -> None:
    @app.get("/api/v1/echo")
    async def echo(value: int) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        return {"value": value}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/echo", params={"value": "not-an-int"})

    assert response.status_code == 422
    error = _envelope(response.json())
    assert error["code"] == "validation_error"
    assert error["detail"]["errors"], "pydantic errors should be included in detail"


async def test_application_error_returns_envelope(app: FastAPI) -> None:
    @app.get("/api/v1/boom")
    async def boom() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        raise ApplicationError(
            "teapot",
            "I'm a teapot.",
            status_code=418,
            detail={"hint": "short and stout"},
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/boom")

    assert response.status_code == 418
    error = _envelope(response.json())
    assert error["code"] == "teapot"
    assert error["message"] == "I'm a teapot."
    assert error["detail"] == {"hint": "short and stout"}


async def test_unhandled_exception_returns_internal_error(app: FastAPI) -> None:
    @app.get("/api/v1/crash")
    async def crash() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        raise RuntimeError("secret traceback detail")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/crash")

    assert response.status_code == 500
    error = _envelope(response.json())
    assert error["code"] == "internal_error"
    assert "secret traceback detail" not in response.text
    assert "RuntimeError" not in response.text
