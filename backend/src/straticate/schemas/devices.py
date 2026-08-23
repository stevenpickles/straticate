"""Logical compute devices exposed by the backend.

Raw PyTorch device objects never leak through application-level APIs.
"""

from pydantic import BaseModel, Field


class ComputeDevice(BaseModel):
    """A logical compute device usable for separation jobs.

    ``backend`` is an open set — ``cuda`` and ``cpu`` initially; later
    accelerators (``mps``, ``directml``, …) are added without API changes.
    """

    id: str = Field(description='Logical device ID, e.g. "cuda:0", "cpu".')
    backend: str = Field(description="Compute backend identifier (open set).")
    name: str = Field(description="Human-readable device name.")
    memory_total_bytes: int = Field(ge=0, description="Total device memory in bytes.")
