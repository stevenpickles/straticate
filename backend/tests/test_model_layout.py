"""Tests for the on-disk layout of installed model weights.

A model ID becomes a directory name, so the only interesting question here is
the one an attacker asks: can an ID name something outside ``models_dir``? The
answer has to be no *by construction* — the ID is validated against the
manifest's own pattern rather than trusted, and rejected rather than sanitized.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from straticate.models import (
    WEIGHTS_DIRECTORY,
    model_weights_dir,
    partial_weights_path,
    remove_weights,
    validate_model_id,
    weights_installed,
    weights_path,
    weights_root,
)

VALID_IDS = ["vocals-hq-001", "a", "mel-band-roformer-kim-vocal-2", "x1", "0"]

UNUSABLE_IDS = [
    "..",
    "../evil",
    "..\\evil",
    "a/b",
    "a\\b",
    "/absolute",
    "C:/windows",
    "Vocals-HQ",
    "vocals_hq",
    "vocals..hq",
    "-leading",
    "trailing-",
    "double--dash",
    "",
    ".",
    "with space",
    "%2e%2e",
]


@pytest.mark.parametrize("model_id", VALID_IDS)
def test_valid_ids_are_accepted(model_id: str) -> None:
    assert validate_model_id(model_id) == model_id


@pytest.mark.parametrize("model_id", UNUSABLE_IDS)
def test_unusable_ids_are_rejected(model_id: str) -> None:
    with pytest.raises(ValueError, match="invalid model ID"):
        validate_model_id(model_id)


@pytest.mark.parametrize("model_id", UNUSABLE_IDS)
def test_no_path_can_be_built_from_an_unusable_id(tmp_path: Path, model_id: str) -> None:
    """Every path accessor refuses; none quietly returns an escaped path."""
    accessors: list[Callable[[Path, str], object]] = [
        model_weights_dir,
        weights_path,
        partial_weights_path,
        remove_weights,
    ]
    for accessor in accessors:
        with pytest.raises(ValueError, match="invalid model ID"):
            accessor(tmp_path, model_id)


@pytest.mark.parametrize("model_id", VALID_IDS)
def test_weights_stay_inside_the_models_directory(tmp_path: Path, model_id: str) -> None:
    path = weights_path(tmp_path, model_id)
    assert path.resolve().is_relative_to(weights_root(tmp_path).resolve())
    assert weights_root(tmp_path) == tmp_path / WEIGHTS_DIRECTORY


def test_the_partial_file_is_a_sibling_of_the_target(tmp_path: Path) -> None:
    """Same directory, so publishing is a same-filesystem ``os.replace``."""
    target = weights_path(tmp_path, "m-001")
    part = partial_weights_path(tmp_path, "m-001")
    assert part.parent == target.parent
    assert part.name == f"{target.name}.part"


def test_weights_installed_tracks_the_file(tmp_path: Path) -> None:
    assert weights_installed(tmp_path, "m-001") is False
    path = weights_path(tmp_path, "m-001")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"weights")
    assert weights_installed(tmp_path, "m-001") is True


def test_weights_installed_answers_false_for_an_unusable_id(tmp_path: Path) -> None:
    """A question about an impossible ID gets an answer, not an exception."""
    assert weights_installed(tmp_path, "../evil") is False


def test_remove_weights_reports_whether_anything_was_there(tmp_path: Path) -> None:
    assert remove_weights(tmp_path, "m-001") is False
    path = weights_path(tmp_path, "m-001")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"weights")
    assert remove_weights(tmp_path, "m-001") is True
    assert not path.exists()
    assert not path.parent.exists()


def test_remove_weights_also_clears_an_orphaned_partial(tmp_path: Path) -> None:
    """A ``.part`` left by a killed process does not survive a remove."""
    part = partial_weights_path(tmp_path, "m-001")
    part.parent.mkdir(parents=True)
    part.write_bytes(b"half")
    assert remove_weights(tmp_path, "m-001") is False
    assert not part.exists()
