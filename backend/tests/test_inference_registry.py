"""Tests for the catalog model → ``Separator`` registry.

The point of the registry is that the *catalog* decides what a separator
claims: adding a fake model to ``models/catalog.json`` must need no code
change, and no built-in descriptor constant may be consulted on the resolution
path.
"""

import pytest

from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.inference import (
    FAKE_ARCHITECTURE,
    FakeSeparator,
    Separator,
    SeparatorRegistry,
    fake_separator_builder,
    separator_info_from_model,
)
from straticate.models import ModelCatalog
from straticate.schemas import Model


def make_catalog_model(model_id: str, **overrides: object) -> Model:
    """A minimal valid :class:`Model`, with ``overrides`` applied."""
    fields: dict[str, object] = {
        "id": model_id,
        "display_name": model_id,
        "architecture": FAKE_ARCHITECTURE,
        "version": "1.0",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": 44100,
        "capabilities": {"cpu": True},
    }
    fields.update(overrides)
    return Model.model_validate(fields)


@pytest.fixture
def real_models() -> list[Model]:
    """The repository's own catalog entries."""
    return ModelCatalog.from_directory(Settings().models_dir).list_models()


def test_fake_architecture_model_yields_a_fake_separator_mirroring_the_catalog(
    real_models: list[Model],
) -> None:
    registry = SeparatorRegistry()
    for model in real_models:
        separator = registry.get(model)
        assert isinstance(separator, FakeSeparator)
        info = separator.info
        assert info.model_id == model.id
        assert info.display_name == model.display_name
        assert info.architecture == model.architecture
        assert info.version == model.version
        assert info.separation_mode == model.separation_mode
        assert list(info.stems) == model.stems
        assert info.sample_rate == model.sample_rate


def test_a_catalog_only_model_needs_no_code_change() -> None:
    """A fake model that no built-in descriptor constant knows about still resolves."""
    model = make_catalog_model(
        "fake-six-stem-999",
        display_name="Fake Six Stems",
        version="3.2",
        separation_mode="six_stems",
        stems=["vocals", "drums", "bass", "guitar", "piano", "other"],
        sample_rate=48000,
    )
    separator = SeparatorRegistry().get(model)
    assert isinstance(separator, FakeSeparator)
    assert separator.info == separator_info_from_model(model)
    assert separator.info.stem_count == 6
    assert separator.info.sample_rate == 48000


def test_get_caches_one_instance_per_model(real_models: list[Model]) -> None:
    registry = SeparatorRegistry()
    first, second = real_models[0], real_models[1]

    assert registry.get(first) is registry.get(first)
    assert registry.get(first) is not registry.get(second)


def test_an_unregistered_architecture_is_a_501(real_models: list[Model]) -> None:
    model = make_catalog_model("roformer-hq-001", architecture="mel_band_roformer")
    registry = SeparatorRegistry()

    with pytest.raises(ApplicationError) as excinfo:
        registry.get(model)

    error = excinfo.value
    assert error.code == "separator_unavailable"
    assert error.status_code == 501
    assert "roformer-hq-001" in error.message
    assert "mel_band_roformer" in error.message
    assert error.detail == {
        "model_id": "roformer-hq-001",
        "architecture": "mel_band_roformer",
    }
    # An architecture that does exist is unaffected.
    assert registry.get(real_models[0]) is not None


def test_a_custom_builder_map_is_honoured() -> None:
    built: list[Model] = []

    def build(model: Model) -> Separator:
        built.append(model)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    registry = SeparatorRegistry({"custom_net": build})
    assert registry.architectures == frozenset({"custom_net"})

    model = make_catalog_model("custom-001", architecture="custom_net")
    separator = registry.get(model)
    assert isinstance(separator, FakeSeparator)
    assert built == [model]

    with pytest.raises(ApplicationError, match="separator implementation"):
        registry.get(make_catalog_model("fake-001"))


def test_register_adds_an_architecture_after_construction() -> None:
    registry = SeparatorRegistry({})
    model = make_catalog_model("late-001", architecture="late_net")

    with pytest.raises(ApplicationError):
        registry.get(model)

    registry.register("late_net", fake_separator_builder(chunk_delay_seconds=0.0))
    assert isinstance(registry.get(model), FakeSeparator)


def test_the_default_registry_covers_exactly_the_fake_architecture() -> None:
    assert SeparatorRegistry().architectures == frozenset({FAKE_ARCHITECTURE})


def test_the_fake_builder_passes_its_tuning_through() -> None:
    """Tests inject a zero-delay builder; production keeps the visible defaults."""
    builder = fake_separator_builder(
        chunk_seconds=1.0,
        chunk_delay_seconds=0.0,
        model_load_seconds=0.0,
        device=None,
    )
    separator = builder(make_catalog_model("fake-001"))
    assert isinstance(separator, FakeSeparator)
    assert separator.runtime_stats() is None
