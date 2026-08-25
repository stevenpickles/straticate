"""Logical separation models and user-facing separation modes.

``Model`` mirrors the model manifest (``models/schemas/model-manifest.schema.json``)
fields that are part of the API surface. ``SeparationMode`` is what the frontend
renders — derived from model capabilities, never hardcoded client-side.

``Model`` also carries the model's **installation state** (feature 025): being
in the catalog and having weights on disk are different facts, and a client that
cannot tell "offered" from "ready" cannot offer to install anything. The state
lives on ``Model`` rather than on a sibling resource because every place a model
is presented is a place the distinction matters, and because ``GET /models`` and
``GET /models/{model_id}`` are already the two routes a client reads models
from — a sibling resource would mean a second fetch per model to answer a
question about the model it just fetched.
"""

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from straticate.schemas.common import ErrorInfo
from straticate.schemas.stems import StemName


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
    """Resource requirements advertised by a model manifest.

    Every figure here is **advisory**: nothing in the application compares them
    against the host, and a job is never refused for failing one. They exist so
    that a user can judge their hardware before committing to a download the
    size of a model checkpoint.

    The two VRAM figures say different things on purpose. ``minimum_vram_mb`` is
    the floor — below it, do not attempt the model on that backend — while
    ``recommended_vram_mb`` is the comfortable figure: a full-length track on a
    card that is also driving a display. Both are measured **whole-device**
    peaks, because the CUDA context and the caching allocator's reservation are
    part of what a card must have free, not only the tensors. Both depend on the
    model's chunking — but, since feature 038 streamed the overlap-add onto the
    host, no longer on how long the track is — so the feature document that sets
    them records the parameters they were measured at.
    """

    recommended_vram_mb: int | None = Field(
        default=None,
        ge=0,
        description=(
            "GPU VRAM in MiB recommended for comfortable use; null when not specified. "
            "Advisory: nothing is refused for failing it."
        ),
    )
    minimum_vram_mb: int | None = Field(
        default=None,
        ge=0,
        description=(
            "GPU VRAM in MiB below which this model should not be attempted; null when "
            "not specified. Advisory: nothing is refused for failing it."
        ),
    )
    minimum_ram_mb: int | None = Field(
        default=None, ge=0, description="Minimum system RAM in MiB; null when not specified."
    )


class ModelInstallState(StrEnum):
    """Whether a catalogued model's weights are on disk, and how that is going.

    A model with no downloadable artifact — every built-in separator, the fake
    ones included — is :attr:`INSTALLED` by definition: it needs no weights, so
    it is never presented as something to download.

    Transitions (feature 025)::

        available ──install──▶ downloading ──verified──▶ installed
             ▲                      │                        │
             │                      └──failed──▶ failed      │
             └──────────── remove ◀─────────────────────────-┘

    ``failed`` is a *terminal report*, not a resting place: it says the last
    install attempt did not produce weights, and the failure's code and message
    are in :attr:`ModelInstallation.error`. Nothing is on disk in that state —
    an incomplete or hash-mismatched artifact is never installed
    (ARCHITECTURE.md §9) — so starting another install clears it.
    """

    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    INSTALLED = "installed"
    FAILED = "failed"


class ModelLicensing(BaseModel):
    """Licence and permission terms declared by a model manifest.

    Surfaced on :class:`Model` so a user can read a model's terms **before**
    installing its weights, which is the only moment the terms can still change
    the decision. Every field is optional: a manifest may declare as much or as
    little as its upstream publishes, and ``null`` means "not declared", never
    "not permitted".
    """

    code_license: str | None = Field(
        default=None, description="SPDX-style licence of the implementation code; null if unknown."
    )
    weights_license: str | None = Field(
        default=None, description="Licence of the weights themselves; null if unknown."
    )
    redistribution_permitted: bool | None = Field(
        default=None, description="Whether the weights may be redistributed; null if unknown."
    )
    commercial_use_permitted: bool | None = Field(
        default=None, description="Whether commercial use is permitted; null if unknown."
    )
    attribution: str | None = Field(
        default=None, description="Attribution text the licence requires; null if none."
    )


