"""Vendored Hybrid Transformer Demucs architecture — see ``README.md``.

Only :class:`HTDemucs` is re-exported. The other modules in this directory are
here because that class imports them (``hdemucs`` for its encoder/decoder
layers, ``demucs`` for ``DConv``/``rescale_module``, ``transformer`` for the
cross-transformer, ``spec`` for the STFT pair, ``utils`` for ``center_trim``,
``states`` for the ``capture_init`` decorator), not because Straticate calls
into them.
"""

from straticate.inference.demucs.vendor.htdemucs import HTDemucs

__all__ = ["HTDemucs"]
