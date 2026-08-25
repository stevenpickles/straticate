# ==========================================================================
# VENDORED CODE -- an *excerpt* of a pinned copy of third-party source (see
# README.md, "Straticate's modifications", modification 2). Do not edit except
# as recorded there. Excluded from Ruff and Pyright.
# ==========================================================================
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""
Utilities to save and load models.

--- Straticate modification 2: this file is an excerpt ---------------------
Upstream's ``demucs/states.py`` also holds ``load_model``, ``get_state``,
``set_state``, ``serialize_model``, ``save_with_checksum``, ``swap_state`` and
the DiffQ quantizer helpers. Straticate reads a checkpoint through its own
restricted loader (``../separator.py``: ``load_checkpoint_package``), which
never executes a pickled class, so none of that is used -- and upstream's
``load_model`` is a plain ``torch.load`` over a fully trusted pickle, which is
exactly the thing this backend deliberately does not do. Leaving it in the
tree would be an unused footgun, so only ``capture_init`` is copied here,
verbatim. It is copied rather than dropped because ``HDemucs.__init__`` and
``HTDemucs.__init__`` are decorated with it.
---------------------------------------------------------------------------
"""
import functools


def capture_init(init):
    @functools.wraps(init)
    def __init__(self, *args, **kwargs):
        self._init_args_kwargs = (args, kwargs)
        init(self, *args, **kwargs)

    return __init__
