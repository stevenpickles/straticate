"""A tiny synthetic Hybrid Transformer Demucs checkpoint, built at test time.

Normal CI must never download the real 80 MiB checkpoint or need a GPU
(ARCHITECTURE.md §14), but the separator's *plumbing* — chunking, progress,
cancellation, stage order, stem mapping, stem writing, error mapping — has
nothing to do with how good the model is. So the tests build a network with the
same architecture and a cut-down configuration (8 channels, depth 2, one
transformer layer, a 64-point STFT at 8 kHz; about 22 000 random parameters, a
130 KB state dict, ~10 ms per forward pass) and save it in **exactly the package
shape a real Demucs checkpoint has**, so the restricted checkpoint reader is
exercised on the same structure it will meet in production.

The audio such a network produces is meaningless. That is fine and it is the
point: what is under test is everything around the model. The one thing this
cannot check is that the *vendored architecture matches the real checkpoint* —
``test_demucs_integration.py`` loads the real weights when asked, and that is
the check that decides whether the vendoring is correct at all.
"""

import pickle
import sys
import types
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import torch

from straticate.inference.base import SeparatorInfo
from straticate.inference.demucs import DEMUCS_ARCHITECTURE, DemucsParameters
from straticate.inference.demucs.vendor import HTDemucs

TINY_SAMPLE_RATE = 8000
"""Native rate of the synthetic model — decodes stay fast and files stay small."""

TINY_CHUNK_SAMPLES = 4000
"""Half a second per forward pass: the tiny model's own training segment."""

TINY_SOURCES = ["drums", "bass", "other", "vocals"]
"""The order the *network* emits — deliberately not the advertised stem order."""

TINY_STEMS = ("vocals", "drums", "bass", "other")
"""The order the *catalog* advertises, matching the ``standard_stems`` mode."""

TINY_MODEL_PARAMETERS: dict[str, Any] = {
    "sources": TINY_SOURCES,
    "audio_channels": 2,
    "samplerate": TINY_SAMPLE_RATE,
    # ``[numerator, denominator]``, as a catalog entry states it: 1/2 s at
    # 8 kHz is 4 000 samples exactly. See ``_FRACTION_PARAMETER_NAMES``.
    "segment": [1, 2],
    "channels": 8,
    "growth": 2,
    "nfft": 64,
    "depth": 2,
    "rewrite": True,
    "multi_freqs": [],
    "multi_freqs_depth": 3,
    "freq_emb": 0.2,
    "emb_scale": 10,
    "emb_smooth": True,
    "kernel_size": 8,
    "stride": 4,
    "time_stride": 2,
    "context": 1,
    "context_enc": 0,
    "norm_starts": 4,
    "norm_groups": 4,
    "dconv_mode": 3,
    "dconv_depth": 1,
    # 1 rather than the real model's 8: the DConv bottleneck is
    # ``channels // dconv_comp``, which at 8 channels would otherwise be zero.
    "dconv_comp": 1,
    "dconv_init": 0.001,
    "bottom_channels": 0,
    "t_layers": 1,
    "t_hidden_scale": 2.0,
    "t_heads": 2,
    "t_dropout": 0.0,
    "t_layer_scale": True,
    "t_gelu": True,
    "t_emb": "sin",
    "cac": True,
    "wiener_iters": 0,
    "end_iters": 0,
}
"""The cut-down configuration, in the JSON form a catalog entry carries."""


def tiny_catalog_block(**overrides: Any) -> dict[str, Any]:
    """The synthetic model's ``default_inference_parameters`` block (JSON form)."""
    model = dict(TINY_MODEL_PARAMETERS)
    model.update(overrides.pop("model", {}))
    inference: dict[str, Any] = {}
    for key in ("chunk_size", "overlap", "transition_power"):
        if key in overrides:
            inference[key] = overrides.pop(key)
    block: dict[str, Any] = {"model": model}
    if inference:
        block["inference"] = inference
    return block


def tiny_parameters(**overrides: Any) -> DemucsParameters:
    """Catalog-shaped parameters for the synthetic model.

    Built through :meth:`DemucsParameters.from_catalog` rather than by calling
    the constructor, so a test is always looking at parameters that went through
    the same validation and normalization a real catalog entry does.
    """
    model_id = overrides.pop("model_id", "tiny-standard-001")
    return DemucsParameters.from_catalog(tiny_catalog_block(**overrides), model_id=model_id)


