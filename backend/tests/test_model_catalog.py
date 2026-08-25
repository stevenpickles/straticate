"""Tests for the model catalog: loading, validation, and mode derivation."""

import json
from pathlib import Path
from typing import Any

import pytest

from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.inference import FAKE_ARCHITECTURE
from straticate.main import create_app
from straticate.models import CATALOG_FILENAME, ModelCatalog, ModelCatalogError

ARTIFACT: dict[str, Any] = {
    "download_url": "https://weights.invalid/model.ckpt",
    "size_bytes": 1024,
    "sha256": "0" * 64,
}
"""A syntactically valid manifest ``artifact`` block (never fetched)."""


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
    """The repository's own ``models/catalog.json``, via the session's settings.

    The session enables ``include_development_models`` (see
    ``conftest.DEVELOPMENT_MODELS_ENV``), so this is the catalog as the file
    declares it — fixtures included. What a *user* gets is asserted separately,
    by the tests that build the catalog with the setting off.
    """
    settings = Settings()
    return ModelCatalog.from_directory(
        settings.models_dir, include_development=settings.include_development_models
    )


# --- Loading and validation -------------------------------------------------


def test_default_models_dir_holds_the_repository_catalog() -> None:
    catalog_path = Settings().models_dir / CATALOG_FILENAME
    assert catalog_path.is_file(), catalog_path


def test_real_catalog_loads(real_catalog: ModelCatalog) -> None:
    ids = [model.id for model in real_catalog.list_models()]
    assert ids == [
        "fake-vocals-001",
        "fake-standard-001",
        "vocals-hq-001",
        "standard-stems-001",
    ]


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


@pytest.mark.parametrize(
    "stems",
    [
        pytest.param(["Vocals", "Instrumental"], id="capitalized"),
        pytest.param(["vocals", "drums-2"], id="hyphen"),
        pytest.param(["vocals", "../escape"], id="traversal"),
        pytest.param(["1st", "instrumental"], id="leading-digit"),
    ],
)
def test_invalid_stem_name_fails_at_load_time(tmp_path: Path, stems: list[str]) -> None:
    """Not at job-create time, where it used to surface as an unhandled 500."""
    write_catalog(tmp_path, [make_model("m-001", stems=stems)])
    with pytest.raises(ModelCatalogError, match=r"models\.0\.stems") as excinfo:
        ModelCatalog.from_directory(tmp_path)
    assert CATALOG_FILENAME in str(excinfo.value)


