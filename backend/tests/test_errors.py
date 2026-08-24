"""Tests for the consistent error envelope."""

from datetime import UTC, datetime
from typing import Any

import httpx2
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


async def test_unknown_route_returns_enveloped_404(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    error = _envelope(response.json())
    assert error["code"] == "not_found"


async def test_validation_error_returns_envelope(app: FastAPI) -> None:
    @app.get("/api/v1/echo")
    async def echo(value: int) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        return {"value": value}

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
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

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
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

    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/crash")

    assert response.status_code == 500
    error = _envelope(response.json())
    assert error["code"] == "internal_error"
    assert "secret traceback detail" not in response.text
    assert "RuntimeError" not in response.text


async def test_internal_error_is_readable_cross_origin(app: FastAPI) -> None:
    """A 500 must carry CORS headers, or the browser hides the envelope.

    Asserting the body alone would pass even with the envelope produced in
    Starlette's outermost ``ServerErrorMiddleware``, where it is invisible to a
    cross-origin caller. The header assertion is the one that fails if the
    envelope ever moves back outside ``CORSMiddleware``.
    """

    @app.get("/api/v1/crash-cors")
    async def crash() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        raise RuntimeError("boom")

    origin = "http://localhost:5173"
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/crash-cors", headers={"Origin": origin})

    assert response.status_code == 500
    assert _envelope(response.json())["code"] == "internal_error"
    assert response.headers["access-control-allow-origin"] == origin


async def test_application_error_is_readable_cross_origin(app: FastAPI) -> None:
    """The same guarantee for the handled errors clients branch on."""

    @app.get("/api/v1/teapot-cors")
    async def teapot() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered via decorator
        raise ApplicationError("teapot", "I'm a teapot.", status_code=418)

    origin = "http://127.0.0.1:5173"
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/teapot-cors", headers={"Origin": origin})

    assert response.status_code == 418
    assert response.headers["access-control-allow-origin"] == origin


def test_application_error_to_error_info_json_encodes_detail() -> None:
    exc = ApplicationError(
        "teapot",
        "I'm a teapot.",
        status_code=418,
        detail={"when": datetime(2026, 1, 1, tzinfo=UTC), "hint": "short and stout"},
    )
    info = exc.to_error_info()
    assert info.code == "teapot"
    assert info.message == "I'm a teapot."
    # detail passes through jsonable_encoder, exactly like error_response:
    # non-JSON-native values become their JSON-safe representations.
    when = info.detail["when"]
    assert isinstance(when, str)
    assert when.startswith("2026-01-01T00:00:00")
    assert info.detail["hint"] == "short and stout"


def test_application_error_to_error_info_defaults_to_empty_detail() -> None:
    info = ApplicationError("code_only", "No detail.").to_error_info()
    assert info.detail == {}
