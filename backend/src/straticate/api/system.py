"""System endpoints: health, version, and compute devices."""

from fastapi import APIRouter

from straticate import __version__
from straticate.schemas import ComputeDevice, HealthStatus, VersionInfo
from straticate.system import DeviceDetectorDep

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> HealthStatus:
    """Report service liveness."""
    return HealthStatus(status="ok")


@router.get("/version")
async def version() -> VersionInfo:
    """Report the running application version."""
    return VersionInfo(version=__version__)


@router.get("/system/devices")
async def list_devices(detector: DeviceDetectorDep) -> list[ComputeDevice]:
    """List the logical compute devices available for separation jobs.

    Detected once at startup and cached — devices do not change during a run.
    NVIDIA CUDA devices come first (when a CUDA-capable PyTorch installation
    is present); the CPU device is always last and always present, so the list
    is never empty. ``memory_total_bytes`` is ``0`` when the host does not
    report a total (CPU only, on exotic platforms).
    """
    return detector.devices()