def tiny_info(**overrides: Any) -> SeparatorInfo:
    """A descriptor consistent with the synthetic model.

    Its ``stems`` are the ``standard_stems`` order, which is **not** the order
    the network emits — that mismatch is the point, and
    ``test_demucs_separator.py`` asserts the mapping survives it.
    """
    fields: dict[str, Any] = {
        "model_id": "tiny-standard-001",
        "display_name": "Tiny Standard Stems (synthetic)",
        "architecture": DEMUCS_ARCHITECTURE,
        "version": "test",
        "separation_mode": "standard_stems",
        "stems": TINY_STEMS,
        "sample_rate": TINY_SAMPLE_RATE,
    }
    fields.update(overrides)
    return SeparatorInfo(**fields)


def build_tiny_model(seed: int = 20260028, **overrides: Any) -> Any:
    """The synthetic network itself, seeded so two runs are byte-identical."""
    parameters = tiny_parameters(model=overrides).model
    # ``torch.manual_seed`` carries no parameter annotation, which strict mode
    # reports as a partially unknown member; reach it through ``Any``.
    untyped: Any = torch
    untyped.manual_seed(seed)
    return HTDemucs(**parameters)


def tiny_package(seed: int = 20260028, **overrides: Any) -> dict[str, Any]:
    """The checkpoint **package**, shaped exactly as upstream saves one.

    A Demucs ``.th`` file is not a bare state dict: it is
    ``{klass, args, kwargs, state, training_args}``, and the reader under test
    has to cope with all of it. ``klass`` is added by
    :func:`write_tiny_weights`, which is the only place that can produce a
    reference to a module that does not exist here.
    """
    model = build_tiny_model(seed, **overrides)
    kwargs = dict(tiny_parameters(model=overrides).model)
    return {
        "args": (),
        "kwargs": kwargs,
        # Upstream stores weights in half precision; ``load_state_dict`` casts
        # them into the network's float32 parameters. Doing the same here means
        # the tests exercise that cast rather than assuming it.
        "state": {name: tensor.half() for name, tensor in model.state_dict().items()},
        "training_args": {"epochs": 0},
    }


def write_tiny_weights(path: Path, *, seed: int = 20260028, **overrides: Any) -> Path:
    """Save a synthetic checkpoint package to ``path``, ``klass`` and all.

    The ``klass`` entry is the interesting part. A real package pickles a
    *reference to the class object* ``demucs.htdemucs.HTDemucs``, a module that
    does not exist in this application and must never be imported to read a
    checkpoint. Reproducing that faithfully needs a module of that name to exist
    while :func:`pickle.dumps` runs — pickle verifies that the name it is about
    to write really resolves to the object — so one is installed for the
    duration and removed immediately afterwards. Nothing outside this function
    ever sees it, and the file it produces is the shape the restricted reader in
    ``inference/demucs/separator.py`` has to handle.
    """
    package = tiny_package(seed, **overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _upstream_module() as reference:
        package["klass"] = reference
        # Pickle resolves a class by name and checks that the name really points
        # back at the object, so the save has to happen while the module exists.
        assert pickle.loads(pickle.dumps(reference)) is reference
        torch.save(package, path)
    return path


class _UpstreamClassReference:
    """A picklable stand-in for the class name a real checkpoint records."""


@contextmanager
def _upstream_module() -> Generator[Any]:
    """Install ``demucs.htdemucs`` for the duration, and take it away again."""
    package = types.ModuleType("demucs")
    module = types.ModuleType("demucs.htdemucs")
    reference = _UpstreamClassReference
    reference.__module__ = "demucs.htdemucs"
    reference.__qualname__ = "HTDemucs"
    module.HTDemucs = reference  # pyright: ignore[reportAttributeAccessIssue]
    package.htdemucs = module  # pyright: ignore[reportAttributeAccessIssue]
    saved = {name: sys.modules.get(name) for name in ("demucs", "demucs.htdemucs")}
    sys.modules["demucs"] = package
    sys.modules["demucs.htdemucs"] = module
    try:
        yield reference
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:  # pragma: no cover - only if something else installed one
                sys.modules[name] = previous
