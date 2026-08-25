"""The model catalog and the separation modes derived from it.

The catalog file (``models/catalog.json``, whose entries follow
``models/schemas/model-manifest.schema.json``) is the single source of truth for
what Straticate can separate. It is loaded and fully validated at application
startup: a malformed catalog raises :class:`ModelCatalogError` rather than
degrading into an empty list of choices.

Derivation rules
----------------

Models are grouped by ``separation_mode``; nothing about a mode is hardcoded in
application code.

*Stems* come from the models themselves. Every model in a mode must advertise
exactly the same stems — the stem list is what the UI promises the user *before*
a quality tier picks a concrete model, so a disagreement is a catalog error.

*Quality options* are the models of a mode, one per :class:`QualityTier`. A
model that declares no ``quality_tier`` counts as ``balanced``, so a mode served
by a single model still offers exactly one sensible choice. Two models in one
mode may not claim the same tier: the tier ID is what a job request's
``quality_id`` selects, so it must identify exactly one model. Options are
ordered by :class:`QualityTier` declaration order (``fast`` → ``balanced`` →
``high_quality``).

*Display names* are data, not code. A mode label may be supplied by the catalog
file's optional ``separation_modes`` table::

    {"catalog_version": 1,
     "separation_modes": {"vocals": {"display_name": "Vocal Isolation"}},
     "models": [...]}

Without an entry the ID is humanized (``standard_stems`` → ``Standard Stems``).
Tier labels are humanized the same way (``high_quality`` → ``High Quality``),
which is why no label table exists anywhere in this package.

*Development fixtures* are dropped before any of that (feature 032). A manifest
entry may declare ``development_only``, meaning it exists to exercise the
application rather than to separate audio, and such an entry is not part of the
catalog unless :class:`ModelCatalog` is built with ``include_development=True``
(from :attr:`straticate.config.Settings.include_development_models`). The marker
is on the *manifest*, deliberately: "fixture" is a fact the catalog author
states, not something inferred from ``architecture`` — an open set nothing
outside :mod:`straticate.inference` may branch on (ARCHITECTURE.md §1). Because
the filter runs here, at the single point the catalog is constructed, every
consumer — the model routes, the installer, ``POST /jobs``'s mode/quality
resolution — is consistent by construction, and a mode left with no models
disappears instead of being served empty.

Architecture-specific manifest fields (``default_inference_parameters``) are
absent from :class:`~straticate.schemas.Model`, so they can never reach the API:
users choose modes and quality tiers, never inference parameters. They are not
*discarded*, though — feature 026 needs a checkpoint's own hyperparameters to
build a network that checkpoint will load into, and ARCHITECTURE.md §9 provides
the field precisely so that configuration is data rather than code. They travel
on :class:`CatalogEntry`, beside the ``artifact`` block and under the same rule:
private to the machinery, invisible to every response.

The manifest's ``artifact`` block (feature 025) is *kept*, but off the API
surface: it holds the download URL and the pinned SHA-256, which are the model
manager's business and nobody else's. It travels in :class:`CatalogEntry`
alongside the public :class:`~straticate.schemas.Model`, so a route that returns
``Model`` cannot leak it by accident. ``licensing`` is public — a user should be
able to read a model's terms before installing its weights — and so it lives on
``Model`` itself.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from straticate.errors import ApplicationError
from straticate.models.layout import validate_model_id
from straticate.schemas import (
    Model,
    ModelInstallation,
    ModelInstallState,
    QualityOption,
    QualityTier,
    SeparationMode,
)

CATALOG_FILENAME = "catalog.json"
"""Name of the catalog file inside ``Settings.models_dir``."""

_DEFAULT_QUALITY_TIER = QualityTier.BALANCED
"""Tier assigned to a model whose manifest declares none."""


class ModelCatalogError(RuntimeError):
    """The model catalog is missing, unreadable, invalid, or inconsistent.

    Raised during loading, which happens at application startup. Failing here is
    deliberate: silently serving a partial catalog would silently remove
    separation choices from the user interface.
    """


def _humanize(identifier: str) -> str:
    """Turn a snake_case identifier into a display label.

    ``standard_stems`` → ``Standard Stems``; ``high_quality`` → ``High Quality``.
    """
    return identifier.replace("_", " ").title()


def _describe(error: ValidationError) -> str:
    """Render a pydantic validation error as a single readable line."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )


class ModelArtifact(BaseModel):
    """The downloadable weights artifact declared by a model manifest.

    **Deliberately not in** :mod:`straticate.schemas`: it is never part of a
    response. ``download_url`` and ``sha256`` are how the model manager fetches
    and verifies weights, not something a user chooses between, and the API
    surface stays "modes and quality tiers" (ARCHITECTURE.md §1, §9).

    The pinned ``sha256`` is enforced, never trusted: third-party checkpoint
    hosts get renamed and taken down, and a 404 page served in place of weights
    must fail loudly rather than install something plausible-looking.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    download_url: str = Field(min_length=1)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("download_url")
    @classmethod
    def _is_fetchable(cls, url: str) -> str:
        """Reject anything the downloader is not willing to fetch.

        ``file:`` would turn a catalog edit into an arbitrary-file read and
        every other scheme is simply not implemented, so the check lives here —
        at load time, where a bad catalog already fails loudly — rather than in
        the download path where it would be a runtime surprise.
        """
        scheme = urlsplit(url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError(f"download_url must be an http(s) URL, got scheme {scheme!r}")
        return url


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One catalog entry: its public projection plus its private artifact.

    The split is the point. :attr:`model` is what any route may return;
    :attr:`artifact` is what the model manager needs and no response ever
    carries.
    """

    model: Model
    """The API-facing model, carrying the *baseline* installation state."""

    artifact: ModelArtifact | None
    """Weights to download, or ``None`` for a built-in (always-installed) model."""

    default_inference_parameters: Mapping[str, Any] | None = None
    """Architecture-specific defaults, opaque to everything but the separator.

    Whatever the manifest declared, unvalidated and uninterpreted: only the
    implementation registered for the model's ``architecture`` knows what a
    ``stft_hop_length`` is, and this module refuses to learn (ARCHITECTURE.md
    §1). ``None`` for a model that declares none — every fake model, and any
    architecture whose separator needs no tuning.
    """


def _baseline_installation(artifact: ModelArtifact | None) -> ModelInstallation:
    """The installation state of a model nothing has yet tried to install.

    No artifact means the model is ``installed`` permanently and can never be
    downloaded or removed. An artifact means ``available`` — weights absent —
    with the manifest's size carried so a client can say how large the download
    is *before* starting it.
    """
    if artifact is None:
        return ModelInstallation()
    return ModelInstallation(
        state=ModelInstallState.AVAILABLE,
        requires_download=True,
        total_bytes=artifact.size_bytes,
    )


def _as_entry(item: Model | CatalogEntry, source: str) -> CatalogEntry:
    """Normalize a constructor argument into a :class:`CatalogEntry`.

    A bare :class:`~straticate.schemas.Model` has no artifact. A
    :class:`_ManifestEntry` is projected down to a plain ``Model`` so the
    artifact cannot ride along inside the public object, and every entry's
    baseline installation state is (re)derived here rather than trusted from
    whatever the caller passed.

    Raises:
        ModelCatalogError: The model ID could not be used as a directory name,
            so its weights could never be stored. Caught here, at load, rather
            than at the first install.
    """
    parameters: Mapping[str, Any] | None = None
    if isinstance(item, CatalogEntry):
        model, artifact = item.model, item.artifact
        parameters = item.default_inference_parameters
    elif isinstance(item, _ManifestEntry):
        artifact = item.artifact
        parameters = item.default_inference_parameters
        model = Model.model_validate(
            item.model_dump(exclude={"artifact", "default_inference_parameters"})
        )
    else:
        model, artifact = item, None
    try:
        validate_model_id(model.id)
    except ValueError as exc:
        raise ModelCatalogError(f"{source}: {exc}.") from exc
    return CatalogEntry(
        model=model.model_copy(update={"installation": _baseline_installation(artifact)}),
        artifact=artifact,
        default_inference_parameters=parameters,
    )


