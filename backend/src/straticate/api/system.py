"""System endpoints: health, version, compute devices, and storage."""

import asyncio

from fastapi import APIRouter

from straticate import __version__
from straticate.api.audio import SettingsDep
from straticate.schemas import ComputeDevice, HealthStatus, StorageReport, VersionInfo
from straticate.system import DeviceDetectorDep, storage_report

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


@router.get("/system/storage")
async def read_storage(settings: SettingsDep) -> StorageReport:
    """Report free and total bytes for the filesystem holding the models directory.

    This is the figure a client needs before offering an install: the weights
    are written by **this** process, to ``Settings.models_dir``, on **this**
    machine — a disk the browser cannot see (``navigator.storage.estimate()``
    describes the page origin's quota inside the browser profile, which is a
    different number about a different disk).

    Read fresh on every request rather than cached: free space changes
    constantly, and the underlying call is one syscall. Unlike
    ``/system/devices`` there is nothing here that could be probed once at
    startup and still be true.

    **It runs in a worker thread.** ``stat`` and ``shutil.disk_usage`` are
    filesystem calls, and on an unresponsive network mount — exactly the case
    this feature's warn-rather-than-refuse reasoning anticipates — they block
    for as long as the mount does. On the event loop that would stall *every*
    REST request, the feature 013 WebSocket hub and job progress delivery
    behind one `stat`, which is what AGENTS.md principle 4 and ARCHITECTURE.md
    §14 forbid; features 022 and 025 offload their blocking work for the same
    reason. Caching would also have hidden it, and caching is the wrong answer
    here (see above), so the read is offloaded instead.

    :func:`asyncio.to_thread` is not cancellable, which costs nothing here:
    this is a pure read that writes no state, so a client that disconnects
    mid-request simply leaves a worker thread to finish a ``stat`` and discard
    the answer. (025's export path has to *shield* its offloaded work because
    that work publishes an artifact; nothing here does.)

    **Both fields are ``null`` when the host cannot answer** — a models
    directory whose whole path is missing, a permissions failure, a filesystem
    the platform has no answer for. That is a documented state, not an error:
    the response is still ``200``, and a client renders "unknown" rather than a
    failure (see :mod:`straticate.system.storage`). ``free_bytes: 0`` says
    something quite different — a full disk — which is why unknown is not
    spelled ``0`` here the way an unknown device memory total is.
    """
    return await asyncio.to_thread(storage_report, settings.models_dir)
