"""On-disk layout of installed model weights.

One place defines where a model's weights live, so the installer that writes
them, the API that reports whether they are there, and the separator that will
load them (feature 026) cannot drift apart::

    {models_dir}/weights/{model_id}/weights.bin
    {models_dir}/weights/{model_id}/weights.bin.part   (in flight only)

This mirrors :mod:`straticate.inference.layout`, which owns the same question
for a job's stems. Two deliberate choices:

- **A directory per model, not a flat file.** The ``.part`` file then lives on
  the same filesystem as its target, which is what makes :func:`os.replace`
  atomic rather than a cross-device copy, and removing a model's weights is one
  directory to delete rather than a glob.
- **An architecture-neutral file name.** A RoFormer checkpoint is a ``.ckpt``
  and an ONNX export is a ``.onnx``, but application code must never branch on
  an architecture (ARCHITECTURE.md §1), and nothing that loads weights cares
  about the suffix. ``weights.bin`` says what the file is without naming a
  framework.

``models_dir`` is :attr:`straticate.config.Settings.models_dir` — the directory
that already holds ``catalog.json``. **Weights are never committed to the
repository** (ARCHITECTURE.md §9): ``models/weights/`` is gitignored.

Model IDs are validated here rather than trusted. The manifest schema constrains
``id`` to :data:`MODEL_ID_PATTERN`, but a request path is not a manifest, and an
ID reaches this module as a path segment — so it is checked before it is joined
to anything.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

MODEL_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""Model IDs we are willing to turn into a directory name.

The same pattern the model manifest schema declares for ``id``
(``models/schemas/model-manifest.schema.json``). It admits no ``.``, no ``/``,
no ``\\`` and no drive letter, so a validated ID cannot name anything outside
:func:`weights_root`.
"""

WEIGHTS_DIRECTORY = "weights"
"""Name of the installed-weights root under ``models_dir``."""

WEIGHTS_FILENAME = "weights.bin"
"""Name of a model's weights file inside its own directory."""

PARTIAL_SUFFIX = ".part"
"""Suffix of the in-flight download, renamed away only after verification."""


def validate_model_id(model_id: str) -> str:
    """Return ``model_id`` if it can safely become a directory name.

    Raises:
        ValueError: The ID does not match :data:`MODEL_ID_PATTERN`. It is
            rejected rather than sanitized: a sanitized ID would silently name a
            *different* model's weights.
    """
    if not MODEL_ID_PATTERN.fullmatch(model_id):
        raise ValueError(f"invalid model ID {model_id!r}")
    return model_id


def weights_root(models_dir: Path) -> Path:
    """Return ``{models_dir}/weights`` — every installed model's weights."""
    return models_dir / WEIGHTS_DIRECTORY


def model_weights_dir(models_dir: Path, model_id: str) -> Path:
    """Return the directory holding one model's weights (and its ``.part``).

    Raises:
        ValueError: ``model_id`` is not a valid model ID.
    """
    return weights_root(models_dir) / validate_model_id(model_id)


def weights_path(models_dir: Path, model_id: str) -> Path:
    """Return the installed weights file for ``model_id``.

    The path is computed, never created: the file exists only once an install
    has downloaded, verified and renamed it into place. Feature 026's separator
    calls this to find what to load.

    Raises:
        ValueError: ``model_id`` is not a valid model ID.
    """
    return model_weights_dir(models_dir, model_id) / WEIGHTS_FILENAME


def partial_weights_path(models_dir: Path, model_id: str) -> Path:
    """Return the in-flight download's ``.part`` file for ``model_id``.

    A sibling of :func:`weights_path`, so publishing the verified artifact is a
    same-filesystem :func:`os.replace`.

    Raises:
        ValueError: ``model_id`` is not a valid model ID.
    """
    return weights_path(models_dir, model_id).with_name(WEIGHTS_FILENAME + PARTIAL_SUFFIX)


def weights_installed(models_dir: Path, model_id: str) -> bool:
    """Whether ``model_id``'s weights are present on disk.

    ``False`` for an ID that is not a valid model ID: an unusable ID cannot name
    installed weights, and a caller asking about one deserves an answer rather
    than an exception.
    """
    try:
        return weights_path(models_dir, model_id).is_file()
    except ValueError:
        return False


def remove_weights(models_dir: Path, model_id: str) -> bool:
    """Delete ``model_id``'s weights directory; return whether weights existed.

    Removes the ``.part`` file along with the weights, so a directory left
    behind by an interrupted process does not survive a remove.

    Raises:
        ValueError: ``model_id`` is not a valid model ID.
    """
    directory = model_weights_dir(models_dir, model_id)
    existed = (directory / WEIGHTS_FILENAME).is_file()
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    return existed


__all__ = [
    "MODEL_ID_PATTERN",
    "PARTIAL_SUFFIX",
    "WEIGHTS_DIRECTORY",
    "WEIGHTS_FILENAME",
    "model_weights_dir",
    "partial_weights_path",
    "remove_weights",
    "validate_model_id",
    "weights_installed",
    "weights_path",
    "weights_root",
]
