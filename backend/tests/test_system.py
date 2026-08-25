"""Tests for the system endpoints."""

import asyncio
import threading
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


async def test_storage_reports_unknown_when_an_ancestor_cannot_be_read(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permissions failure in the *directory walk* is still a documented 200.

    ``Path.is_dir`` re-raises ``EACCES`` (only ``ENOENT``/``ENOTDIR``/``EBADF``/
    ``ELOOP`` are swallowed), so a models directory whose ancestor the process
    may not read raises inside the report. A review found the walk outside the
    guard, where that reached the client as a ``500`` — contradicting the
    endpoint's own contract and this feature's acceptance criterion.
    """

    def refuse(self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "is_dir", refuse)

    response = await client.get("/api/v1/system/storage")

    assert response.status_code == 200
    assert response.json() == {"free_bytes": None, "total_bytes": None}


class _BlockingUsage:
    """A disk read that parks its caller until the test releases it.

    Stands in for ``models_dir`` on an unresponsive network mount — the case
    this feature's warn-rather-than-refuse reasoning explicitly anticipates.
    The wait is **bounded**, so a regression (the read back on the event loop)
    fails the test in a few seconds instead of hanging the suite.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, path: Path) -> DiskUsageLike:
        self.entered.set()
        self.release.wait(_BLOCK_TIMEOUT)
        return _FixedUsage(total=1_000_000, free=250_000)


_BLOCK_TIMEOUT = 5.0
"""Ceiling on the parked read, so a regression fails rather than deadlocks."""

_RESPONSE_TIMEOUT = 2.0
"""How long another request may take while the slow read is parked."""


async def test_other_requests_are_served_while_a_storage_read_blocks(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the ``stat`` ran on the loop, nothing below could complete.

    ``shutil.disk_usage`` is a filesystem call that can hang for as long as an
    unresponsive mount does, and this endpoint deliberately does not cache — so
    the route offloads it with :func:`asyncio.to_thread`, the discipline
    features 022 and 025 already follow. Without that, one ``stat`` stalls every
    REST request, the WebSocket hub and job progress delivery alike.
    """
    blocking = _BlockingUsage()
    monkeypatch.setattr(storage, "read_disk_usage", blocking)

    task = asyncio.create_task(client.get("/api/v1/system/storage"))
    try:
        await asyncio.wait_for(
            asyncio.to_thread(blocking.entered.wait, _BLOCK_TIMEOUT), timeout=_BLOCK_TIMEOUT
        )
        assert blocking.entered.is_set(), "the read never started"

        health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=_RESPONSE_TIMEOUT)
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        devices = await asyncio.wait_for(
            client.get("/api/v1/system/devices"), timeout=_RESPONSE_TIMEOUT
        )
        assert devices.status_code == 200
    finally:
        blocking.release.set()

    response = await asyncio.wait_for(task, timeout=_BLOCK_TIMEOUT)
    assert response.status_code == 200
    assert response.json() == {"free_bytes": 250_000, "total_bytes": 1_000_000}
