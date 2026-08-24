"""The catalog model → :class:`~straticate.inference.base.Separator` seam.

Feature 015 has to turn "the model behind mode *vocals*, tier *balanced*" into
something that can actually separate audio. That mapping is the one place where
a model *architecture* is allowed to select an implementation — and it is
deliberately keyed **by architecture, not by model ID**:

- adding another model of an already-implemented architecture to
  ``models/catalog.json`` is a data edit, never a code change;
- a new inference backend (feature 026 and beyond) registers exactly one
  builder under its own architecture name and needs to touch nothing else;
- the API layer never names an architecture, a mode or a stem — it hands the
  registry a catalog :class:`~straticate.schemas.Model` and gets a separator
  (AGENTS.md principles 1 and 6).

The descriptor a separator advertises is built **from the catalog entry**
(:func:`separator_info_from_model`), so the catalog stays the single source of
truth for stems, sample rate, version and display name. Built-in constants such
as :data:`~straticate.inference.fake.FAKE_VOCALS_INFO` exist only so the fake
engine and the catalog file can be asserted consistent — they are never
consulted on the resolution path.

Instances are cached one per model ID: constructing a separator is expensive
for a real backend (it loads weights), the job manager runs one job at a time
(ARCHITECTURE.md §6), and a separator's own contract is one separation at a
time (feature 014) — so a shared, lazily created instance per model is exactly
right.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS
from straticate.errors import ApplicationError
from straticate.inference.base import Separator, SeparatorInfo
from straticate.inference.fake import (
    DEFAULT_CHUNK_DELAY_SECONDS,
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_FADE_SECONDS,
    DEFAULT_FAKE_DEVICE,
    DEFAULT_MODEL_LOAD_SECONDS,
    FAKE_ARCHITECTURE,
    FakeDeviceProfile,
    FakeSeparator,
)
from straticate.schemas.models import Model

SeparatorBuilder = Callable[[Model], Separator]
"""Constructs the separator that runs one catalog model.

Called at most once per model ID (:meth:`SeparatorRegistry.get` caches the
result). Implementations must not assume anything about the model beyond what
:class:`~straticate.schemas.Model` carries.
"""


def separator_info_from_model(model: Model) -> SeparatorInfo:
    """Project a catalog model onto the descriptor its separator advertises.

    The catalog is authoritative: stems, sample rate, version, display name and
    separation mode all come from the manifest entry, never from a constant
    baked into an implementation module.
    """
    return SeparatorInfo(
        model_id=model.id,
        display_name=model.display_name,
        architecture=model.architecture,
        version=model.version,
        separation_mode=model.separation_mode,
        stems=tuple(model.stems),
        sample_rate=model.sample_rate,
    )


def fake_separator_builder(
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    chunk_delay_seconds: float = DEFAULT_CHUNK_DELAY_SECONDS,
    model_load_seconds: float = DEFAULT_MODEL_LOAD_SECONDS,
    fade_seconds: float = DEFAULT_FADE_SECONDS,
    device: FakeDeviceProfile | None = DEFAULT_FAKE_DEVICE,
    ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
) -> SeparatorBuilder:
    """Build the :data:`SeparatorBuilder` for the ``fake`` architecture.

    Every catalog model whose ``architecture`` is
    :data:`~straticate.inference.fake.FAKE_ARCHITECTURE` is served by a
    :class:`~straticate.inference.fake.FakeSeparator` configured from the
    catalog entry, so a third fake model needs no code change at all.

    The tuning arguments are passed straight through to ``FakeSeparator``.
    Production keeps its defaults — the per-chunk delay is what makes the M1
    demo's progress visibly real-time — while tests inject a builder with
    ``chunk_delay_seconds=0.0`` and ``model_load_seconds=0.0`` so a full job
    runs as fast as the machine allows.
    """

    def build(model: Model) -> Separator:
        return FakeSeparator(
            separator_info_from_model(model),
            chunk_seconds=chunk_seconds,
            chunk_delay_seconds=chunk_delay_seconds,
            model_load_seconds=model_load_seconds,
            fade_seconds=fade_seconds,
            device=device,
            ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        )

    return build


def default_separator_builders(
    *, ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS
) -> Mapping[str, SeparatorBuilder]:
    """The builders a :class:`SeparatorRegistry` starts with.

    Today: the fake engine only. Feature 026 adds its own architecture here (or
    registers it with :meth:`SeparatorRegistry.register`); nothing outside this
    module learns the architecture's name.

    ``ffmpeg_timeout_seconds`` is the one application setting a separator needs:
    :func:`straticate.main.create_app` passes
    ``Settings.ffmpeg_timeout_seconds`` here so an application built with
    explicit settings really governs its subprocesses. It is a *builder*
    argument rather than a registry one so the registry keeps knowing nothing
    about how any particular engine decodes audio.
    """
    return MappingProxyType(
        {FAKE_ARCHITECTURE: fake_separator_builder(ffmpeg_timeout_seconds=ffmpeg_timeout_seconds)}
    )


class SeparatorRegistry:
    """Resolves catalog models to separator instances, caching one per model.

    Args:
        builders: Architecture → builder mapping. Defaults to
            :func:`default_separator_builders`; tests inject fast (zero-delay)
            builders through this argument.
    """

    __slots__ = ("_builders", "_instances")

    def __init__(self, builders: Mapping[str, SeparatorBuilder] | None = None) -> None:
        source = default_separator_builders() if builders is None else builders
        self._builders: dict[str, SeparatorBuilder] = dict(source)
        self._instances: dict[str, Separator] = {}

    @property
    def architectures(self) -> frozenset[str]:
        """The architectures this registry can build a separator for."""
        return frozenset(self._builders)

    def register(self, architecture: str, builder: SeparatorBuilder) -> None:
        """Register (or replace) the builder for ``architecture``.

        Replacing a builder does not evict separators already created from the
        previous one — construct a fresh registry if that matters.
        """
        self._builders[architecture] = builder

    def get(self, model: Model) -> Separator:
        """Return the separator for ``model``, creating it on first use.

        Raises:
            ApplicationError: ``separator_unavailable`` (501) when no builder
                is registered for the model's architecture — the model is
                catalogued but this build has no implementation able to run it.
        """
        cached = self._instances.get(model.id)
        if cached is not None:
            return cached
        builder = self._builders.get(model.architecture)
        if builder is None:
            raise ApplicationError(
                "separator_unavailable",
                f"No separator implementation is available for model {model.id!r} "
                f"(architecture {model.architecture!r}).",
                status_code=501,
                detail={"model_id": model.id, "architecture": model.architecture},
            )
        separator = builder(model)
        self._instances[model.id] = separator
        return separator


__all__ = [
    "SeparatorBuilder",
    "SeparatorRegistry",
    "default_separator_builders",
    "fake_separator_builder",
    "separator_info_from_model",
]
