"""Compute device detection: logical devices, never PyTorch objects.

Design decision — **PyTorch is an optional runtime probe, not a dependency**
==========================================================================

ARCHITECTURE.md §14 requires normal CI to run without CUDA, a GPU, or
multi-gigabyte downloads. PyTorch is therefore deliberately deferred to
feature 026 (real inference) and is *not* declared in ``pyproject.toml`` by
this feature.

To still report CUDA devices when a real installation has torch available,
CUDA detection lives behind a **pluggable probe**
(:class:`ComputeDeviceProbe`). The bundled :class:`TorchCudaProbe` resolves
torch lazily via :func:`importlib.import_module` *inside* the probe call and
accesses it through the narrow :class:`TorchModuleLike` protocol, so:

- nothing in this package imports torch at module scope;
- with torch absent the probe yields no devices and the system reports CPU
  only — a normal, first-class, tested code path;
- when feature 026 adds the real torch dependency, the same probe starts
  reporting CUDA devices with **no API change** anywhere;
- tests inject a fake torch module (or a whole fake probe) and never need
  torch installed.

An NVML probe (utilization/temperature) is likewise optional and is *not*
implemented here — ARCHITECTURE.md §12 states basic operation must never
require NVML. Feature 019 may add one as another
:class:`ComputeDeviceProbe`-adjacent sampler.

Detection priority is 1) NVIDIA CUDA, 2) CPU. The CPU device is always
present and always last: accelerator probes are consulted in order, then
:func:`cpu_device` is appended unconditionally. A probe that raises logs a
warning and contributes no devices, so a misbehaving probe can never break
detection (or application startup).

For feature 019 (runtime telemetry)
-----------------------------------

This module owns the **static** device facts: ``id``, ``backend``, ``name``
and ``memory_total_bytes``. Resolve a job's device with
:meth:`DeviceDetector.get_device` (or :meth:`DeviceDetector.select_default_device`
when the job did not pin one) and copy those fields into
:class:`~straticate.schemas.GpuMetrics`; sample the *dynamic* fields
(allocated/peak VRAM, utilization, temperature) in the telemetry sampler
itself. Nothing here polls hardware after startup.
"""

import ctypes
import importlib
import logging
import os
import platform
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, Request

from straticate.errors import ApplicationError
from straticate.schemas import ComputeDevice

logger = logging.getLogger(__name__)

CPU_BACKEND = "cpu"
"""Backend identifier for the always-present CPU device."""

CUDA_BACKEND = "cuda"
"""Backend identifier for NVIDIA CUDA devices."""

CPU_DEVICE_ID = "cpu"
"""Logical device ID of the CPU device."""

_FALLBACK_CPU_NAME = "CPU"
"""Used when the platform reports no usable processor description."""


# --------------------------------------------------------------------------
# Probe interface
# --------------------------------------------------------------------------


class ComputeDeviceProbe(Protocol):
    """Detects the available devices of exactly one compute backend.

    Implementations must be cheap, synchronous, and side-effect free. They may
    raise: :class:`DeviceDetector` logs the failure and treats the backend as
    unavailable.
    """

    backend: str
    """Backend identifier the probe reports for (used in log messages)."""

    def detect(self) -> Sequence[ComputeDevice]:
        """Return the devices this backend currently exposes (possibly empty)."""
        ...


class CudaDevicePropertiesLike(Protocol):
    """The subset of ``torch.cuda.get_device_properties()`` we consume."""

    name: str
    total_memory: int


class CudaNamespaceLike(Protocol):
    """The subset of ``torch.cuda`` we consume."""

    def is_available(self) -> bool: ...

    def device_count(self) -> int: ...

    def get_device_properties(self, device: int) -> CudaDevicePropertiesLike: ...


class TorchModuleLike(Protocol):
    """The subset of the ``torch`` module we consume (read-only)."""

    @property
    def cuda(self) -> CudaNamespaceLike: ...


TorchLoader = Callable[[], TorchModuleLike | None]
"""Resolves the ``torch`` module, or ``None`` when it is unavailable."""