def test_duplicate_stem_name_fails_at_load_time(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-001", stems=["vocals", "vocals"])])
    with pytest.raises(ModelCatalogError, match=r"models\.0\.stems") as excinfo:
        ModelCatalog.from_directory(tmp_path)
    message = str(excinfo.value)
    assert CATALOG_FILENAME in message
    assert "unique" in message


def test_application_refuses_to_start_on_an_invalid_stem_name(tmp_path: Path) -> None:
    """The whole point of load-time validation: startup fails, not the first job."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    write_catalog(models_dir, [make_model("m-001", stems=["Vocals", "Instrumental"])])

    with pytest.raises(ModelCatalogError):
        create_app(Settings(models_dir=models_dir, data_dir=tmp_path / "data"))


def test_inference_only_fields_are_dropped_on_load(tmp_path: Path) -> None:
    """``default_inference_parameters`` never reaches the API model."""
    write_catalog(
        tmp_path,
        [make_model("m-001", default_inference_parameters={"segment_size": 256, "overlap": 4})],
    )
    model = ModelCatalog.from_directory(tmp_path).get_model("m-001")
    assert "default_inference_parameters" not in model.model_dump()


def test_licensing_is_surfaced_on_the_model(tmp_path: Path) -> None:
    """A user can read a model's terms before installing its weights (025)."""
    write_catalog(
        tmp_path,
        [make_model("m-001", licensing={"code_license": "MIT", "commercial_use_permitted": True})],
    )
    licensing = ModelCatalog.from_directory(tmp_path).get_model("m-001").licensing
    assert licensing is not None
    assert licensing.code_license == "MIT"
    assert licensing.commercial_use_permitted is True
    assert licensing.weights_license is None


def test_the_artifact_block_never_reaches_the_public_model(tmp_path: Path) -> None:
    """``download_url``/``sha256`` are the installer's, not the API's."""
    write_catalog(tmp_path, [make_model("m-001", artifact=ARTIFACT)])
    entry = ModelCatalog.from_directory(tmp_path).get_entry("m-001")
    assert entry.artifact is not None
    assert entry.artifact.download_url == ARTIFACT["download_url"]
    assert "artifact" not in entry.model.model_dump()


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


def test_modes_derived_from_the_repository_catalog(real_catalog: ModelCatalog) -> None:
    modes = {mode.id: mode for mode in real_catalog.list_separation_modes()}
    assert set(modes) == {"vocals", "standard_stems"}

    vocals = modes["vocals"]
    assert vocals.display_name == "Vocal Isolation"
    assert vocals.stems == ["vocals", "instrumental"]
    # Two tiers since feature 026: the fake development model claims balanced
    # (explicitly, since feature 032) and the Mel-Band RoFormer backs
    # high_quality. Both advertise the same stems, which is what lets one mode
    # offer both.
    assert [(option.id, option.model_id) for option in vocals.quality_options] == [
        ("balanced", "fake-vocals-001"),
        ("high_quality", "vocals-hq-001"),
    ]

    standard = modes["standard_stems"]
    assert standard.display_name == "Standard Stems"
    assert standard.stems == ["vocals", "drums", "bass", "other"]
    # Two tiers since feature 028: the fixture claims ``fast`` and the Hybrid
    # Transformer Demucs backs ``balanced``.
    assert [(option.id, option.model_id) for option in standard.quality_options] == [
        ("fast", "fake-standard-001"),
        ("balanced", "standard-stems-001"),
    ]


def test_the_real_model_keeps_its_inference_parameters_off_the_public_model(
    real_catalog: ModelCatalog,
) -> None:
    """``default_inference_parameters`` reaches the separator, never a response."""
    entry = real_catalog.get_entry("vocals-hq-001")
    parameters = entry.default_inference_parameters
    assert parameters is not None
    assert parameters["model"]["num_bands"] == 60
    assert parameters["inference"]["chunk_size"] == 352800
    assert real_catalog.inference_parameters("vocals-hq-001") == parameters
    assert "default_inference_parameters" not in entry.model.model_dump()
    # A model that declares none says so, rather than inventing an empty block.
    assert real_catalog.inference_parameters("fake-vocals-001") is None


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


# --- Development fixtures (feature 032) -------------------------------------


def user_catalog(directory: Path) -> ModelCatalog:
    """The catalog a **user** gets: development fixtures excluded."""
    return ModelCatalog.from_directory(directory, include_development=False)


def repository_manifest() -> dict[str, dict[str, Any]]:
    """The shipped ``models/catalog.json``, as written, keyed by model ID."""
    payload: dict[str, Any] = json.loads(
        (Settings().models_dir / CATALOG_FILENAME).read_text(encoding="utf-8")
    )
    return {entry["id"]: entry for entry in payload["models"]}


def test_the_shipped_fixtures_are_marked_development_only() -> None:
    """The two fake entries must carry the marker, in the file itself.

    This is the test that makes the exclusion real. The filter reads
    ``development_only``; if a fixture were added to ``models/catalog.json``
    without it — as both of these were, before feature 032 — it would be offered
    to users as an ordinary quality tier and nothing else would notice.
    """
    entries = repository_manifest()
    assert entries["fake-vocals-001"]["development_only"] is True
    assert entries["fake-standard-001"]["development_only"] is True
    # Nothing real may be marked, or a user's catalog would silently lose it.
    assert entries["vocals-hq-001"].get("development_only", False) is False
    assert entries["standard-stems-001"].get("development_only", False) is False


def test_every_entry_the_fake_engine_serves_is_marked() -> None:
    """A future fixture cannot be added without the marking.

    The named-ID test above pins today's two entries; this one keeps working
    when a third arrives. Branching on the architecture name is fine *here* — a
    test may know what the inference package knows — and is precisely what
    application code must never do (ARCHITECTURE.md §1), which is why the marker
    exists as its own manifest field rather than being inferred.
    """
    unmarked = [
        model_id
        for model_id, entry in repository_manifest().items()
        if entry["architecture"] == FAKE_ARCHITECTURE and not entry.get("development_only", False)
    ]
    assert unmarked == []


def test_the_repository_catalog_offers_a_user_no_fixture() -> None:
    catalog = user_catalog(Settings().models_dir)
    real = ["vocals-hq-001", "standard-stems-001"]
    assert [model.id for model in catalog.list_models()] == real
    assert [entry.model.id for entry in catalog.list_entries()] == real


def test_every_mode_a_user_is_offered_is_backed_by_a_real_model() -> None:
    """Feature 032's rule, now that both modes have a real model behind them.

    Until feature 028, ``standard_stems`` was backed only by a fixture and so
    disappeared from a default server rather than being served empty. It is back,
    with one tier — the Demucs entry — and the fixture's ``fast`` tier is still
    filtered out.
    """
    modes = user_catalog(Settings().models_dir).list_separation_modes()
    assert [mode.id for mode in modes] == ["vocals", "standard_stems"]
    assert [option.model_id for option in modes[0].quality_options] == ["vocals-hq-001"]
    assert [option.model_id for option in modes[1].quality_options] == ["standard-stems-001"]
    assert modes[1].stems == ["vocals", "drums", "bass", "other"]


@pytest.mark.parametrize("include_development", [False, True])
def test_no_mode_is_ever_served_with_an_empty_option_list(include_development: bool) -> None:
    modes = ModelCatalog.from_directory(
        Settings().models_dir, include_development=include_development
    ).list_separation_modes()
    assert modes
    assert all(mode.quality_options for mode in modes)


def test_including_development_serves_the_catalog_as_written(real_catalog: ModelCatalog) -> None:
    """The opt-in restores exactly the pre-032 catalog."""
    assert [model.id for model in real_catalog.list_models()] == [
        "fake-vocals-001",
        "fake-standard-001",
        "vocals-hq-001",
        "standard-stems-001",
    ]
    modes = {mode.id: mode for mode in real_catalog.list_separation_modes()}
    assert set(modes) == {"vocals", "standard_stems"}
    assert [option.model_id for option in modes["vocals"].quality_options] == [
        "fake-vocals-001",
        "vocals-hq-001",
    ]
    assert [option.model_id for option in modes["standard_stems"].quality_options] == [
        "fake-standard-001",
        "standard-stems-001",
    ]


def test_a_hidden_model_is_not_a_catalog_key(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [make_model("m-dev", development_only=True), make_model("m-real", quality_tier="fast")],
    )
    catalog = user_catalog(tmp_path)
    with pytest.raises(ApplicationError) as excinfo:
        catalog.get_model("m-dev")
    assert excinfo.value.code == "model_not_found"
    assert excinfo.value.status_code == 404
    assert catalog.get_model("m-real").id == "m-real"


def test_hiding_a_fixture_frees_the_tier_it_occupied(tmp_path: Path) -> None:
    """The defect in miniature: an untiered fixture takes ``balanced``.

    ``balanced`` sorts before ``high_quality``, and feature 011's UI preselects
    a mode's first option — which is how "upload, press Start" produced comb
    filtering rather than separation.
    """
    write_catalog(
        tmp_path,
        [make_model("m-dev", development_only=True), make_model("m-real", quality_tier="fast")],
    )
    (mode,) = user_catalog(tmp_path).list_separation_modes()
    assert [(option.id, option.model_id) for option in mode.quality_options] == [("fast", "m-real")]


def test_a_mode_all_of_whose_models_are_hidden_is_not_derived(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model(
                "m-dev",
                separation_mode="standard_stems",
                stems=["vocals", "drums", "bass", "other"],
                development_only=True,
            ),
            make_model("m-real"),
        ],
    )
    assert [mode.id for mode in user_catalog(tmp_path).list_separation_modes()] == ["vocals"]


