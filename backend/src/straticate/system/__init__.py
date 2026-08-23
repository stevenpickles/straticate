"""Host system capabilities: compute device detection.

Public surface (consumed by features 015/019/026):

- :class:`DeviceDetector` — detects, caches, and looks up the logical
  :class:`~straticate.schemas.ComputeDevice` list (CUDA first, CPU last).
- :class:`ComputeDeviceProbe` — the per-backend detection seam; feature 026
  plugs real PyTorch in through the bundled :class:`TorchCudaProbe` simply by
  making ``torch`` installable, with no API change.
- :func:`cpu_device` / :func:`cpu_name` / :func:`total_system_memory_bytes` —
  the always-available CPU fallback and its statistics.
- :func:`get_device_detector` / :data:`DeviceDetectorDep` — FastAPI
  dependency accessors.

PyTorch is **not** a dependency of this package; see
:mod:`straticate.system.devices` for the rationale.
"""

from straticate.system.devices import (
    CPU_BACKEND,
    CPU_DEVICE_ID,
    CUDA_BACKEND,
    ComputeDeviceProbe,
    CudaDevicePropertiesLike,
    CudaNamespaceLike,
    DeviceDetector,
    DeviceDetectorDep,
    TorchCudaProbe,
    TorchLoader,
    TorchModuleLike,
    cpu_device,
    cpu_name,
    get_device_detector,
    load_torch,
    total_system_memory_bytes,
)

__all__ = [
    "CPU_BACKEND",
    "CPU_DEVICE_ID",
    "CUDA_BACKEND",
    "ComputeDeviceProbe",
    "CudaDevicePropertiesLike",
    "CudaNamespaceLike",
    "DeviceDetector",
    "DeviceDetectorDep",
    "TorchCudaProbe",
    "TorchLoader",
    "TorchModuleLike",
    "cpu_device",
    "cpu_name",
    "get_device_detector",
    "load_torch",
    "total_system_memory_bytes",
]
