"""The model catalog: logical models, the modes they enable, and their weights.

Application code asks this package what Straticate can separate; it never
branches on model architectures (ARCHITECTURE.md §1).

Three concerns, three modules:

- :mod:`~straticate.models.catalog` — what models exist (``catalog.json``).
- :mod:`~straticate.models.layout` — where a model's weights live on disk.
- :mod:`~straticate.models.installer` — how they get there: download, SHA-256
  verification, atomic rename.
"""

from straticate.models.catalog import (
    CATALOG_FILENAME,
    CatalogEntry,
    ModelArtifact,
    ModelCatalog,
    ModelCatalogError,
)
from straticate.models.installer import ModelInstaller, ModelInstallError
from straticate.models.layout import (
    MODEL_ID_PATTERN,
    WEIGHTS_DIRECTORY,
    WEIGHTS_FILENAME,
    model_weights_dir,
    partial_weights_path,
    remove_weights,
    validate_model_id,
    weights_installed,
    weights_path,
    weights_root,
)

__all__ = [
    "CATALOG_FILENAME",
    "MODEL_ID_PATTERN",
    "WEIGHTS_DIRECTORY",
    "WEIGHTS_FILENAME",
    "CatalogEntry",
    "ModelArtifact",
    "ModelCatalog",
    "ModelCatalogError",
    "ModelInstallError",
    "ModelInstaller",
    "model_weights_dir",
    "partial_weights_path",
    "remove_weights",
    "validate_model_id",
    "weights_installed",
    "weights_path",
    "weights_root",
]
