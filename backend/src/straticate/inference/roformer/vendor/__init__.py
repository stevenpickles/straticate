"""Pinned third-party Mel-Band RoFormer architecture — see ``README.md``.

Vendored from ``openmirlab/melband-roformer-infer`` v0.1.5 (MIT, ``LICENSE``
beside this file). Nothing outside :mod:`straticate.inference.roformer` imports
from here: the architecture, its tensors and its hyperparameters stay behind the
``Separator`` seam (ARCHITECTURE.md §1).
"""

from straticate.inference.roformer.vendor.mel_band_roformer import MelBandRoformer

__all__ = ["MelBandRoformer"]
