"""Tests for the system endpoints."""

import httpx

import straticate


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_matches_package_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"version": straticate.__version__}