class ModelInstallation(BaseModel):
    """Installation state and download progress for one model.

    **Progress is served here rather than pushed as a WebSocket event.** The
    reasoning is written up in ``docs/features/025-model-download-manager.md``;
    in short, ARCHITECTURE.md §11's rule is that REST is the source of truth for
    reconnect and refresh, and an install is a rare, user-initiated,
    coarse-grained operation whose state a client needs on every plain
    ``GET /models`` anyway.

    The defaults describe a model that needs no weights — which is exactly what
    a :class:`Model` built from a manifest with no ``artifact`` block is.
    """

    state: ModelInstallState = Field(
        default=ModelInstallState.INSTALLED, description="Current installation state."
    )
    requires_download: bool = Field(
        default=False,
        description=(
            "Whether this model has a downloadable weights artifact at all. False for "
            "built-in separators, which are always installed and can never be removed."
        ),
    )
    total_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Size of the weights artifact in bytes; null when there is none.",
    )
    downloaded_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Bytes received so far; null unless an install is running.",
    )
    progress: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Download progress in [0, 1] (downloaded_bytes / total_bytes); "
            "null unless an install is running."
        ),
    )
    error: ErrorInfo | None = Field(
        default=None,
        description=(
            "Why the last install attempt failed; null unless state is `failed`. "
            "Carries the same shape as the REST error envelope's `error`."
        ),
    )


class Model(BaseModel):
    """A logical separation model from the catalog.

    ``architecture`` is an open set (e.g. ``mel_band_roformer``, ``mdx``,
    ``mdxc``, ``demucs``, ``fake``); application code never branches on it
    outside the inference package. ``development_only`` exists precisely so
    that it does not have to: "this is a fixture, not a separator" is a fact
    about the *entry*, declared by the manifest, not something to be guessed
    from an architecture name (feature 032).

    ``stems`` carries the constraints the separation engine has always
    enforced: each name matches
    :data:`~straticate.schemas.stems.STEM_NAME_REGEX`, and the list holds no
    duplicates. They live here so a malformed catalog fails **at load time**
    (feature 010's stated principle) instead of loading cleanly, serving
    ``GET /models`` and ``GET /separation-modes``, and then raising an
    unhandled ``ValueError`` on the first job created for that mode.
    """

    id: str = Field(description='Stable logical model ID, e.g. "vocals-hq-001".')
    display_name: str = Field(description="Human-readable model name.")
    architecture: str = Field(description="Implementation family (open set).")
    version: str = Field(description="Model version string.")
    development_only: bool = Field(
        default=False,
        description=(
            "Whether this is a development fixture — an entry that exists to exercise the "
            "application (CI, the test suite, the end-to-end tier) and does not perform real "
            "separation. False for every model a user is normally offered: such entries are "
            "excluded from the catalog unless the server enables them, so a client only ever "
            "sees `true` here on a server that deliberately opted in."
        ),
    )
    separation_mode: str = Field(description='Logical mode ID this model serves, e.g. "vocals".')
    quality_tier: QualityTier | None = Field(
        default=None,
        description=(
            "User-facing quality tier this model backs within its separation mode; "
            'null means "balanced". Unique per separation mode.'
        ),
    )
    stems: list[StemName] = Field(
        min_length=2, description="Stem names this model produces; unique, in output order."
    )
    sample_rate: int = Field(ge=8000, description="Native sample rate in Hz.")
    requirements: ModelRequirements = Field(
        default_factory=ModelRequirements, description="Resource requirements."
    )
    capabilities: dict[str, bool] = Field(
        description="Compute backends this model supports (open set of backend IDs)."
    )
    licensing: ModelLicensing | None = Field(
        default=None,
        description=(
            "Licence and permission terms from the manifest; null when the manifest declares none."
        ),
    )
    installation: ModelInstallation = Field(
        default_factory=ModelInstallation,
        description=(
            "Whether this model's weights are on disk, and the progress of a running "
            "install. The default describes a model that needs no weights."
        ),
    )

    @field_validator("stems")
    @classmethod
    def _stems_are_unique(cls, stems: list[str]) -> list[str]:
        """Reject a repeated stem name.

        A stem name identifies one output file and one URL, so a duplicate is
        not a harmless redundancy: two stems would write to and be served from
        the same path.
        """
        duplicates = sorted({name for name in stems if stems.count(name) > 1})
        if duplicates:
            raise ValueError(f"stem names must be unique; repeated: {', '.join(duplicates)}")
        return stems


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
