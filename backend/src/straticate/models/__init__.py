"""The model catalog: logical models and the separation modes they enable.

Application code asks this package what Straticate can separate; it never
branches on model architectures (ARCHITECTURE.md §1).
"""

from straticate.models.catalog import CATALOG_FILENAME, ModelCatalog, ModelCatalogError

__all__ = ["CATALOG_FILENAME", "ModelCatalog", "ModelCatalogError"]
