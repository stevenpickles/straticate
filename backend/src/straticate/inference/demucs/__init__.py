"""The Hybrid Transformer Demucs inference backend (feature 028).

Three parts, deliberately separated, mirroring
:mod:`straticate.inference.roformer`:

- :mod:`~straticate.inference.demucs.architecture` — the architecture *name*,
  and nothing else. Torch-free, so the registry can key its builder map by it.
- ``vendor/`` — a pinned copy of a third-party architecture, with its licence
  and its provenance (see ``vendor/README.md``). Not maintained here.
- :mod:`~straticate.inference.demucs.separator` — Straticate's code: the
  chunked overlap-add loop, the checkpoint reader, progress, cancellation,
  telemetry, stem mapping, stem writing and error mapping, all behind the
  :class:`~straticate.inference.base.Separator` protocol.

Nothing outside this package imports torch, names a tensor, or knows what a
segment size is (ARCHITECTURE.md §1).

**Importing this package does not import PyTorch** (feature 034). Everything
except :data:`DEMUCS_ARCHITECTURE` is resolved on first attribute access
through :func:`__getattr__` (PEP 562), which imports
:mod:`~straticate.inference.demucs.separator` — and therefore torch — only then.
A build without torch raises ``ImportError`` from the attribute access, which
:func:`straticate.inference.registry.demucs_separator_builder` turns into the
``separator_unavailable`` (501) the registry has always raised for a model this
build cannot run.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from straticate.inference.demucs.architecture import DEMUCS_ARCHITECTURE

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from straticate.inference.demucs.separator import (
        DEFAULT_OVERLAP,
        DEFAULT_TRANSITION_POWER,
        DemucsParameters,
        DemucsSeparator,
        NvmlProbe,
        load_checkpoint_package,
    )

_LAZY_FROM_SEPARATOR = frozenset(
    {
        "DEFAULT_OVERLAP",
        "DEFAULT_TRANSITION_POWER",
        "DemucsParameters",
        "DemucsSeparator",
        "NvmlProbe",
        "load_checkpoint_package",
    }
)
"""Names re-exported from the torch-importing implementation module."""


# ``if not TYPE_CHECKING`` is **load-bearing, not decoration** — see the same
# guard in ``straticate/inference/roformer/__init__.py``. A module-level
# ``__getattr__`` the type checker can see makes *every* attribute of this
# package resolve to whatever it returns, so a typo or a deleted export stops
# being a type error. Pyright evaluates ``TYPE_CHECKING`` as true, so this
# branch is statically unreachable and the ``if TYPE_CHECKING`` imports above
# are the only surface it sees; at runtime the branch is the live one.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Resolve a torch-backed export on first access (PEP 562).

        The imported value is cached in this module's ``globals()``, so the
        second access is an ordinary attribute lookup and the lazy layer costs
        nothing after the first.

        Raises:
            ImportError: PyTorch is not installed. Callers that must degrade —
                the registry's Demucs builder — catch this; nothing else in the
                application touches these names.
            AttributeError: No such export.
        """
        if name in _LAZY_FROM_SEPARATOR:
            module = importlib.import_module("straticate.inference.demucs.separator")
            value = getattr(module, name)
            globals()[name] = value
            return value
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_OVERLAP",
    "DEFAULT_TRANSITION_POWER",
    "DEMUCS_ARCHITECTURE",
    "DemucsParameters",
    "DemucsSeparator",
    "NvmlProbe",
    "load_checkpoint_package",
]
