"""System endpoints: health and version."""

from fastapi import APIRouter

from straticate import __version__

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Report service liveness."""
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict[str, str]:
    """Report the running application version."""
    return {"version": __version__}
