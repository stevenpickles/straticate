"""Tests for compute device detection.

Every test here runs on a GPU-free machine with **no PyTorch installed**: CUDA
detection is exercised through injected fakes (a fake torch module for the real
probe, or a fake probe for the detector).
"""

import importlib
import logging
import platform
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from straticate.errors import ApplicationError
from straticate.schemas import ComputeDevice
from straticate.system import (
    CPU_BACKEND,
    CPU_DEVICE_ID,
    CUDA_BACKEND,
    CudaDevicePropertiesLike,
    DeviceDetector,
    TorchCudaProbe,
    cpu_device,
    cpu_name,
    load_torch,
    total_system_memory_bytes,
)

_GPU_A = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)
_GPU_B = ComputeDevice(
    id="cuda:1",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 4070",
    memory_total_bytes=12884901888,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeDeviceProperties:
    """Stand-in for ``torch.cuda.get_device_properties()`` results."""

    name: str
    total_memory: int


@dataclass
class FakeCuda:
    """Stand-in for the ``torch.cuda`` namespace."""

    devices: list[FakeDeviceProperties] = field(default_factory=list[FakeDeviceProperties])
    available: bool = True

    def is_available(self) -> bool:
        return self.available

    def device_count(self) -> int:
        return len(self.devices)

    def get_device_properties(self, device: int) -> CudaDevicePropertiesLike:
        return self.devices[device]


@dataclass
class FakeTorch:
    """Stand-in for the ``torch`` module (only what the probe consumes)."""

    cuda: FakeCuda


@dataclass
class StaticProbe:
    """A probe returning a fixed device list."""

    backend: str
    devices: Sequence[ComputeDevice]

    def detect(self) -> Sequence[ComputeDevice]:
        return self.devices


@dataclass
class ExplodingProbe:
    """A probe that misbehaves by raising."""

    backend: str = CUDA_BACKEND

    def detect(self) -> Sequence[ComputeDevice]:
        raise RuntimeError("probe exploded")


def _two_gpu_torch() -> FakeTorch:
    return FakeTorch(
        cuda=FakeCuda(
            devices=[
                FakeDeviceProperties("NVIDIA GeForce RTX 5090", 34359738368),
                FakeDeviceProperties("NVIDIA GeForce RTX 4070", 12884901888),
            ]
        )
    )


def _cuda_detector() -> DeviceDetector:
    """A detector whose only accelerator probe reports two fake GPUs."""
    return DeviceDetector(probes=[StaticProbe(CUDA_BACKEND, [_GPU_A, _GPU_B])])


# --------------------------------------------------------------------------
# No PyTorch required
# --------------------------------------------------------------------------


def test_module_imports_without_torch() -> None:
    """Importing the package must never pull in (or require) PyTorch."""
    importlib.import_module("straticate.system.devices")
    assert "torch" not in sys.modules


def test_load_torch_returns_none_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(name: str) -> object:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", _raise)
    assert load_torch() is None


def test_default_probe_reports_no_devices_without_torch() -> None:
    probe = TorchCudaProbe(loader=lambda: None)
    assert probe.detect() == ()
    assert probe.backend == CUDA_BACKEND


# --------------------------------------------------------------------------
# CPU device
# --------------------------------------------------------------------------


def test_cpu_device_is_always_reported() -> None:
    devices = DeviceDetector(probes=[]).devices()
    assert [device.id for device in devices] == [CPU_DEVICE_ID]

    cpu = devices[0]
    assert cpu.backend == CPU_BACKEND
    assert cpu.name.strip()
    assert cpu.memory_total_bytes >= 0


def test_cpu_name_falls_back_when_processor_is_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "processor", lambda: "   ")
    assert cpu_name() in {platform.uname().machine.strip(), "CPU"}
    assert cpu_name() == cpu_device().name


def test_total_system_memory_is_plausible() -> None:
    """0 means "unknown"; anything else must be a sane positive size."""
    total = total_system_memory_bytes()
    assert total == 0 or total >= 128 * 1024**2


# --------------------------------------------------------------------------
# CUDA detection through the torch probe
# --------------------------------------------------------------------------


def test_torch_probe_maps_two_cuda_devices() -> None:
    torch = _two_gpu_torch()
    devices = TorchCudaProbe(loader=lambda: torch).detect()

    assert [device.model_dump() for device in devices] == [
        {
            "id": "cuda:0",
            "backend": "cuda",
            "name": "NVIDIA GeForce RTX 5090",
            "memory_total_bytes": 34359738368,
        },
        {
            "id": "cuda:1",
            "backend": "cuda",
            "name": "NVIDIA GeForce RTX 4070",
            "memory_total_bytes": 12884901888,
        },
    ]


def test_torch_probe_ignores_unavailable_cuda_runtime() -> None:
    torch = _two_gpu_torch()
    torch.cuda.available = False
    assert TorchCudaProbe(loader=lambda: torch).detect() == ()


def test_cuda_devices_sort_before_cpu() -> None:
    torch = _two_gpu_torch()
    devices = DeviceDetector(probes=[TorchCudaProbe(loader=lambda: torch)]).devices()

    assert [device.id for device in devices] == ["cuda:0", "cuda:1", CPU_DEVICE_ID]
    assert [device.backend for device in devices] == [CUDA_BACKEND, CUDA_BACKEND, CPU_BACKEND]


# --------------------------------------------------------------------------
# Failing probes
# --------------------------------------------------------------------------


def test_failing_probe_degrades_to_cpu_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="straticate.system.devices")
    devices = DeviceDetector(probes=[ExplodingProbe()]).devices()

    assert [device.id for device in devices] == [CPU_DEVICE_ID]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert CUDA_BACKEND in warnings[0].getMessage()


def test_failing_probe_does_not_hide_healthy_probes() -> None:
    detector = DeviceDetector(probes=[ExplodingProbe(), StaticProbe("mps", [_GPU_A])])
    assert [device.id for device in detector.devices()] == [_GPU_A.id, CPU_DEVICE_ID]


# --------------------------------------------------------------------------
# Detector API
# --------------------------------------------------------------------------


def test_devices_are_detected_once_and_cached() -> None:
    calls = 0

    def loader() -> FakeTorch:
        nonlocal calls
        calls += 1
        return _two_gpu_torch()

    detector = DeviceDetector(probes=[TorchCudaProbe(loader=loader)])
    assert detector.devices() == detector.devices()
    assert calls == 1

    detector.refresh()
    assert calls == 2


def test_select_default_device_prefers_cuda() -> None:
    assert _cuda_detector().select_default_device() == _GPU_A


def test_select_default_device_falls_back_to_cpu() -> None:
    default = DeviceDetector(probes=[]).select_default_device()
    assert default.id == CPU_DEVICE_ID
    assert default.backend == CPU_BACKEND


def test_get_device_returns_known_devices() -> None:
    detector = _cuda_detector()
    assert detector.get_device("cuda:1") == _GPU_B
    assert detector.get_device(CPU_DEVICE_ID).backend == CPU_BACKEND


def test_get_device_raises_404_for_unknown_id() -> None:
    with pytest.raises(ApplicationError) as excinfo:
        DeviceDetector(probes=[]).get_device("cuda:7")

    error = excinfo.value
    assert error.code == "device_not_found"
    assert error.status_code == 404
    assert error.detail == {"device_id": "cuda:7"}
