"""Torch devices and CUDA/NVML telemetry, shared by every torch-backed separator.

Feature 039. This module is the one place a *logical* compute device ID
(feature 018) becomes a ``torch.device``, the one place ``torch.cuda``'s
unannotated members are reached, and the one place NVML is touched — for the
RoFormer backend, the Demucs backend and every backend after them.

It used to be five definitions copied into each backend. Feature 028 said so in
a comment above its copy and recorded the extraction as the obvious follow-up;
this is that follow-up. What made it worth waiting for is the seam below:

**``cuda_namespace()`` is a test seam, not decoration.** Feature 026 introduced
it so the CUDA telemetry path could be exercised on a host with no GPU — a test
replaces this module's ``cuda_namespace`` with a double and every CUDA figure
in :func:`device_stats` comes from it. Those tests live in
``tests/test_inference_torch_device.py``, which is where the seam moved to; they
are 026's tests, assertions unchanged, patching this module instead of two.

Nothing here knows what a separator is. What crosses back out is
:class:`~straticate.inference.base.DeviceStats` — the architecture-neutral
telemetry record ARCHITECTURE.md §12 describes — and a ``torch.device``, which
never leaves the inference package (ARCHITECTURE.md §10).
"""

from __future__ import annotations

import atexit
import importlib
import logging
from typing import Any, Protocol, cast

import torch

from straticate.errors import ApplicationError
from straticate.inference.base import DeviceStats

logger = logging.getLogger(__name__)


def resolve_torch_device(device_id: str | None) -> torch.device:
    """Map a *logical* device ID (feature 018) onto a torch device.

    This is the only place the two vocabularies meet, and it is deliberately one
    small function: ARCHITECTURE.md §10 says raw torch device objects never leak
    through application-level APIs, so they are constructed here from the ID the
    job already resolved and recorded.

    Raises:
        ApplicationError: ``compute_device_unavailable`` (409) when the job
            names a device this process cannot use — a CUDA device on a host
            whose CUDA runtime has gone away since detection.
    """
    if device_id is None or device_id == "cpu":
        return torch.device("cpu")
    try:
        device = torch.device(device_id)
    except (RuntimeError, ValueError) as exc:
        raise _device_unavailable(device_id, "not a device this build understands") from exc
    if device.type == "cpu":
        return device
    if device.type != "cuda" or not torch.cuda.is_available():
        raise _device_unavailable(device_id, "no such compute backend is available")
    if (device.index or 0) >= torch.cuda.device_count():
        raise _device_unavailable(device_id, "no such device index")
    return device


def _device_unavailable(device_id: str, reason: str) -> ApplicationError:
    return ApplicationError(
        "compute_device_unavailable",
        f"Compute device {device_id!r} is not available: {reason}.",
        status_code=409,
        detail={"device_id": device_id, "reason": reason},
    )


class _CudaDevicePropertiesLike(Protocol):
    """The subset of ``torch.cuda.get_device_properties()`` this module reads.

    The same narrowing :mod:`straticate.system.devices` applies, and for the
    same reason: torch's own return type is untyped, and a structural protocol
    states exactly what is consumed instead of spraying ``Any`` through a strict
    type check.
    """

    name: str
    total_memory: int


def cuda_namespace() -> Any:
    """The ``torch.cuda`` namespace, as ``Any``.

    Two jobs in one small function. It is where torch's unannotated CUDA
    members are reached (strict mode reports them as partially unknown, and
    :mod:`straticate.system.devices` narrows the same way), and it is the single
    seam a test replaces to exercise the CUDA telemetry path on a host with no
    GPU — which is the only way any of this gets covered until real hardware
    runs it.
    """
    return torch.cuda


def reset_peak_memory(device: torch.device) -> None:
    """Start a fresh peak-memory measurement for ``device``; a no-op off CUDA.

    ``torch.cuda.max_memory_allocated`` is a **per-device high-water mark that
    only an explicit reset clears**, so this belongs once per separation — which
    is where :meth:`straticate.inference.torch_separator.TorchSeparator._separate`
    calls it, per **run** and not per device placement (026's review finding). It
    resets the peak to the currently allocated figure rather than to zero, so the
    resident network still counts towards the run's peak.
    """
    if device.type != "cuda":
        return
    cuda_namespace().reset_peak_memory_stats(device)


