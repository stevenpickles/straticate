"""Tests for the /api/v1 model catalog endpoints."""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
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
    "development_only",
    "separation_mode",
    "quality_tier",
    "stems",
    "sample_rate",
    "requirements",
    "capabilities",
    "licensing",
    "installation",
}
INSTALLATION_KEYS = {
    "state",
    "requires_download",
    "total_bytes",
    "downloaded_bytes",
    "progress",
    "error",
}
MODE_KEYS = {"id", "display_name", "stems", "quality_options"}
QUALITY_OPTION_KEYS = {"id", "display_name", "model_id"}


@pytest.fixture
async def synthetic_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
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
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_list_models_returns_the_catalog(client: httpx2.AsyncClient) -> None:
    response = await client.get(MODELS_URL)
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    assert [model["id"] for model in body] == [
        "fake-vocals-001",
        "fake-standard-001",
        "vocals-hq-001",
    ]
    assert set(body[0]) == MODEL_KEYS


async def test_get_model_returns_one_model(client: httpx2.AsyncClient) -> None:
    response = await client.get(f"{MODELS_URL}/fake-standard-001")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["separation_mode"] == "standard_stems"
    assert body["stems"] == ["vocals", "drums", "bass", "other"]
    assert body["capabilities"] == {"cuda": True, "cpu": True}
    assert body["quality_tier"] is None


async def test_get_unknown_model_returns_the_error_envelope(client: httpx2.AsyncClient) -> None:
    response = await client.get(f"{MODELS_URL}/does-not-exist")
    assert response.status_code == 404
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "does-not-exist" in error["message"]


async def test_separation_modes_are_derived_from_the_catalog(client: httpx2.AsyncClient) -> None:
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
    synthetic_client: httpx2.AsyncClient,
) -> None:
    response = await synthetic_client.get(MODES_URL)
    assert response.status_code == 200
    (mode,) = response.json()
    assert [option["id"] for option in mode["quality_options"]] == ["fast", "high_quality"]
    assert "default_inference_parameters" not in response.text
    assert "segment_size" not in response.text


async def test_models_endpoint_hides_inference_parameters(
    synthetic_client: httpx2.AsyncClient,
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


# --- Development fixtures (feature 032) -------------------------------------

FIXTURE_IDS = ("fake-vocals-001", "fake-standard-001")
"""The repository catalog's ``development_only`` entries."""


@asynccontextmanager
async def catalog_client(
    tmp_path: Path, *, development: bool
) -> AsyncGenerator[httpx2.AsyncClient]:
    """A client for the **repository** catalog, with fixtures on or off.

    The setting is passed explicitly rather than inherited from the environment:
    the whole test session enables fixtures (``conftest.DEVELOPMENT_MODELS_ENV``)
    so that the suite can separate audio at all, and these tests are about what
    each state actually serves.
    """
    app = create_app(Settings(data_dir=tmp_path / "data", include_development_models=development))
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def user_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for an application with the **default** (user-facing) settings."""
    async with catalog_client(tmp_path, development=False) as client:
        yield client


@pytest.fixture
async def development_client(tmp_path: Path) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for an application that deliberately opted fixtures back in."""
    async with catalog_client(tmp_path, development=True) as client:
        yield client


async def test_models_omits_development_fixtures_by_default(
    user_client: httpx2.AsyncClient,
) -> None:
    """``/models`` filters too, not only ``/separation-modes``.

    ``/models`` is arguably an inventory — but this application has no
    authentication and one audience, so an inventory listing a comb filter that
    ``/separation-modes`` refuses to offer is a contradiction a client would
    have to reconcile, and a fixture a client can see is one it can offer to
    install.
    """
    response = await user_client.get(MODELS_URL)
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    assert [model["id"] for model in body] == ["vocals-hq-001"]
    assert body[0]["development_only"] is False


async def test_models_lists_the_fixtures_when_they_are_enabled(
    development_client: httpx2.AsyncClient,
) -> None:
    response = await development_client.get(MODELS_URL)
    assert response.status_code == 200
    body: list[dict[str, Any]] = response.json()
    assert [model["id"] for model in body] == [*FIXTURE_IDS, "vocals-hq-001"]
    assert [model["development_only"] for model in body] == [True, True, False]


@pytest.mark.parametrize("model_id", FIXTURE_IDS)
async def test_fetching_a_hidden_model_is_the_ordinary_404(
    user_client: httpx2.AsyncClient, model_id: str
) -> None:
    """Not a 403 and not a distinct code.

    On a server that hides fixtures the ID names nothing the catalog contains,
    which is exactly what ``model_not_found`` says. A dedicated "hidden" code
    would be a second thing every client has to handle and would announce the
    existence of an entry it may not have.
    """
    response = await user_client.get(f"{MODELS_URL}/{model_id}")
    assert response.status_code == 404
    error: dict[str, Any] = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert model_id in error["message"]


@pytest.mark.parametrize("model_id", FIXTURE_IDS)
async def test_fetching_a_fixture_works_when_they_are_enabled(
    development_client: httpx2.AsyncClient, model_id: str
) -> None:
    response = await development_client.get(f"{MODELS_URL}/{model_id}")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert body["id"] == model_id
    assert body["development_only"] is True


@pytest.mark.parametrize("route", ["install", "weights"])
async def test_a_hidden_model_cannot_be_installed_or_removed(
    user_client: httpx2.AsyncClient, route: str
) -> None:
    """The whole model resource is absent, not just its representation."""
    model_id = FIXTURE_IDS[0]
    response = (
        await user_client.post(f"{MODELS_URL}/{model_id}/install")
        if route == "install"
        else await user_client.delete(f"{MODELS_URL}/{model_id}/weights")
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_separation_modes_offers_no_fixture_by_default(
    user_client: httpx2.AsyncClient,
) -> None:
    """The defect: ``balanced`` was the fake model, and 011's UI preselects it."""
    response = await user_client.get(MODES_URL)
    assert response.status_code == 200
    modes: list[dict[str, Any]] = response.json()
    assert [mode["id"] for mode in modes] == ["vocals"]
    assert [option["model_id"] for option in modes[0]["quality_options"]] == ["vocals-hq-001"]


async def test_no_mode_is_served_with_an_empty_option_list(
    user_client: httpx2.AsyncClient,
) -> None:
    """``standard_stems`` is gone, not empty.

    There is no real four-stem model until feature 028, and an empty mode is a
    choice the frontend would render and nobody could act on.
    """
    modes: list[dict[str, Any]] = (await user_client.get(MODES_URL)).json()
    assert "standard_stems" not in {mode["id"] for mode in modes}
    assert modes
    for mode in modes:
        assert mode["quality_options"], mode["id"]


async def test_separation_modes_are_unchanged_when_fixtures_are_enabled(
    development_client: httpx2.AsyncClient,
) -> None:
    """The opt-in reproduces the pre-032 response exactly."""
    modes: list[dict[str, Any]] = (await development_client.get(MODES_URL)).json()
    by_id = {mode["id"]: mode for mode in modes}
    assert set(by_id) == {"vocals", "standard_stems"}
    assert [option["model_id"] for option in by_id["vocals"]["quality_options"]] == [
        "fake-vocals-001",
        "vocals-hq-001",
    ]
    assert [option["model_id"] for option in by_id["standard_stems"]["quality_options"]] == [
        "fake-standard-001"
    ]
    # The quality option itself is unchanged: no field was added to it, so a
    # frontend generated against the old contract still type-checks.
    for mode in modes:
        for option in mode["quality_options"]:
            assert set(option) == QUALITY_OPTION_KEYS
