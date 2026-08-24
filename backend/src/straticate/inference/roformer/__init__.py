"""The Mel-Band RoFormer inference backend (feature 026).

Two halves, deliberately separated:

- ``vendor/`` — a pinned copy of a third-party architecture, with its licence
  and its provenance (see ``vendor/README.md``). Not maintained here.
- :mod:`~straticate.inference.roformer.separator` — Straticate's code: the
  chunked overlap-add loop, progress, cancellation, telemetry, stem writing and
  error mapping, all behind the :class:`~straticate.inference.base.Separator`
  protocol.

Nothing outside this package imports torch, names a tensor, or knows what a
segment size is (ARCHITECTURE.md §1).
"""

from straticate.inference.roformer.separator import (
    DEFAULT_CHUNK_SAMPLES,
    DEFAULT_NUM_OVERLAP,
    ROFORMER_ARCHITECTURE,
    RoFormerParameters,
    RoFormerSeparator,
)

__all__ = [
    "DEFAULT_CHUNK_SAMPLES",
    "DEFAULT_NUM_OVERLAP",
    "ROFORMER_ARCHITECTURE",
    "RoFormerParameters",
    "RoFormerSeparator",
]