class _ModeLabel(BaseModel):
    """Catalog-supplied display label for one separation mode."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1)


class _ManifestEntry(Model):
    """On-disk shape of one catalog entry: :class:`Model` plus its private blocks.

    Entry fields outside all of these (``schema_version``, …) are accepted and
    ignored. ``artifact`` and ``default_inference_parameters`` are declared here
    only so they survive onto :class:`CatalogEntry`; they are stripped before
    the public :class:`Model` is built, so no route can return them.
    """

    artifact: ModelArtifact | None = None
    default_inference_parameters: dict[str, Any] | None = None


class _CatalogFile(BaseModel):
    """On-disk shape of ``models/catalog.json``."""

    catalog_version: int = Field(ge=1)
    models: list[_ManifestEntry]
    separation_modes: dict[str, _ModeLabel] = Field(default_factory=dict)


class ModelCatalog:
    """An immutable, validated view of the logical models Straticate offers.

    Modes are derived once, at construction, so an inconsistent catalog fails at
    startup rather than on the first request.

    **The installation state a catalog serves is a baseline, not a reading of
    the disk.** A model with no artifact is ``installed`` for good; one with an
    artifact starts at ``available``. The catalog is loaded once, at startup, so
    it is the wrong place to answer a question whose answer changes while the
    process runs — :class:`straticate.models.installer.ModelInstaller` overlays
    the live state, and the model routes serve *that*.

    **Development fixtures are filtered out here, once** (feature 032). An entry
    whose manifest declares ``development_only`` is not a separator — today it
    is the comb filter of ARCHITECTURE.md §8 — and unless ``include_development``
    says otherwise it is absent from :meth:`list_models`, :meth:`list_entries`,
    :meth:`get_entry` and every derived mode. Filtering at the single place the
    catalog is built, rather than at each route, is what makes "a user cannot
    select or run a fixture" a property of the object every surface already
    shares: the model routes, the installer, and ``POST /jobs``'s mode/quality
    resolution all read *this*, so none of them needs to know the rule exists.
    A mode whose every model is filtered out is not derived at all, so no empty,
    unselectable mode is ever served.

    The catalog is nonetheless **validated as written**, hidden entries
    included: whether a fixture is served must never decide whether a malformed
    catalog is detected, or CI (fixtures on) and a user's machine (fixtures off)
    would disagree about which files are valid.

    Args:
        models: The catalog's entries, in presentation order. A bare
            :class:`~straticate.schemas.Model` is taken to have no artifact,
            which is what every built-in separator is.
        mode_display_names: Optional mode ID → label overrides; missing modes
            fall back to a humanized ID.
        source: Human-readable origin used in error messages (a file path when
            loaded from disk).
        include_development: Whether entries marked ``development_only`` are
            part of the catalog. Defaults to ``False`` — the user-facing
            behaviour; :attr:`straticate.config.Settings.include_development_models`
            (``STRATICATE_INCLUDE_DEVELOPMENT_MODELS``) is what turns it on for
            the test suite, CI and the end-to-end tier.

    Raises:
        ModelCatalogError: On an unusable or duplicate model ID, models of one
            mode disagreeing on stems, or two models of one mode claiming the
            same quality tier.
    """

    def __init__(
        self,
        models: Sequence[Model | CatalogEntry],
        *,
        mode_display_names: Mapping[str, str] | None = None,
        source: str = "<memory>",
        include_development: bool = False,
    ) -> None:
        self._source = source
        declared = [_as_entry(item, source) for item in models]
        seen: set[str] = set()
        for entry in declared:
            if entry.model.id in seen:
                raise ModelCatalogError(f"{source}: duplicate model ID {entry.model.id!r}.")
            seen.add(entry.model.id)
        labels = dict(mode_display_names or {})
        # Derive over *every* declared entry first and throw the result away:
        # this is the consistency check (stems agreement, tier uniqueness), and
        # it has to see the file as written so a broken fixture cannot hide
        # behind being filtered out. See the class docstring.
        self._derive_modes([entry.model for entry in declared], labels)
        self._entries: list[CatalogEntry] = [
            entry for entry in declared if include_development or not entry.model.development_only
        ]
        self._models: list[Model] = [entry.model for entry in self._entries]
        self._by_id: dict[str, CatalogEntry] = {entry.model.id: entry for entry in self._entries}
        self._modes: list[SeparationMode] = self._derive_modes(self._models, labels)

    @classmethod
    def from_directory(cls, models_dir: Path, *, include_development: bool = False) -> Self:
        """Load the catalog from ``{models_dir}/catalog.json``.

        ``include_development`` is forwarded verbatim; see the class docstring.
        """
        return cls.from_file(models_dir / CATALOG_FILENAME, include_development=include_development)

    @classmethod
    def from_file(cls, path: Path, *, include_development: bool = False) -> Self:
        """Load and validate a catalog file.

        ``include_development`` is forwarded verbatim; see the class docstring.

        Raises:
            ModelCatalogError: If the file cannot be read, is not JSON, does not
                match the catalog shape, or is internally inconsistent. The
                message always names the file and the offending field or models.
        """
        source = str(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelCatalogError(f"{source}: model catalog could not be read ({exc}).") from exc
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelCatalogError(f"{source}: model catalog is not valid JSON ({exc}).") from exc
        try:
            catalog = _CatalogFile.model_validate(payload)
        except ValidationError as exc:
            raise ModelCatalogError(
                f"{source}: model catalog is invalid — {_describe(exc)}."
            ) from exc
        return cls(
            catalog.models,
            mode_display_names={
                mode_id: label.display_name for mode_id, label in catalog.separation_modes.items()
            },
            source=source,
            include_development=include_development,
        )

    def list_models(self) -> list[Model]:
        """Every model in the catalog, in catalog order.

        "Every" means every *catalogued* model: development fixtures are absent
        unless the catalog was built with ``include_development`` (see the class
        docstring).

        The models carry the *baseline* installation state (see the class
        docstring); the model routes serve
        :meth:`straticate.models.installer.ModelInstaller.describe` instead.
        """
        return list(self._models)

    def get_model(self, model_id: str) -> Model:
        """Return the model with ``model_id``.

        Raises:
            ApplicationError: ``model_not_found`` (404) if no such model exists.
        """
        return self.get_entry(model_id).model

    def list_entries(self) -> list[CatalogEntry]:
        """Every entry — model plus artifact — in catalog order."""
        return list(self._entries)

    def inference_parameters(self, model_id: str) -> Mapping[str, Any] | None:
        """The ``default_inference_parameters`` of ``model_id``, if it declares any.

        The lookup :func:`straticate.main.create_app` hands the separator
        registry, so a builder can reach its model's hyperparameters without
        this package's private types leaving it.

        Raises:
            ApplicationError: ``model_not_found`` (404) if no such model exists.
        """
        return self.get_entry(model_id).default_inference_parameters

    def get_entry(self, model_id: str) -> CatalogEntry:
        """Return the entry for ``model_id``, artifact included.

        An ID that could not be a model ID at all (a traversal attempt, an
        absolute path) is simply not a key here, so it exits as a clean
        ``model_not_found`` — never a 500, and never a path outside
        ``models_dir``.

        Raises:
            ApplicationError: ``model_not_found`` (404) if no such model exists.
        """
        entry = self._by_id.get(model_id)
        if entry is None:
            raise ApplicationError(
                "model_not_found",
                f"No model with ID {model_id!r}.",
                status_code=404,
            )
        return entry

    def list_separation_modes(self) -> list[SeparationMode]:
        """The separation modes derived from the catalog's model capabilities.

        Modes appear in the order their first model appears in the catalog.
        """
        return list(self._modes)

    def _derive_modes(
        self, models: Sequence[Model], labels: Mapping[str, str]
    ) -> list[SeparationMode]:
        """Group ``models`` into modes and build each mode's stems and tiers.

        A mode exists exactly when at least one of ``models`` serves it, so a
        mode all of whose models were filtered out simply does not appear —
        which is why no mode is ever served with an empty ``quality_options``
        list, and why ``standard_stems`` (backed only by a fixture until feature
        028) vanishes rather than degrading into an unusable choice.
        """
        grouped: dict[str, list[Model]] = {}
        for model in models:
            grouped.setdefault(model.separation_mode, []).append(model)
        return [
            SeparationMode(
                id=mode_id,
                display_name=labels.get(mode_id) or _humanize(mode_id),
                stems=self._mode_stems(mode_id, members),
                quality_options=self._quality_options(mode_id, members),
            )
            for mode_id, members in grouped.items()
        ]

    def _mode_stems(self, mode_id: str, members: Sequence[Model]) -> list[str]:
        """The stems of a mode; every model in it must agree on them."""
        reference = members[0]
        for model in members[1:]:
            if model.stems != reference.stems:
                raise ModelCatalogError(
                    f"{self._source}: models in separation mode {mode_id!r} disagree on stems — "
                    f"{reference.id!r} produces {reference.stems} but {model.id!r} produces "
                    f"{model.stems}."
                )
        return list(reference.stems)

    def _quality_options(self, mode_id: str, members: Sequence[Model]) -> list[QualityOption]:
        """Map a mode's models onto user-facing quality tiers, in tier order."""
        by_tier: dict[QualityTier, Model] = {}
        for model in members:
            tier = model.quality_tier or _DEFAULT_QUALITY_TIER
            claimed = by_tier.get(tier)
            if claimed is not None:
                raise ModelCatalogError(
                    f"{self._source}: models {claimed.id!r} and {model.id!r} both claim quality "
                    f"tier {tier.value!r} in separation mode {mode_id!r}; a tier must identify "
                    "exactly one model."
                )
            by_tier[tier] = model
        return [
            QualityOption(
                id=tier.value,
                display_name=_humanize(tier.value),
                model_id=model.id,
            )
            for tier in QualityTier
            if (model := by_tier.get(tier)) is not None
        ]
