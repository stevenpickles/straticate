"""The PCM ↔ tensor bridge every torch-backed separator crosses twice.

Feature 039. Decoded audio reaches a separator as
:class:`straticate.inference.pcm.PcmAudio` — planar 16-bit integers, the shape
FFmpeg produces and :func:`straticate.inference.pcm.write_wav` consumes — and a
network wants a float tensor in ``[-1, 1]``. These four functions are that
conversion, and they were byte-identical in both backends before this module
existed.

Two rules are encoded here rather than in either backend:

- **Channel folding is the bridge's business, not the application's.** A mono
  source fed to a stereo-only checkpoint is duplicated across both channels on
  the way in and folded back down on the way out, so a job returns the channel
  count it was given whatever the network happens to take.
- **Quantization happens once, at the end.** Everything a separator derives —
  a residual stem, a normalization undone — is computed in float and rounded to
  16 bits exactly once, by :func:`tensor_to_pcm`. That is what makes
  ``vocals + instrumental`` reconstruct the mixture to within one LSB instead of
  accumulating two rounding errors.
"""

from __future__ import annotations

from array import array
from typing import Final

import torch
from torch import Tensor

from straticate.inference.pcm import PcmAudio

INT16_SCALE: Final = 32767.0
"""Full-scale multiplier for the float → 16-bit PCM conversion."""


def pcm_to_tensor(source: PcmAudio, wanted_channels: int) -> Tensor:
    """Decoded 16-bit PCM → a ``(channels, samples)`` float tensor in ``[-1, 1]``.

    A mono source fed to a stereo network is duplicated across both channels
    (and folded back down afterwards by :func:`to_source_channels`), so the
    application never has to care that this particular checkpoint is stereo-only.
    """
    frames = source.frame_count
    planes = [_plane_to_tensor(plane, frames) for plane in source.channels]
    stacked = torch.stack(planes)
    if stacked.shape[0] == wanted_channels:
        return stacked
    if stacked.shape[0] == 1:
        return stacked.expand(wanted_channels, -1).contiguous()
    return stacked.mean(dim=0, keepdim=True).expand(wanted_channels, -1).contiguous()


def _plane_to_tensor(plane: array[int], frames: int) -> Tensor:
    """One ``array("h")`` channel → a float tensor scaled to ``[-1, 1]``."""
    buffer = memoryview(plane)[:frames]
    return torch.frombuffer(bytearray(buffer), dtype=torch.int16).to(torch.float32) / INT16_SCALE


def to_source_channels(plane: Tensor, channels: int) -> Tensor:
    """Return a ``(channels, samples)`` view of a model-layout stem."""
    if plane.shape[0] == channels:
        return plane
    if channels == 1:
        return plane.mean(dim=0, keepdim=True)
    return plane[:1].expand(channels, -1).contiguous()


def tensor_to_pcm(plane: Tensor, sample_rate: int) -> PcmAudio:
    """Float ``(channels, samples)`` in ``[-1, 1]`` → 16-bit planar PCM."""
    quantized = (plane.clamp(-1.0, 1.0) * INT16_SCALE).round().to(torch.int16).contiguous()
    channels = tuple(_tensor_to_plane(quantized[index]) for index in range(quantized.shape[0]))
    return PcmAudio(sample_rate=sample_rate, channels=channels)


def _tensor_to_plane(row: Tensor) -> array[int]:
    """One int16 row → the ``array("h")`` :mod:`straticate.inference.pcm` speaks."""
    plane: array[int] = array("h")
    plane.frombytes(row.contiguous().numpy().tobytes())
    return plane


__all__ = [
    "INT16_SCALE",
    "pcm_to_tensor",
    "tensor_to_pcm",
    "to_source_channels",
]
