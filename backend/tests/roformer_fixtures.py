"""A tiny synthetic Mel-Band RoFormer checkpoint, built at test time.

Normal CI must never download the real 913 MB checkpoint or need a GPU
(ARCHITECTURE.md §14), but the separator's *plumbing* — chunking, progress,
cancellation, stage order, stem writing, error mapping — has nothing to do with
how good the model is. So the tests build a network with the same architecture
and a cut-down configuration (8 mel bands, one layer, a 64-point STFT at 8 kHz;
about 20 000 random parameters, an 80 KB state dict, ~10 ms per forward pass)
and save its ``state_dict`` exactly as a real checkpoint is saved.

The audio such a network produces is meaningless. That is fine and it is the
point: what is under test is everything around the model. The one thing this
cannot check is that the *vendored architecture matches the real checkpoint* —
``test_roformer_mel_filters.py`` pins the band structure for that in normal CI,
and ``test_roformer_integration.py`` loads the real weights when asked.
"""

from pathlib import Path
from typing import Any

import torch

from straticate.inference.base import SeparatorInfo
from straticate.inference.roformer import ROFORMER_ARCHITECTURE, RoFormerParameters
from straticate.inference.roformer.vendor import MelBandRoformer

TINY_SAMPLE_RATE = 8000
"""Native rate of the synthetic model — decodes stay fast and files stay small."""

TINY_CHUNK_SAMPLES = 4096
"""Half a second per forward pass, so a two-second clip is a handful of chunks."""

TINY_MODEL_PARAMETERS: dict[str, Any] = {
    "dim": 8,
    "depth": 1,
    "stereo": True,
    "num_stems": 1,
    "time_transformer_depth": 1,
    "freq_transformer_depth": 1,
    "num_bands": 8,
    "dim_head": 4,
    "heads": 2,
    "attn_dropout": 0.0,
    "ff_dropout": 0.0,
    "flash_attn": False,
    "dim_freqs_in": 33,
    "sample_rate": TINY_SAMPLE_RATE,
    "stft_n_fft": 64,
    "stft_hop_length": 16,
    "stft_win_length": 64,
    "stft_normalized": False,
    "mask_estimator_depth": 1,
}
"""The cut-down configuration. Same *shape* of configuration a real entry has."""


def tiny_parameters(**overrides: Any) -> RoFormerParameters:
    """Catalog-shaped parameters for the synthetic model.

    ``residual_stem`` defaults to ``"instrumental"`` because the synthetic model
    emits one stem and :func:`tiny_info` advertises two — the same shape the real
    entry has. Pass ``residual_stem=None`` for a model that emits them all.
    """
    model = dict(TINY_MODEL_PARAMETERS)
    model.update(overrides.pop("model", {}))
    return RoFormerParameters(
        model=model,
        chunk_samples=overrides.pop("chunk_samples", TINY_CHUNK_SAMPLES),
        num_overlap=overrides.pop("num_overlap", 2),
        residual_stem=overrides.pop("residual_stem", "instrumental"),
    )


def tiny_catalog_block(**overrides: Any) -> dict[str, Any]:
    """The same thing in ``default_inference_parameters`` (JSON) form."""
    model = dict(TINY_MODEL_PARAMETERS)
    model.update(overrides.pop("model", {}))
    block: dict[str, Any] = {
        "model": model,
        "inference": {
            "chunk_size": overrides.pop("chunk_size", TINY_CHUNK_SAMPLES),
            "num_overlap": overrides.pop("num_overlap", 2),
        },
    }
    residual = overrides.pop("residual_stem", "instrumental")
    if residual is not None:
        block["output"] = {"residual_stem": residual}
    return block


def tiny_info(**overrides: Any) -> SeparatorInfo:
    """A descriptor consistent with the synthetic model."""
    fields: dict[str, Any] = {
        "model_id": "tiny-vocals-001",
        "display_name": "Tiny Vocals (synthetic)",
        "architecture": ROFORMER_ARCHITECTURE,
        "version": "test",
        "separation_mode": "vocals",
        "stems": ("vocals", "instrumental"),
        "sample_rate": TINY_SAMPLE_RATE,
    }
    fields.update(overrides)
    return SeparatorInfo(**fields)


def write_tiny_weights(path: Path, *, seed: int = 20260026, **overrides: Any) -> Path:
    """Build the synthetic network and save its ``state_dict`` to ``path``.

    Seeded, so two runs of the suite produce byte-identical weights and any test
    that compares outputs across runs is comparing the same network.
    """
    parameters = dict(TINY_MODEL_PARAMETERS)
    parameters.update(overrides)
    # ``torch.manual_seed`` carries no parameter annotation, which strict mode
    # reports as a partially unknown member; reach it through ``Any``, as
    # ``roformer/separator.py`` does for ``torch.cuda``.
    untyped: Any = torch
    untyped.manual_seed(seed)
    model = MelBandRoformer(**parameters)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), path)
    return path