def test_every_model_hidden_leaves_a_catalog_with_no_modes(tmp_path: Path) -> None:
    """Degenerate but legal: nothing to offer, and nothing broken by offering it.

    A catalog of fixtures alone is what a checkout looked like before feature
    026. It loads — refusing to start would be worse — and serves an empty list
    rather than an unusable mode.
    """
    write_catalog(tmp_path, [make_model("m-dev", development_only=True)])
    catalog = user_catalog(tmp_path)
    assert catalog.list_models() == []
    assert catalog.list_separation_modes() == []


def test_a_hidden_entry_is_still_validated(tmp_path: Path) -> None:
    """Whether a fixture is *served* may not decide whether the file is valid.

    CI runs with the fixtures on and a user runs with them off; if the
    consistency checks only saw the visible entries the two would disagree about
    which catalogs load, and a fixture that broke its mode would sail through
    every user's startup and fail only in CI.
    """
    write_catalog(
        tmp_path,
        [
            make_model("m-real", quality_tier="fast"),
            make_model(
                "m-dev",
                quality_tier="high_quality",
                development_only=True,
                stems=["vocals", "drums", "other"],
            ),
        ],
    )
    with pytest.raises(ModelCatalogError, match="disagree on stems"):
        user_catalog(tmp_path)


def test_a_hidden_entry_still_collides_on_a_duplicate_tier(tmp_path: Path) -> None:
    write_catalog(
        tmp_path,
        [
            make_model("m-real", quality_tier="fast"),
            make_model("m-dev", quality_tier="fast", development_only=True),
        ],
    )
    with pytest.raises(ModelCatalogError, match="both claim quality tier 'fast'"):
        user_catalog(tmp_path)


