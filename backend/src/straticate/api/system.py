"""System endpoints: health and version."""

from fastapi import APIRouter

from straticate import __version__
from straticate.schemas import HealthStatus, VersionInfo

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> HealthStatus:
    """Report service liveness."""
    return HealthStatus(status="ok")


@router.get("/version")
async def version() -> VersionInfo:
    """Report the running application version."""
    return VersionInfo(version=__version__)
