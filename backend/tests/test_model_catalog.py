"""Tests for the model catalog: loading, validation, and mode derivation."""

import json
from pathlib import Path
from typing import Any

import pytest

from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.models import CATALOG_FILENAME, ModelCatalog, ModelCatalogError


def make_model(model_id: str, **overrides: Any) -> dict[str, Any]:
    """A minimal valid catalog entry, with ``overrides`` applied."""
    entry: dict[str, Any] = {
        "schema_version": 1,
        "id": model_id,
        "display_name": model_id,
        "architecture": "fake",
        "version": "1.0",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": 44100,
        "capabilities": {"cpu": True},
    }
    entry.update(overrides)
    return entry


def write_catalog(directory: Path, models: list[dict[str, Any]], **extra: Any) -> Path:
    """Write a synthetic catalog into ``directory`` and return its path."""
    payload: dict[str, Any] = {"catalog_version": 1, "models": models, **extra}
    path = directory / CATALOG_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def real_catalog() -> ModelCatalog:
    """The repository's own ``models/catalog.json``, via the default settings."""
    return ModelCatalog.from_directory(Settings().models_dir)


# --- Loading and validation -------------------------------------------------


def test_default_models_dir_holds_the_repository_catalog() -> None:
    catalog_path = Settings().models_dir / CATALOG_FILENAME
    assert catalog_path.is_file(), catalog_path


def test_real_catalog_loads(real_catalog: ModelCatalog) -> None:
    ids = [model.id for model in real_catalog.list_models()]
    assert ids == ["fake-vocals-001", "fake-standard-001"]


def test_missing_catalog_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ModelCatalogError, match="could not be read"):
        ModelCatalog.from_directory(tmp_path)


def test_malformed_json_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(ModelCatalogError, match="not valid JSON"):
        ModelCatalog.from_directory(tmp_path)


def test_invalid_entry_fails_loudly_naming_the_field(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-001", stems=["vocals"])])
    with pytest.raises(ModelCatalogError, match=r"models\.0\.stems") as excinfo:
        ModelCatalog.from_directory(tmp_path)
    assert CATALOG_FILENAME in str(excinfo.value)


def test_missing_required_field_fails_loudly(tmp_path: Path) -> None:
    entry = make_model("m-001")
    del entry["sample_rate"]
    write_catalog(tmp_path, [entry])
    with pytest.raises(ModelCatalogError, match=r"models\.0\.sample_rate"):
        ModelCatalog.from_directory(tmp_path)


def test_duplicate_model_id_fails_loudly(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-001"), make_model("m-001")])
    with pytest.raises(ModelCatalogError, match="duplicate model ID 'm-001'"):
        ModelCatalog.from_directory(tmp_path)


def test_manifest_only_fields_are_dropped_on_load(tmp_path: Path) -> None:
    """``default_inference_parameters`` and friends never reach the API model."""
    write_catalog(
        tmp_path,
        [
            make_model(
                "m-001",
                default_inference_parameters={"segment_size": 256, "overlap": 4},
                licensing={"code_license": "MIT"},
            )
        ],
    )
    model = ModelCatalog.from_directory(tmp_path).get_model("m-001")
    assert "default_inference_parameters" not in model.model_dump()
    assert "licensing" not in model.model_dump()


# --- Model lookup -----------------------------------------------------------


def test_get_model_returns_the_requested_model(real_catalog: ModelCatalog) -> None:
    model = real_catalog.get_model("fake-vocals-001")
    assert model.separation_mode == "vocals"
    assert model.stems == ["vocals", "instrumental"]


def test_get_model_raises_model_not_found(real_catalog: ModelCatalog) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        real_catalog.get_model("nope-001")
    assert excinfo.value.code == "model_not_found"
    assert excinfo.value.status_code == 404


# --- Separation mode derivation ---------------------------------------------


def test_modes_derived_from_the_two_fake_models(real_catalog: ModelCatalog) -> None:
    modes = {mode.id: mode for mode in real_catalog.list_separation_modes()}
    assert set(modes) == {"vocals", "standard_stems"}

    vocals = modes["vocals"]
    assert vocals.display_name == "Vocal Isolation"
    assert vocals.stems == ["vocals", "instrumental"]
    assert [option.model_id for option in vocals.quality_options] == ["fake-vocals-001"]

    standard = modes["standard_stems"]
    assert standard.display_name == "Standard Stems"
    assert standard.stems == ["vocals", "drums", "bass", "other"]
    assert [option.model_id for option in standard.quality_options] == ["fake-standard-001"]


def test_single_untiered_model_yields_one_balanced_option(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-001")])
    (mode,) = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert [(option.id, option.display_name) for option in mode.quality_options] == [
        ("balanced", "Balanced")
    ]


def test_quality_options_are_ordered_fast_to_high_quality(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-hq", quality_tier="high_quality"),
            make_model("m-balanced"),
            make_model("m-fast", quality_tier="fast"),
        ],
    )
    (mode,) = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert [option.id for option in mode.quality_options] == ["fast", "balanced", "high_quality"]
    assert [option.model_id for option in mode.quality_options] == [
        "m-fast",
        "m-balanced",
        "m-hq",
    ]
    assert [option.display_name for option in mode.quality_options] == [
        "Fast",
        "Balanced",
        "High Quality",
    ]


def test_modes_keep_catalog_order(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-stems", separation_mode="standard_stems"),
            make_model("m-vocals"),
        ],
    )
    modes = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert [mode.id for mode in modes] == ["standard_stems", "vocals"]


def test_mode_display_name_is_humanized_without_a_catalog_label(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-001", separation_mode="four_stems")])
    (mode,) = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert mode.display_name == "Four Stems"


def test_catalog_label_overrides_the_humanized_mode_name(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [make_model("m-001")],
        separation_modes={"vocals": {"display_name": "Vocal Isolation"}},
    )
    (mode,) = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert mode.display_name == "Vocal Isolation"


def test_inconsistent_stems_within_a_mode_fail_loudly(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-fast", quality_tier="fast"),
            make_model("m-hq", quality_tier="high_quality", stems=["vocals", "drums", "other"]),
        ],
    )
    with pytest.raises(ModelCatalogError, match="disagree on stems") as excinfo:
        ModelCatalog.from_directory(tmp_path)
    message = str(excinfo.value)
    assert "'vocals'" in message and "m-hq" in message


def test_two_models_claiming_one_tier_in_a_mode_fail_loudly(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-a", quality_tier="fast"),
            make_model("m-b", quality_tier="fast"),
        ],
    )
    with pytest.raises(ModelCatalogError, match="both claim quality tier 'fast'"):
        ModelCatalog.from_directory(tmp_path)


def test_untiered_models_collide_on_the_default_tier(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-a"), make_model("m-b")])
    with pytest.raises(ModelCatalogError, match="both claim quality tier 'balanced'"):
        ModelCatalog.from_directory(tmp_path)


def test_same_tier_in_different_modes_is_fine(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-a", quality_tier="fast"),
            make_model(
                "m-b",
                quality_tier="fast",
                separation_mode="standard_stems",
                stems=["vocals", "drums", "bass", "other"],
            ),
        ],
    )
    modes = ModelCatalog.from_directory(tmp_path).list_separation_modes()
    assert [option.id for mode in modes for option in mode.quality_options] == ["fast", "fast"]
