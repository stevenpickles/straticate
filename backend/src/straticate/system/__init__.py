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
- :func:`storage_report` — free/total bytes for the filesystem holding the
  models directory (feature 040), degrading to a documented "unknown" the same
  way the device probes degrade to "no devices". See
  :mod:`straticate.system.storage`.

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
from straticate.system.storage import (
    UNKNOWN_STORAGE,
    DiskUsageLike,
    DiskUsageReader,
    nearest_existing_dir,
    read_disk_usage,
    storage_report,
)

__all__ = [
    "CPU_BACKEND",
    "CPU_DEVICE_ID",
    "CUDA_BACKEND",
    "UNKNOWN_STORAGE",
    "ComputeDeviceProbe",
    "CudaDevicePropertiesLike",
    "CudaNamespaceLike",
    "DeviceDetector",
    "DeviceDetectorDep",
    "DiskUsageLike",
    "DiskUsageReader",
    "TorchCudaProbe",
    "TorchLoader",
    "TorchModuleLike",
    "cpu_device",
    "cpu_name",
    "get_device_detector",
    "load_torch",
    "nearest_existing_dir",
    "read_disk_usage",
    "storage_report",
    "total_system_memory_bytes",
]
