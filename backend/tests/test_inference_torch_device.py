"""The CUDA telemetry path, exercised on a CPU-only host through one seam.

**These are feature 026's tests, moved.** They were written in
``tests/test_roformer_separator.py`` against
``straticate.inference.roformer.separator``'s module globals — 026 added the
``cuda_namespace()`` indirection specifically so this path could be covered on a
host with no GPU — and feature 028 copied them, verbatim, against its own
module's globals. Feature 039 moved the code they cover into
:mod:`straticate.inference.torch_device`, so the tests followed it here: one
copy, patching one module, with their assertions and their reasoning unchanged.

Everything below replaces ``torch_device.cuda_namespace`` with a double. It is
still the only way this code gets covered before it meets real hardware; the
``gpu``-marked tests in ``tests/test_roformer_integration.py`` and
``tests/test_demucs_integration.py`` are what run it against a real device.
"""

import sys
from types import SimpleNamespace
from typing import Any

import pytest
import torch

from straticate.errors import ApplicationError
from straticate.inference import torch_device as device_module
from straticate.inference.torch_device import (
    NvmlProbe,
    device_stats,
    reset_peak_memory,
    resolve_torch_device,
)

# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class FakeProperties:
    """What ``torch.cuda.get_device_properties`` gives us, minus everything else."""

    def __init__(self, name: str, total_memory: int) -> None:
        self.name = name
        self.total_memory = total_memory


class FakeCuda:
    """A recording stand-in for the ``torch.cuda`` namespace."""

    def __init__(self, *, allocated: int = 1 << 30, peak: int = 3 << 30) -> None:
        self.allocated = allocated
        self.peak = peak
        self.resets: list[str] = []

    def is_available(self) -> bool:
        return True

    def get_device_properties(self, index: int) -> FakeProperties:
        return FakeProperties(f"NVIDIA Fake {index}", 8 << 30)

    def memory_allocated(self, index: int) -> int:
        return self.allocated

    def max_memory_allocated(self, index: int) -> int:
        return self.peak

    def reset_peak_memory_stats(self, device: object) -> None:
        self.resets.append(str(device))
        # What torch really does: the high-water mark drops to what is
        # currently allocated, so the resident model still counts.
        self.peak = self.allocated


class FakeAtexit:
    """Captures teardown registrations instead of leaking them into the session."""

    def __init__(self) -> None:
        self.hooks: list[object] = []

    def register(self, hook: Any) -> Any:
        self.hooks.append(hook)
        return hook


class FakeNvml:
    """A ``pynvml`` double that counts how often it is initialised."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self) -> None:
        self.inits = 0
        self.shutdowns = 0
        self.handles = 0
        self.samples = 0

    def nvmlInit(self) -> None:
        self.inits += 1

    def nvmlShutdown(self) -> None:
        self.shutdowns += 1

    def nvmlDeviceGetHandleByIndex(self, index: int) -> str:
        self.handles += 1
        return f"handle-{index}"

    def nvmlDeviceGetUtilizationRates(self, handle: str) -> Any:
        self.samples += 1
        return SimpleNamespace(gpu=63)

    def nvmlDeviceGetTemperature(self, handle: str, sensor: int) -> int:
        return 61


@pytest.fixture
def fake_cuda(monkeypatch: pytest.MonkeyPatch) -> FakeCuda:
    cuda = FakeCuda()
    monkeypatch.setattr(device_module, "cuda_namespace", lambda: cuda)
    return cuda


# --------------------------------------------------------------------------
# Device resolution
# --------------------------------------------------------------------------


def test_no_device_and_cpu_both_resolve_to_the_cpu() -> None:
    """A job that pinned nothing runs on the CPU, not on a guess."""
    assert resolve_torch_device(None) == torch.device("cpu")
    assert resolve_torch_device("cpu") == torch.device("cpu")


def test_a_device_this_build_cannot_use_is_a_clear_409() -> None:
    """The one place a logical device ID (018) becomes a torch device.

    Both separators' suites prove this end to end through a whole separation;
    this is the same envelope at the function it comes from, which is where the
    two backends now share it.
    """
    with pytest.raises(ApplicationError) as excinfo:
        resolve_torch_device("nonsense:0")
    error = excinfo.value
    assert error.code == "compute_device_unavailable"
    assert error.status_code == 409
    assert error.detail is not None
    assert error.detail["device_id"] == "nonsense:0"


# --------------------------------------------------------------------------
# Device statistics
# --------------------------------------------------------------------------


def test_device_stats_report_the_devices_real_memory_figures(fake_cuda: FakeCuda) -> None:
    stats = device_stats(torch.device("cuda:1"))
    assert stats is not None
    assert stats.device_id == "cuda:1"
    assert stats.backend == "cuda"
    assert stats.name == "NVIDIA Fake 1"
    assert stats.memory_allocated_bytes == 1 << 30
    assert stats.memory_peak_bytes == 3 << 30
    assert stats.memory_total_bytes == 8 << 30
    # NVML absent: the two optional fields stay empty, everything else does not.
    assert stats.utilization is None
    assert stats.temperature_celsius is None
    assert stats.to_gpu_metrics().device_id == "cuda:1"


def test_device_stats_are_absent_on_cpu(fake_cuda: FakeCuda) -> None:
    """The contract renders "no device block" as ``gpu: null``."""
    assert device_stats(torch.device("cpu")) is None
    assert fake_cuda.resets == []


def test_reset_peak_memory_only_touches_cuda(fake_cuda: FakeCuda) -> None:
    reset_peak_memory(torch.device("cpu"))
    assert fake_cuda.resets == []
    reset_peak_memory(torch.device("cuda:0"))
    assert fake_cuda.resets == ["cuda:0"]


def test_a_reset_restarts_the_high_water_mark_from_the_resident_allocation(
    fake_cuda: FakeCuda,
) -> None:
    """A previous run's peak does not survive the reset; the resident model does."""
    fake_cuda.allocated = 2 << 30
    fake_cuda.peak = 7 << 30
    before = device_stats(torch.device("cuda:0"))
    assert before is not None
    assert before.memory_peak_bytes == 7 << 30

    reset_peak_memory(torch.device("cuda:0"))

    after = device_stats(torch.device("cuda:0"))
    assert after is not None
    assert after.memory_peak_bytes == 2 << 30


