"""The Mel-Band RoFormer architecture *name*, importable without PyTorch.

A single string in a module of its own, and the reason is feature 034. The
separator registry has to key its builder map by this architecture name at
import time, but the module that *implements* the architecture imports torch at
its own module scope — so importing the name from
:mod:`straticate.inference.roformer.separator` would make PyTorch a hard
dependency of importing the application, which is exactly the regression 034
removes (ARCHITECTURE.md §14, and the "PyTorch is an optional probe, not a
dependency" section of ``docs/features/018-device-detection.md``).

The name is data — a value from the open ``architecture`` set of
ARCHITECTURE.md §9 — so it costs nothing to state where every reader can reach
it. :mod:`straticate.inference.roformer.separator` imports it from here and
re-exports it, so its own public surface is unchanged.
"""

from typing import Final

ROFORMER_ARCHITECTURE: Final = "mel_band_roformer"
"""``architecture`` value this backend is registered under (§9's open set).

Nothing outside :mod:`straticate.inference` compares against this string; the
registry keys its builder map by it so that adding *another* Mel-Band RoFormer
checkpoint to ``models/catalog.json`` is a pure data edit.
"""

__all__ = ["ROFORMER_ARCHITECTURE"]
