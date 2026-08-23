"""Tests for the /api/v1 model catalog endpoints."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from straticate.config import Settings
from straticate.main import create_app
from tests.test_model_catalog import make_model, write_catalog

MODELS_URL = "/api/v1/models"
MODES_URL = "/api/v1/separation-modes"

MODEL_KEYS = {
    "id",
    "display_name",
    "architecture",
    "version",
    "separation_mode",
    "quality_tier",
    "stems",
    "sample_rate",
    "requirements",
    "capabilities",
}
MODE_KEYS = {"id", "display_name", "stems", "quality_options"}
QUALITY_OPTION_KEYS = {"id", "display_name", "model_id"}


@pytest.fixture
async def synthetic_client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """A client for an app whose catalog carries inference-only manifest fields."""
    write_catalog(
        tmp_path,
        [
            make_model(
                "m-fast",
                quality_tier="fast",
                default_inference_parameters={"segment_size": 256, "overlap": 4},
            ),
            make_model(
                "m-hq",
                quality_tier="high_quality",
                default_inference_parameters={"segment_size": 512},
            ),
        ],
    )
    app: FastAPI = create_app(Settings(models_dir=tmp_path, data_dir=tmp_path / "data"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_list_models_returns_the_catalog(client: httpx.AsyncClient) -> None:
    response = await client.get(MODELS_URL)
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    assert [model["id"] for model in body] == ["fake-vocals-001", "fake-standard-001"]
    assert set(body[0]) == MODEL_KEYS


async def test_get_model_returns_one_model(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{MODELS_URL}/fake-standard-001")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["separation_mode"] == "standard_stems"
    assert body["stems"] == ["vocals", "drums", "bass", "other"]
    assert body["capabilities"] == {"cuda": True, "cpu": True}
    assert body["quality_tier"] is None


async def test_get_unknown_model_returns_the_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{MODELS_URL}/does-not-exist")
    assert response.status_code == 404
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "does-not-exist" in error["message"]


async def test_separation_modes_are_derived_from_the_catalog(client: httpx.AsyncClient) -> None:
    response = await client.get(MODES_URL)
    assert response.status_code == 200
    modes: list[dict[str, Any]] = response.json()
    assert {mode["id"] for mode in modes} == {"vocals", "standard_stems"}

    by_id = {mode["id"]: mode for mode in modes}
    assert set(by_id["vocals"]) == MODE_KEYS
    assert by_id["vocals"]["display_name"] == "Vocal Isolation"
    assert by_id["vocals"]["stems"] == ["vocals", "instrumental"]
    assert by_id["standard_stems"]["stems"] == ["vocals", "drums", "bass", "other"]

    for mode in modes:
        assert mode["quality_options"], mode["id"]
        for option in mode["quality_options"]:
            assert set(option) == QUALITY_OPTION_KEYS
            assert option["model_id"]


async def test_separation_modes_expose_quality_tiers_not_inference_parameters(
    synthetic_client: httpx.AsyncClient,
) -> None:
    response = await synthetic_client.get(MODES_URL)
    assert response.status_code == 200
    (mode,) = response.json()
    assert [option["id"] for option in mode["quality_options"]] == ["fast", "high_quality"]
    assert "default_inference_parameters" not in response.text
    assert "segment_size" not in response.text


async def test_models_endpoint_hides_inference_parameters(
    synthetic_client: httpx.AsyncClient,
) -> None:
    response = await synthetic_client.get(MODELS_URL)
    assert response.status_code == 200
    assert "default_inference_parameters" not in response.text
    assert "segment_size" not in response.text
    assert set(response.json()[0]) == MODEL_KEYS


async def test_endpoints_appear_in_the_openapi_document(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/models" in paths
    assert "/api/v1/models/{model_id}" in paths
    assert "/api/v1/separation-modes" in paths