def load_torch() -> TorchModuleLike | None:
    """Import ``torch`` lazily, returning ``None`` when it is not installed.

    Uses :func:`importlib.import_module` rather than a static ``import torch``
    so that neither this module nor the type checker requires torch to be
    present (see the module docstring).
    """
    try:
        module = importlib.import_module("torch")
    except ImportError:
        logger.debug("PyTorch is not installed; CUDA devices will not be reported.")
        return None
    return cast(TorchModuleLike, module)


class TorchCudaProbe:
    """Reports NVIDIA CUDA devices through an optional PyTorch installation.

    Yields no devices — without raising — when torch is missing or reports no
    usable CUDA runtime. Feature 026 adds the real torch dependency; this
    probe then starts returning devices with no change to its API.

    Args:
        loader: Resolver for the torch module. Defaults to :func:`load_torch`;
            tests inject a fake module through it.
    """

    backend: str = CUDA_BACKEND

    def __init__(self, loader: TorchLoader | None = None) -> None:
        self._loader: TorchLoader = loader or load_torch

    def detect(self) -> Sequence[ComputeDevice]:
        """Return one :class:`ComputeDevice` per visible CUDA device."""
        torch = self._loader()
        if torch is None or not torch.cuda.is_available():
            return ()
        return tuple(
            self._describe(torch, index) for index in range(max(torch.cuda.device_count(), 0))
        )

    @staticmethod
    def _describe(torch: TorchModuleLike, index: int) -> ComputeDevice:
        """Map torch device properties onto the logical device contract."""
        properties = torch.cuda.get_device_properties(index)
        return ComputeDevice(
            id=f"{CUDA_BACKEND}:{index}",
            backend=CUDA_BACKEND,
            name=str(properties.name),
            memory_total_bytes=max(int(properties.total_memory), 0),
        )


# --------------------------------------------------------------------------
# CPU device
# --------------------------------------------------------------------------


def cpu_name() -> str:
    """Human-readable processor description, with graceful degradation.

    Tries :func:`platform.processor` (informative on Windows/macOS, often
    empty on Linux), then ``platform.uname().machine``, then a generic
    ``"CPU"`` — the value is descriptive only and never empty.
    """
    for candidate in (platform.processor(), platform.uname().machine):
        cleaned = candidate.strip()
        if cleaned:
            return cleaned
    return _FALLBACK_CPU_NAME


def _posix_total_memory_bytes() -> int:
    """Total RAM via ``sysconf`` (Linux/macOS); ``0`` where unsupported.

    Resolved with :func:`getattr` because ``os.sysconf`` does not exist on
    Windows — this keeps the module type-checking cleanly on every platform.
    """
    sysconf = cast(Callable[[str], int] | None, getattr(os, "sysconf", None))
    if sysconf is None:
        return 0
    return max(sysconf("SC_PAGE_SIZE") * sysconf("SC_PHYS_PAGES"), 0)


class _MemoryStatusEx(ctypes.Structure):
    """``MEMORYSTATUSEX`` for the Win32 ``GlobalMemoryStatusEx`` call."""

    dwLength: int
    dwMemoryLoad: int
    ullTotalPhys: int
    ullAvailPhys: int
    ullTotalPageFile: int
    ullAvailPageFile: int
    ullTotalVirtual: int
    ullAvailVirtual: int
    ullAvailExtendedVirtual: int

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_total_memory_bytes() -> int:
    """Total RAM via ``GlobalMemoryStatusEx``; ``0`` when unavailable.

    ``ctypes.WinDLL`` is resolved with :func:`getattr` so this module also
    type-checks (and imports) on non-Windows platforms.
    """
    windll_factory = cast(Callable[[str], Any] | None, getattr(ctypes, "WinDLL", None))
    if windll_factory is None:
        return 0
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    kernel32 = windll_factory("kernel32")
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0
    return max(int(status.ullTotalPhys), 0)


