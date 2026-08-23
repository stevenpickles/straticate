"""Logical separation models and user-facing separation modes.

``Model`` mirrors the model manifest (``models/schemas/model-manifest.schema.json``)
fields that are part of the API surface. ``SeparationMode`` is what the frontend
renders — derived from model capabilities, never hardcoded client-side.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class QualityTier(StrEnum):
    """User-facing quality tiers a model may back within its separation mode.

    Users choose a tier, never an architecture or inference parameters
    (ARCHITECTURE.md §9). **Declaration order is presentation order** — cheapest
    and fastest first — and is what orders
    :attr:`SeparationMode.quality_options`.
    """

    FAST = "fast"
    BALANCED = "balanced"
    HIGH_QUALITY = "high_quality"


class ModelRequirements(BaseModel):
    """Resource requirements advertised by a model manifest."""

    recommended_vram_mb: int | None = Field(
        default=None, ge=0, description="Recommended GPU VRAM in MiB; null when not specified."
    )
    minimum_ram_mb: int | None = Field(
        default=None, ge=0, description="Minimum system RAM in MiB; null when not specified."
    )


class Model(BaseModel):
    """A logical separation model from the catalog.

    ``architecture`` is an open set (e.g. ``mel_band_roformer``, ``mdx``,
    ``mdxc``, ``demucs``, ``fake``); application code never branches on it
    outside the inference package.
    """

    id: str = Field(description='Stable logical model ID, e.g. "vocals-hq-001".')
    display_name: str = Field(description="Human-readable model name.")
    architecture: str = Field(description="Implementation family (open set).")
    version: str = Field(description="Model version string.")
    separation_mode: str = Field(description='Logical mode ID this model serves, e.g. "vocals".')
    quality_tier: QualityTier | None = Field(
        default=None,
        description=(
            "User-facing quality tier this model backs within its separation mode; "
            'null means "balanced". Unique per separation mode.'
        ),
    )
    stems: list[str] = Field(min_length=2, description="Stem names this model produces.")
    sample_rate: int = Field(ge=8000, description="Native sample rate in Hz.")
    requirements: ModelRequirements = Field(
        default_factory=ModelRequirements, description="Resource requirements."
    )
    capabilities: dict[str, bool] = Field(
        description="Compute backends this model supports (open set of backend IDs)."
    )


class QualityOption(BaseModel):
    """A user-facing quality tier mapping to a concrete model.

    ``id`` carries a :class:`QualityTier` value; it is what a job request's
    ``quality_id`` names, and it identifies exactly one model within a mode.
    """

    id: str = Field(description='Quality tier ID, e.g. "fast", "high_quality".')
    display_name: str = Field(description="Human-readable tier name.")
    model_id: str = Field(description="ID of the model backing this tier.")


class SeparationMode(BaseModel):
    """A user-facing separation mode derived from model capabilities."""

    id: str = Field(description='Mode ID, e.g. "vocals", "standard_stems".')
    display_name: str = Field(description="Human-readable mode name.")
    stems: list[str] = Field(description="Stem names produced in this mode.")
    quality_options: list[QualityOption] = Field(description="Available quality tiers.")