def test_a_hidden_untiered_entry_blocks_a_visible_untiered_one(tmp_path: Path) -> None:
    """The trap feature 028 would otherwise walk into, pinned deliberately.

    ``quality_tier`` defaults to ``balanced``, so two entries that both say
    nothing collide — and since feature 032 the *blocking* one can be invisible
    in every response, which makes a startup failure with no visible cause. The
    behaviour is right (validation must see the file as written, or CI and a
    user's machine would disagree about which catalogs load), so the fix is
    discoverability: the error names both IDs, and every shipped fixture
    declares its tier explicitly (see the test below).
    """
    write_catalog(
        tmp_path,
        [make_model("m-dev", development_only=True), make_model("m-real")],
    )
    with pytest.raises(ModelCatalogError, match="both claim quality tier 'balanced'") as excinfo:
        user_catalog(tmp_path)
    message = str(excinfo.value)
    assert "m-dev" in message and "m-real" in message


def test_the_shipped_fixtures_declare_an_explicit_tier() -> None:
    """A fixture may not squat on the tier a manifest gets by saying nothing.

    ``quality_tier`` is optional and defaults to ``balanced``, so an untiered
    fixture silently claims the tier a real model gets by omission — which is
    how ``fake-standard-001`` would have blocked feature 028's four-stem model
    at startup for every user, including the ones who never see fixtures.
    Requiring the declaration does not remove the collision (nothing can, while
    validation reads the whole file — and it must), but it puts every fixture's
    claim in plain sight beside the entry a contributor is adding.
    """
    undeclared = [
        model_id
        for model_id, entry in repository_manifest().items()
        if entry.get("development_only", False) and "quality_tier" not in entry
    ]
    assert undeclared == []


def test_a_hidden_entry_still_collides_on_a_duplicate_id(tmp_path: Path) -> None:
    write_catalog(tmp_path, [make_model("m-a"), make_model("m-a", development_only=True)])
    with pytest.raises(ModelCatalogError, match="duplicate model ID"):
        user_catalog(tmp_path)


def test_an_unmarked_entry_is_visible_without_declaring_anything(tmp_path: Path) -> None:
    """A normal manifest needs no new field: the marker defaults to false."""
    write_catalog(tmp_path, [make_model("m-001")])
    (model,) = user_catalog(tmp_path).list_models()
    assert model.development_only is False
