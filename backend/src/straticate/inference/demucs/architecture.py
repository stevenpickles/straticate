"""The Hybrid Transformer Demucs architecture *name*, importable without PyTorch.

A single string in a module of its own, for the reason
:mod:`straticate.inference.roformer.architecture` states: the separator registry
keys its builder map by this name at import time, and the module that
*implements* the architecture imports torch at its own module scope — so
importing the name from
:mod:`straticate.inference.demucs.separator` would make PyTorch a hard
dependency of importing the application, which is the regression feature 034
removed (ARCHITECTURE.md §14).

**Why ``htdemucs`` and not ``demucs``.** The architecture ID is what makes
"another checkpoint of a known architecture is a pure data edit" true, so it has
to name a set of checkpoints that really do load into one class. Upstream ships
three incompatible families behind the one project name — ``htdemucs`` /
``htdemucs_ft`` / ``htdemucs_6s`` are ``HTDemucs``, ``hdemucs_mmi`` is
``HDemucs``, and the ``mdx*`` bags are v3 ``Demucs`` — and a ``.th`` package
from one will not build with another's constructor. Naming the class family is
therefore the honest granularity; ``demucs`` would be a promise this builder
cannot keep. It also leaves ``demucs`` free as the *unimplemented* architecture
the registry's tests use to prove the ``separator_unavailable`` (501) path.
"""

from typing import Final

DEMUCS_ARCHITECTURE: Final = "htdemucs"
"""``architecture`` value this backend is registered under (§9's open set).

Nothing outside :mod:`straticate.inference` compares against this string; the
registry keys its builder map by it so that adding *another* Hybrid Transformer
Demucs checkpoint to ``models/catalog.json`` is a pure data edit.
"""

__all__ = ["DEMUCS_ARCHITECTURE"]
