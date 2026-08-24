"""The package version has exactly one source of truth.

``straticate.__version__`` is resolved from installed distribution metadata,
which is built from ``pyproject.toml``. This test reads the *file* — not the
metadata — so it fails if the two are ever edited apart, which is the whole
point of the check.
"""

import tomllib
from pathlib import Path
from typing import Any

import httpx2

import straticate

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_version() -> str:
    """Return the version literally declared in ``backend/pyproject.toml``."""
    with PYPROJECT.open("rb") as handle:
        document: dict[str, Any] = tomllib.load(handle)
    project: dict[str, Any] = document["project"]
    declared = project["version"]
    assert isinstance(declared, str)
    return declared


def test_package_version_matches_pyproject() -> None:
    assert straticate.__version__ == _declared_version()


def test_version_is_not_the_missing_distribution_fallback() -> None:
    # A stale or absent editable install would report the fallback and make the
    # comparison above pass only by accident; it must never be what we serve.
    assert straticate.__version__ != straticate.UNKNOWN_VERSION


async def test_version_endpoint_serves_the_same_version(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"version": _declared_version()}
