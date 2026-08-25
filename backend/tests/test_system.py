"""Tests for the system endpoints."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import httpx2
import pytest
from fastapi import FastAPI

import straticate
from straticate.config import Settings
from straticate.schemas import ComputeDevice
from straticate.system import (
    CPU_BACKEND,
    CPU_DEVICE_ID,
    CUDA_BACKEND,
    DeviceDetector,
    DiskUsageLike,
    nearest_existing_dir,
    storage,
)

_FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)


class _StaticProbe:
    """Probe reporting a fixed device list, standing in for real CUDA."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [_FAKE_GPU]


@pytest.fixture
async def gpu_client(app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A client whose app reports one fake CUDA device plus the CPU."""
    app.state.device_detector = DeviceDetector(probes=[_StaticProbe()])
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_matches_package_version(client: httpx2.AsyncClient) -> None:
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"version": straticate.__version__}


async def test_devices_always_include_cpu(client: httpx2.AsyncClient) -> None:
    """The real application detector: GPU-free CI reports the CPU device."""
    response = await client.get("/api/v1/system/devices")
    assert response.status_code == 200

    payload: list[dict[str, object]] = response.json()
    assert payload, "the device list is never empty"

    cpu = payload[-1]
    assert set(cpu) == {"id", "backend", "name", "memory_total_bytes"}
    assert cpu["id"] == CPU_DEVICE_ID
    assert cpu["backend"] == CPU_BACKEND

    name = cpu["name"]
    assert isinstance(name, str)
    assert name.strip()

    memory = cpu["memory_total_bytes"]
    assert isinstance(memory, int)
    assert memory >= 0


async def test_devices_report_cuda_before_cpu(gpu_client: httpx2.AsyncClient) -> None:
    response = await gpu_client.get("/api/v1/system/devices")
    assert response.status_code == 200

    payload: list[dict[str, object]] = response.json()
    assert payload[0] == {
        "id": "cuda:0",
        "backend": "cuda",
        "name": "NVIDIA GeForce RTX 5090",
        "memory_total_bytes": 34359738368,
    }
    assert [device["backend"] for device in payload] == [CUDA_BACKEND, CPU_BACKEND]


# --------------------------------------------------------------------------
# GET /system/storage
# --------------------------------------------------------------------------


class _FixedUsage:
    """A ``shutil.disk_usage`` reading, as far as the storage report cares."""

    def __init__(self, total: int, free: int) -> None:
        self.total = total
        self.free = free


async def test_storage_reports_the_models_filesystem(client: httpx2.AsyncClient) -> None:
    """The real application, the real platform primitive, no disk filled."""
    response = await client.get("/api/v1/system/storage")
    assert response.status_code == 200

    payload: dict[str, object] = response.json()
    assert set(payload) == {"free_bytes", "total_bytes"}

    free = payload["free_bytes"]
    total = payload["total_bytes"]
    assert isinstance(free, int)
    assert isinstance(total, int)
    assert total > 0
    assert 0 <= free <= total


async def test_storage_reports_unknown_rather_than_failing(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that cannot answer gets a documented ``null``, not a 500.

    The platform primitive is stubbed at its seam, so everything between it and
    the wire — the report, the route, the response model — is the real code.
    """

    def unavailable(path: Path) -> DiskUsageLike:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(storage, "read_disk_usage", unavailable)

    response = await client.get("/api/v1/system/storage")

    assert response.status_code == 200
    assert response.json() == {"free_bytes": None, "total_bytes": None}


async def test_storage_reads_the_settings_models_directory(
    client: httpx2.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The figures describe ``Settings.models_dir``, not the working directory."""
    seen: list[Path] = []

    def record(path: Path) -> DiskUsageLike:
        seen.append(path)
        return _FixedUsage(total=1_000_000, free=250_000)

    monkeypatch.setattr(storage, "read_disk_usage", record)

    response = await client.get("/api/v1/system/storage")

    assert response.status_code == 200
    assert response.json() == {"free_bytes": 250_000, "total_bytes": 1_000_000}
    settings = cast(Settings, app.state.settings)
    assert seen == [nearest_existing_dir(settings.models_dir)]
