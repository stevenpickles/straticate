"""The Mel-Band RoFormer inference backend (feature 026).

Three parts, deliberately separated:

- :mod:`~straticate.inference.roformer.architecture` — the architecture *name*,
  and nothing else. Torch-free, so the registry can key its builder map by it.
- ``vendor/`` — a pinned copy of a third-party architecture, with its licence
  and its provenance (see ``vendor/README.md``). Not maintained here.
- :mod:`~straticate.inference.roformer.separator` — Straticate's code: the
  chunked overlap-add loop, progress, cancellation, telemetry, stem writing and
  error mapping, all behind the :class:`~straticate.inference.base.Separator`
  protocol.

Nothing outside this package imports torch, names a tensor, or knows what a
segment size is (ARCHITECTURE.md §1).

**Importing this package does not import PyTorch** (feature 034). Everything
except :data:`ROFORMER_ARCHITECTURE` is resolved on first attribute access
through :func:`__getattr__` (PEP 562), which imports
:mod:`~straticate.inference.roformer.separator` — and therefore torch — only
then. The names, the ``__all__`` and the types a caller sees are exactly what
they were; what changed is *when* the cost is paid, and whether an installation
without torch can import the application at all. A build without torch raises
``ImportError`` from the attribute access, which
:func:`straticate.inference.registry.roformer_separator_builder` turns into the
``separator_unavailable`` (501) it has always raised for a model this build
cannot run.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from straticate.inference.roformer.architecture import ROFORMER_ARCHITECTURE

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from straticate.inference.roformer.separator import (
        DEFAULT_CHUNK_SAMPLES,
        DEFAULT_NUM_OVERLAP,
        NvmlProbe,
        RoFormerParameters,
        RoFormerSeparator,
    )

_LAZY_FROM_SEPARATOR = frozenset(
    {
        "DEFAULT_CHUNK_SAMPLES",
        "DEFAULT_NUM_OVERLAP",
        "NvmlProbe",
        "RoFormerParameters",
        "RoFormerSeparator",
    }
)
"""Names re-exported from the torch-importing implementation module."""


def __getattr__(name: str) -> Any:
    """Resolve a torch-backed export on first access (PEP 562).

    The imported value is cached in this module's ``globals()``, so the second
    access is an ordinary attribute lookup and the lazy layer costs nothing
    after the first.

    Raises:
        ImportError: PyTorch (or another dependency of the vendored
            architecture) is not installed. Callers that must degrade — the
            registry's RoFormer builder — catch this; nothing else in the
            application touches these names.
        AttributeError: No such export.
    """
    if name in _LAZY_FROM_SEPARATOR:
        module = importlib.import_module("straticate.inference.roformer.separator")
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_CHUNK_SAMPLES",
    "DEFAULT_NUM_OVERLAP",
    "ROFORMER_ARCHITECTURE",
    "NvmlProbe",
    "RoFormerParameters",
    "RoFormerSeparator",
]