# --------------------------------------------------------------------------
# NVML stays optional, and stays cheap
# --------------------------------------------------------------------------


def test_nvml_is_initialised_once_and_not_per_sample(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``runtime_stats()`` is called on the event loop, ~1 Hz, for a whole job.

    ``straticate.telemetry.sampler`` polls it directly on the loop because
    ``inference/base.py`` promises a "cheap, non-blocking snapshot". An
    ``nvmlInit``/``nvmlShutdown`` pair per sample is tens of milliseconds of
    driver setup in front of every WebSocket frame the loop owes somebody, so
    the binding is initialised once and the handles are cached.
    """
    nvml = FakeNvml()
    exit_hooks = FakeAtexit()
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(device_module, "atexit", exit_hooks)
    monkeypatch.setattr(device_module, "_NVML", NvmlProbe())

    for _ in range(5):
        stats = device_stats(torch.device("cuda:0"))
        assert stats is not None
        assert stats.utilization == 0.63
        assert stats.temperature_celsius == 61.0

    assert nvml.inits == 1, "NVML was re-initialised per sample"
    assert nvml.shutdowns == 0, "NVML was torn down while a job was still sampling"
    assert nvml.handles == 1, "the device handle was re-fetched per sample"
    assert nvml.samples == 5
    assert len(exit_hooks.hooks) == 1, "teardown must be registered once, at exit"


def test_nvml_shuts_down_at_teardown(fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch) -> None:
    nvml = FakeNvml()
    exit_hooks = FakeAtexit()
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(device_module, "atexit", exit_hooks)
    probe = NvmlProbe()
    monkeypatch.setattr(device_module, "_NVML", probe)

    assert device_stats(torch.device("cuda:0")) is not None
    hook = exit_hooks.hooks[0]
    assert callable(hook)
    hook()
    assert nvml.shutdowns == 1
    # Idempotent: a second teardown is not a second shutdown.
    hook()
    assert nvml.shutdowns == 1


def test_a_missing_nvml_binding_costs_one_failed_import(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NVML is optional (ARCHITECTURE.md §12) and its absence is not an error."""
    attempts = 0

    def refuse(name: str) -> Any:
        nonlocal attempts
        attempts += 1
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(device_module.importlib, "import_module", refuse)
    monkeypatch.setattr(device_module, "_NVML", NvmlProbe())

    for _ in range(4):
        stats = device_stats(torch.device("cuda:0"))
        assert stats is not None
        assert stats.utilization is None
        assert stats.temperature_celsius is None
        # Everything that does not come from NVML is unaffected.
        assert stats.memory_total_bytes == 8 << 30

    assert attempts == 1, "an absent binding must not be re-imported on every sample"


def test_a_driver_failure_mid_job_does_not_break_the_snapshot(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    nvml = FakeNvml()

    def explode(handle: str) -> Any:
        raise RuntimeError("NVML_ERROR_GPU_IS_LOST")

    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(device_module, "atexit", FakeAtexit())
    monkeypatch.setattr(device_module, "_NVML", NvmlProbe())
    monkeypatch.setattr(nvml, "nvmlDeviceGetUtilizationRates", explode)

    stats = device_stats(torch.device("cuda:0"))
    assert stats is not None
    assert stats.utilization is None
    assert stats.memory_allocated_bytes == 1 << 30