def total_system_memory_bytes() -> int:
    """Total installed system RAM in bytes, or ``0`` when undeterminable.

    Read through platform APIs (``sysconf`` on POSIX, ``GlobalMemoryStatusEx``
    on Windows) rather than a third-party dependency such as ``psutil`` — this
    is the only system statistic the backend needs and it does not justify a
    new package. Any failure degrades to ``0``, which the
    :class:`~straticate.schemas.ComputeDevice` contract documents as "unknown"
    for the CPU device; it never raises.
    """
    try:
        if os.name == "nt":
            return _windows_total_memory_bytes()
        return _posix_total_memory_bytes()
    except Exception:  # pragma: no cover - platform-specific failure path
        logger.warning("Could not determine total system memory; reporting 0.", exc_info=True)
        return 0


def cpu_device() -> ComputeDevice:
    """The always-available CPU device.

    ``memory_total_bytes`` is total system RAM, or ``0`` when the platform
    does not report it (see :func:`total_system_memory_bytes`).
    """
    return ComputeDevice(
        id=CPU_DEVICE_ID,
        backend=CPU_BACKEND,
        name=cpu_name(),
        memory_total_bytes=total_system_memory_bytes(),
    )


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------


class DeviceDetector:
    """Detects and caches the logical compute devices this machine offers.

    Devices do not change during a run, so detection happens once (at startup
    via :func:`straticate.main.create_app`) and the result is cached; call
    :meth:`refresh` to re-probe.

    Args:
        probes: Accelerator probes, highest priority first. Defaults to
            ``(TorchCudaProbe(),)``. The CPU device is **not** a probe: it is
            always appended last so a device list is never empty.
    """

    def __init__(self, probes: Sequence[ComputeDeviceProbe] | None = None) -> None:
        self._probes: tuple[ComputeDeviceProbe, ...] = (
            tuple(probes) if probes is not None else (TorchCudaProbe(),)
        )
        self._devices: tuple[ComputeDevice, ...] | None = None

    def devices(self) -> list[ComputeDevice]:
        """All detected devices, accelerators first and CPU last (cached)."""
        if self._devices is None:
            self._devices = self._detect()
        return list(self._devices)

    def refresh(self) -> list[ComputeDevice]:
        """Re-run every probe, replacing the cache, and return the result."""
        self._devices = self._detect()
        return list(self._devices)

    def select_default_device(self) -> ComputeDevice:
        """The preferred device: the first CUDA device if any, else CPU.

        Feature 015 uses this when
        :class:`~straticate.schemas.SeparationConfiguration` omits
        ``device_id``.
        """
        devices = self.devices()
        for device in devices:
            if device.backend == CUDA_BACKEND:
                return device
        return devices[-1]

    def get_device(self, device_id: str) -> ComputeDevice:
        """Look up one device by its logical ID.

        Raises:
            ApplicationError: ``device_not_found`` (404) when no detected
                device has that ID.
        """
        for device in self.devices():
            if device.id == device_id:
                return device
        raise ApplicationError(
            "device_not_found",
            f"No compute device with ID {device_id!r}.",
            status_code=404,
            detail={"device_id": device_id},
        )

    def _detect(self) -> tuple[ComputeDevice, ...]:
        """Run the accelerator probes, then append the CPU device."""
        detected: list[ComputeDevice] = []
        for probe in self._probes:
            detected.extend(self._probe_safely(probe))
        detected.append(cpu_device())
        return tuple(detected)

    @staticmethod
    def _probe_safely(probe: ComputeDeviceProbe) -> Sequence[ComputeDevice]:
        """Run one probe, downgrading any failure to a warning + no devices."""
        try:
            return probe.detect()
        except Exception:
            logger.warning(
                "Compute device probe for backend %r failed; reporting no %s devices.",
                probe.backend,
                probe.backend,
                exc_info=True,
            )
            return ()


def get_device_detector(request: Request) -> DeviceDetector:
    """FastAPI dependency: the application's :class:`DeviceDetector`.

    The instance is created and warmed by
    :func:`straticate.main.create_app` and stored on
    ``app.state.device_detector``.
    """
    return cast(DeviceDetector, request.app.state.device_detector)


DeviceDetectorDep = Annotated[DeviceDetector, Depends(get_device_detector)]
"""Annotated dependency alias for endpoints needing device detection."""
