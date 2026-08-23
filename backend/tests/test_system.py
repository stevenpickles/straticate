"""Tests for the system endpoints."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

import straticate
from straticate.schemas import ComputeDevice
from straticate.system import CPU_BACKEND, CPU_DEVICE_ID, CUDA_BACKEND, DeviceDetector

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
async def gpu_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose app reports one fake CUDA device plus the CPU."""
    app.state.device_detector = DeviceDetector(probes=[_StaticProbe()])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_matches_package_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {"version": straticate.__version__}


async def test_devices_always_include_cpu(client: httpx.AsyncClient) -> None:
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


async def test_devices_report_cuda_before_cpu(gpu_client: httpx.AsyncClient) -> None:
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