def device_stats(device: torch.device) -> DeviceStats | None:
    """Real device telemetry, or ``None`` on CPU (the contract's ``gpu: null``).

    ARCHITECTURE.md §12 lists utilization and temperature as NVML-sourced and
    **optional**; they stay ``None`` unless an NVML binding happens to be
    importable, so nothing here can make basic operation depend on it.

    Reading this is cheap, which :mod:`straticate.inference.base` requires: on
    CUDA it is three allocator queries, a cached device-property lookup and two
    NVML queries against a handle initialised once per process (see
    :class:`NvmlProbe`). The telemetry sampler calls it **directly on the event
    loop**, ~1 Hz, for the length of a job.
    """
    cuda = cuda_namespace()
    if device.type != "cuda" or not cuda.is_available():
        return None
    index = device.index or 0
    properties = cast("_CudaDevicePropertiesLike", cuda.get_device_properties(index))
    utilization, temperature = _NVML.sample(index)
    return DeviceStats(
        device_id=f"cuda:{index}",
        name=str(properties.name),
        backend="cuda",
        memory_allocated_bytes=int(cuda.memory_allocated(index)),
        memory_peak_bytes=int(cuda.max_memory_allocated(index)),
        memory_total_bytes=int(properties.total_memory),
        utilization=utilization,
        temperature_celsius=temperature,
    )


class NvmlProbe:
    """Optional NVML utilization/temperature, initialised **at most once**.

    NVML is not a dependency and never becomes one (ARCHITECTURE.md §12: basic
    operation must never require it). But it is sampled from
    :func:`device_stats`, which
    :meth:`straticate.inference.torch_separator.TorchSeparator.runtime_stats`
    calls and which :class:`straticate.telemetry.TelemetrySampler` in turn calls
    **directly on the event loop** — deliberately, because
    :mod:`straticate.inference.base` promises that ``runtime_stats()`` "must be a
    cheap, non-blocking snapshot".

    An ``nvmlInit()``/``nvmlShutdown()`` pair per sample is not that: it is tens
    of milliseconds of driver setup and teardown, once a second, for the whole
    length of a job, in front of every WebSocket frame, job event and HTTP
    request the loop owes somebody. So the binding is loaded and initialised
    lazily on the first sample, the device handles are cached, and shutdown is
    left to :mod:`atexit` — which is how a long-running NVML consumer is meant
    to behave anyway. What remains per sample is two driver queries.

    A failure at any point is absorbed: the two optional fields stay ``None``
    and every other number in the snapshot is unaffected. A failure to *load*
    is remembered, so an absent binding costs one failed import per process
    rather than one per sample.

    The module imported here is ``pynvml``, but the package that supplies it is
    **``nvidia-ml-py``** — NVIDIA's own binding. The PyPI package *named*
    ``pynvml`` is a deprecated shim that installs an import hook raising
    ``FutureWarning``, and since ``torch.cuda`` imports ``pynvml`` at torch
    import time, installing it makes every test that imports torch fail under
    ``-W error``. DEVELOPMENT.md, *Optional: NVML*, has the traceback.
    """

    __slots__ = ("_handles", "_module", "_unavailable")

    def __init__(self) -> None:
        self._module: Any | None = None
        self._handles: dict[int, Any] = {}
        self._unavailable = False

    def sample(self, index: int) -> tuple[float | None, float | None]:
        """Utilization (0..1) and temperature in °C, or ``(None, None)``."""
        module = self._load()
        if module is None:
            return None, None
        try:  # pragma: no cover - needs a real NVML binding and driver
            handle = self._handles.get(index)
            if handle is None:
                handle = module.nvmlDeviceGetHandleByIndex(index)
                self._handles[index] = handle
            rates = module.nvmlDeviceGetUtilizationRates(handle)
            celsius = module.nvmlDeviceGetTemperature(handle, module.NVML_TEMPERATURE_GPU)
            return round(float(rates.gpu) / 100.0, 3), float(celsius)
        except Exception:
            self._handles.pop(index, None)
            return None, None

    def _load(self) -> Any | None:
        """Import and initialise NVML once, or remember that it is unusable."""
        if self._module is not None:
            return self._module
        if self._unavailable:
            return None
        try:
            module: Any = importlib.import_module("pynvml")
            module.nvmlInit()
        except Exception:
            logger.debug("NVML is unavailable; GPU utilization and temperature stay empty.")
            self._unavailable = True
            return None
        atexit.register(self._shutdown)
        self._module = module
        return module

    def _shutdown(self) -> None:
        """Release NVML at interpreter exit. Never raises."""
        module, self._module = self._module, None
        self._handles.clear()
        if module is None:
            return
        try:  # pragma: no cover - only runs at interpreter exit on an NVML host
            module.nvmlShutdown()
        except Exception:
            logger.debug("NVML shutdown failed; ignoring at teardown.")


_NVML = NvmlProbe()
"""Process-wide NVML probe. Replaced wholesale in tests; never re-created here."""


__all__ = [
    "NvmlProbe",
    "cuda_namespace",
    "device_stats",
    "reset_peak_memory",
    "resolve_torch_device",
]
