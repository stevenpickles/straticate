"""Straticate backend package.

The version is **not** written here. It is resolved at import time from the
installed distribution's metadata, which is generated from the single
authoritative declaration in ``backend/pyproject.toml``. A hardcoded copy here
would be a second source of truth that nothing forces to agree with the first:
``GET /api/v1/version``, the OpenAPI document's ``info.version`` and the wheel
metadata would silently drift apart. ``tests/test_version.py`` asserts the two
still match, so editing one without the other fails the suite.

The package is always installed (editable in development, ``uv sync``), so the
lookup succeeds; the fallback exists only for the pathological case of the
source tree being imported with no distribution installed at all, where a
recognisably unreal version is better than an import error.
"""

from importlib.metadata import PackageNotFoundError, version

DISTRIBUTION_NAME = "straticate"
"""Name of the distribution whose metadata carries the version."""

UNKNOWN_VERSION = "0.0.0+unknown"
"""Reported when the distribution is not installed (never in a real run)."""

try:
    __version__ = version(DISTRIBUTION_NAME)
except PackageNotFoundError:  # pragma: no cover - the package is always installed
    __version__ = UNKNOWN_VERSION

__all__ = ["DISTRIBUTION_NAME", "UNKNOWN_VERSION", "__version__"]
