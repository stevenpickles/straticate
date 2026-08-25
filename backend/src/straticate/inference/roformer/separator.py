"""``RoFormerSeparator`` — real vocal separation behind the ``Separator`` seam.

This is the first *real* inference backend (feature 026, milestone M2). It runs
the vendored Mel-Band RoFormer architecture (``vendor/``, see its ``README.md``)
over weights that feature 025 downloaded and verified, on CUDA when the job
resolved to a CUDA device and on CPU otherwise.

Everything architecture-specific stops here. PyTorch, tensors, STFT sizes,
segment length and overlap, the mel band split, the architecture's *name* — none
of it appears in :mod:`straticate.inference.base`, in the job manager, in the
API or in the frontend (ARCHITECTURE.md §1). What crosses the seam is exactly
what :class:`~straticate.inference.base.Separator` defines: a descriptor, chunk
counts, stages, stems and a result.

What is *not* here
------------------

Since feature 039, the run lifecycle is not: stages, decode plumbing, device
placement, CUDA/NVML telemetry, the PCM bridge, encoding, cleanup and RTF live
in :mod:`straticate.inference.torch_separator`,
:mod:`straticate.inference.torch_device` and
:mod:`straticate.inference.torch_audio`, shared with every other torch backend.
This module is the Mel-Band RoFormer *difference*: the catalog parameters, the
loader, the residual stem, and the two methods
:class:`~straticate.inference.torch_separator.TorchSeparator` leaves open —
:meth:`RoFormerSeparator._run_chunks` and
:meth:`RoFormerSeparator._finish_stems`.

How a run is shaped
-------------------

Stages, all of them real work this separator actually performs:

``decoding``
    FFmpeg decodes the source to the model's native rate through
    :mod:`straticate.inference.pcm` — the same decoder the fake separator uses,
    so format support is FFmpeg's, once, for everybody.
``loading_model``
    The network moves onto the compute device. Weights were read from disk when
    the separator was *constructed* (which the caller offloads — see
    :meth:`straticate.inference.registry.SeparatorRegistry.aget`), because
    constructing is the expensive, once-per-model part and running is not.
``separating``
    The chunked overlap-add loop below, in a worker thread.
``post_processing``
    Any residual stem (``instrumental`` = mixture minus vocals), the channel layout
    the source had, and quantization back to 16-bit.
``encoding``
    One WAV per stem, written ``.part``-then-renamed.

**Chunking is the progress.** The mixture is cut into ``chunk_samples`` windows
advancing by ``chunk_samples // num_overlap``, each faded in and out and summed
into an accumulator that is finally divided by the accumulated window weight —
the standard overlap-add demix used by upstream's ``demix_track``, with the same
chunk size, overlap, fade shape and reflect-padded borders. Every window is one
forward pass through a 228-million-parameter network, so
``chunks_completed / chunks_total`` is a report of work genuinely done
(AGENTS.md principle 3) and the gap between two windows is the natural place to
observe cancellation.

**The loop runs in a worker thread.** ``separate`` is awaited on the job
manager's event loop, and a single forward pass is seconds of compute on CPU, so
the loop is dispatched with :func:`asyncio.to_thread` and calls its progress and
stage callbacks from that thread —
:class:`straticate.inference.executor.SeparatorJobExecutor` marshals them back
onto the loop, which is exactly why the contract puts that adapter there.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self, cast

import torch
from torch import Tensor

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS
from straticate.errors import ApplicationError
from straticate.inference.base import ProgressCallback, SeparatorInfo
from straticate.inference.model_errors import (
    parameters_invalid,
    positive_int,
    require_installed_weights,
    weights_not_loadable,
)
from straticate.inference.pcm import PcmAudio
from straticate.inference.roformer.architecture import ROFORMER_ARCHITECTURE
from straticate.inference.roformer.vendor import MelBandRoformer
from straticate.inference.torch_audio import pcm_to_tensor, tensor_to_pcm, to_source_channels
from straticate.inference.torch_separator import RunState, TorchSeparator
from straticate.jobs.cancellation import CancellationToken

logger = logging.getLogger(__name__)

# ``ROFORMER_ARCHITECTURE`` is imported from
# :mod:`straticate.inference.roformer.architecture` and re-exported here (see
# ``__all__``), because the registry needs the *name* at import time and must
# not pull torch in to get it (feature 034). Every existing import of it from
# this module keeps working.

DEFAULT_CHUNK_SAMPLES: Final = 352800
"""Samples per forward pass when the catalog entry names no ``chunk_size``.

8 s at 44.1 kHz — the value the Kim Vocal 2 configuration ships with.
"""

DEFAULT_NUM_OVERLAP: Final = 2
"""Overlap factor when the catalog entry names none: windows advance by ``C/2``."""

FADE_FRACTION: Final = 10
"""The cross-fade at each window edge is ``chunk_samples // FADE_FRACTION`` long."""

_MODEL_PARAMETER_NAMES: Final = frozenset(
    {
        "dim",
        "depth",
        "stereo",
        "num_stems",
        "time_transformer_depth",
        "freq_transformer_depth",
        "num_bands",
        "dim_head",
        "heads",
        "attn_dropout",
        "ff_dropout",
        "flash_attn",
        "dim_freqs_in",
        "sample_rate",
        "stft_n_fft",
        "stft_hop_length",
        "stft_win_length",
        "stft_normalized",
        "mask_estimator_depth",
        "multi_stft_resolution_loss_weight",
        "multi_stft_resolutions_window_sizes",
        "multi_stft_hop_size",
        "multi_stft_normalized",
        "match_input_audio_length",
    }
)
"""Constructor arguments a catalog entry may set on the architecture.

Deliberately a *whitelist that rejects*, not one that silently drops: upstream's
loader ignores unknown keys because it reads community training configs, but a
key in ``models/catalog.json`` is something a maintainer typed, and a typo that
is quietly ignored is a model that runs with the wrong hyperparameters.
"""

_TUPLE_PARAMETER_NAMES: Final = frozenset({"multi_stft_resolutions_window_sizes"})
"""Parameters whose JSON arrays must become tuples before construction."""


@dataclass(frozen=True, slots=True)
class RoFormerParameters:
    """Everything the catalog says about *how* to run one RoFormer model.

    This is the payload of the manifest's ``default_inference_parameters`` — the
    field ARCHITECTURE.md §9 provides precisely so that per-model tuning is
    **data**, not code. It never reaches the API (``models/catalog.py`` keeps it
    off :class:`~straticate.schemas.Model`) and no application code reads it;
    only this module knows what any of it means.

    Attributes:
        model: Constructor arguments for the architecture — the checkpoint's own
            hyperparameters. A checkpoint loads only into a network built with
            exactly these, which is why they are pinned per model rather than
            defaulted in code.
        chunk_samples: Samples per forward pass.
        num_overlap: How many windows cover each sample; windows advance by
            ``chunk_samples // num_overlap``.
        residual_stem: Name of the advertised stem that is the mixture minus
            everything the network emits, or ``None`` when the network emits
            every advertised stem itself. Named rather than positional — see
            :func:`_residual_stem_index`.
    """

    model: Mapping[str, Any]
    chunk_samples: int = DEFAULT_CHUNK_SAMPLES
    num_overlap: int = DEFAULT_NUM_OVERLAP
    residual_stem: str | None = None

    @property
    def num_stems(self) -> int:
        """Stems the network itself emits (the rest are residuals)."""
        return int(self.model.get("num_stems", 1))

    @property
    def audio_channels(self) -> int:
        """Channels the network takes: 2 for a stereo model, 1 for mono."""
        return 2 if bool(self.model.get("stereo", False)) else 1

    @classmethod
    def from_catalog(cls, raw: Mapping[str, Any] | None, *, model_id: str) -> Self:
        """Build from a manifest's ``default_inference_parameters`` block.

        Shape::

            "default_inference_parameters": {
              "model":     { …the checkpoint's hyperparameters… },
              "inference": { "chunk_size": 352800, "num_overlap": 2 },
              "output":    { "residual_stem": "instrumental" }
            }

        Raises:
            ApplicationError: ``model_parameters_invalid`` (500) — the block is
                missing, malformed, or names a constructor argument the
                architecture does not take. A catalog that cannot be run is a
                deployment error, not a client error, and it says so loudly
                rather than falling back to defaults that would load the wrong
                network.
        """
        if not raw:
            raise parameters_invalid(model_id, "no default_inference_parameters block")
        model_block = raw.get("model")
        if not isinstance(model_block, Mapping):
            raise parameters_invalid(
                model_id, "default_inference_parameters.model must be an object"
            )
        parameters = cast(Mapping[str, Any], model_block)
        unknown = sorted(set(parameters) - _MODEL_PARAMETER_NAMES)
        if unknown:
            raise parameters_invalid(
                model_id, f"unknown architecture parameters: {', '.join(unknown)}"
            )
        normalized: dict[str, Any] = {
            key: _as_tuple(value) if key in _TUPLE_PARAMETER_NAMES else value
            for key, value in parameters.items()
        }
        inference_block = raw.get("inference")
        inference = cast(
            Mapping[str, Any], inference_block if isinstance(inference_block, Mapping) else {}
        )
        chunk_samples = positive_int(inference.get("chunk_size", DEFAULT_CHUNK_SAMPLES), model_id)
        num_overlap = positive_int(inference.get("num_overlap", DEFAULT_NUM_OVERLAP), model_id)
        output_block = raw.get("output")
        output = cast(Mapping[str, Any], output_block if isinstance(output_block, Mapping) else {})
        residual = output.get("residual_stem")
        if residual is not None and not isinstance(residual, str):
            raise parameters_invalid(
                model_id, f"output.residual_stem must be a stem name, got {residual!r}"
            )
        return cls(
            model=normalized,
            chunk_samples=chunk_samples,
            num_overlap=num_overlap,
            residual_stem=residual,
        )


class RoFormerSeparator(TorchSeparator):
    """A :class:`~straticate.inference.base.Separator` running Mel-Band RoFormer.

    The run lifecycle is
    :class:`~straticate.inference.torch_separator.TorchSeparator`'s; what this
    class adds is the two architecture-specific holes — the overlap-add chunk
    loop and the residual stem — plus the catalog parameters and the loader.

    Construction is the expensive half: it reads a few hundred megabytes of
    weights off disk and builds a 228-million-parameter network. That is why
    :meth:`straticate.inference.registry.SeparatorRegistry.aget` exists — a
    build must never happen on the event loop — and why the network is built
    once and reused for every job of that model.

    Args:
        info: The model descriptor, projected from the catalog entry, so stems,
            sample rate, version and display name all come from
            ``models/catalog.json`` and never from a constant in this file.
        weights_file: Where feature 025 published the verified checkpoint —
            ``weights_path(settings.models_dir, model.id)``.
        parameters: The catalog's ``default_inference_parameters``.
        ffmpeg_timeout_seconds: Bound for the decode subprocesses, passed down
            from ``Settings.ffmpeg_timeout_seconds`` exactly as the fake
            separator takes it.

    Raises:
        ApplicationError: ``model_weights_missing`` (409) when the checkpoint is
            not installed, ``model_weights_invalid`` (500) when it is installed
            but does not load into this architecture, or
            ``model_parameters_invalid`` (500) when the catalog entry's
            parameters are unusable.
    """

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        weights_file: Path,
        parameters: RoFormerParameters,
        ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(info, ffmpeg_timeout_seconds=ffmpeg_timeout_seconds)
        self._parameters = parameters
        self._residual_stem = _residual_stem_index(info, parameters)
        self._model = _load_model(info, weights_file, parameters)

    @property
    def parameters(self) -> RoFormerParameters:
        """The catalog parameters this separator was built with (for tests/telemetry)."""
        return self._parameters

    # -- the architecture-specific halves -----------------------------------

    def _run_chunks(
        self,
        source: PcmAudio,
        run: RunState,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        device: torch.device,
    ) -> Tensor:
        """The chunked overlap-add loop. Runs in a worker thread.

        Returns the per-network-stem estimates as a ``(num_stems, channels,
        samples)`` float tensor on the CPU, in the model's channel layout.

        See
        :meth:`straticate.inference.torch_separator.TorchSeparator._run_chunks`
        for the contract this keeps — and note that it is the method feature 038
        will work inside.
        """
        parameters = self._parameters
        chunk = parameters.chunk_samples
        step = max(chunk // parameters.num_overlap, 1)
        fade = max(chunk // FADE_FRACTION, 1)
        border = chunk - step

        mixture = pcm_to_tensor(source, parameters.audio_channels).to(device)
        frames = mixture.shape[-1]
        padded = mixture
        if border > 0 and frames > 2 * border:
            padded = torch.nn.functional.pad(mixture.unsqueeze(0), (border, border), mode="reflect")
            padded = padded.squeeze(0)
        else:
            border = 0
        total = padded.shape[-1]

        chunks_total = max(1, math.ceil(total / step))
        run.chunks_total = chunks_total
        run.audio_total_seconds = source.duration_seconds
        self._report(progress_callback, run)

        window = _fade_window(chunk, fade, device)
        stem_count = parameters.num_stems
        shape = (stem_count, padded.shape[0], total)
        accumulator = torch.zeros(shape, dtype=torch.float32, device=device)
        weights = torch.zeros(shape, dtype=torch.float32, device=device)

        offset = 0
        index = 0
        while offset < total:
            cancellation_token.raise_if_cancelled()
            chunk_started = time.monotonic()
            part = padded[:, offset : offset + chunk]
            length = int(part.shape[-1])
            if length < chunk:
                part = _pad_tail(part, chunk, length)

            estimate = self._forward(part, device)

            chunk_window = window.clone()
            if offset == 0:
                chunk_window[:fade] = 1.0
            if offset + chunk >= total:
                chunk_window[-fade:] = 1.0
            scaled = chunk_window[:length]
            accumulator[..., offset : offset + length] += estimate[..., :length] * scaled
            weights[..., offset : offset + length] += scaled

            offset += step
            index += 1
            run.last_chunk_seconds = time.monotonic() - chunk_started
            run.chunk_seconds_total += run.last_chunk_seconds
            run.chunks_completed = index
            # Source coordinate of the last sample this window covered: the
            # window ran from ``offset - step`` to ``offset - step + chunk`` in
            # padded coordinates, and the reflect border shifts that back.
            covered = offset + chunk - step - border
            run.audio_processed_seconds = min(max(covered, 0), frames) / source.sample_rate
            self._report(progress_callback, run)

        estimates = torch.nan_to_num(accumulator / weights.clamp(min=1e-8), nan=0.0)
        if border > 0:
            estimates = estimates[..., border : border + frames]
        return estimates.detach().to("cpu", dtype=torch.float32)

    def _forward(self, part: Tensor, device: torch.device) -> Tensor:
        """One forward pass, returned as ``(num_stems, channels, samples)``."""
        with torch.inference_mode():
            if device.type == "cuda":
                with torch.autocast(device_type="cuda"):
                    output = self._model(part.unsqueeze(0))
            else:
                output = self._model(part.unsqueeze(0))
        estimate = cast(Tensor, output)[0].to(torch.float32)
        return estimate.unsqueeze(0) if estimate.ndim == 2 else estimate

    def _finish_stems(self, estimates: Tensor, source: PcmAudio) -> list[PcmAudio]:
        """Derive any residual stem and return the source's channel layout back.

        Real work, and the reason ``post_processing`` is announced: a two-stem
        vocals model emits **one** stem, and the instrumental is the mixture
        minus it. Doing that subtraction in the float domain — before
        quantization — is what makes ``vocals + instrumental`` reconstruct the
        mixture instead of accumulating two rounding errors.

        The residual is inserted at the position the catalog gave it (see
        :func:`_residual_stem_index`), so the list returned here lines up with
        :attr:`SeparatorInfo.stems` index for index — which is what
        :meth:`~straticate.inference.torch_separator.TorchSeparator._encode` then
        relies on when it zips the two together.
        """
        parameters = self._parameters
        mixture = pcm_to_tensor(source, parameters.audio_channels)
        planes = [estimates[index] for index in range(estimates.shape[0])]
        if self._residual_stem is not None:
            residual = mixture[..., : planes[0].shape[-1]]
            for plane in planes:
                residual = residual - plane
            planes.insert(self._residual_stem, residual)
        channels = source.channel_count
        return [
            tensor_to_pcm(to_source_channels(plane, channels), source.sample_rate)
            for plane in planes
        ]


# --------------------------------------------------------------------------
# Construction helpers
# --------------------------------------------------------------------------


def _load_model(
    info: SeparatorInfo, weights_file: Path, parameters: RoFormerParameters
) -> MelBandRoformer:
    """Build the architecture and load the installed checkpoint into it.

    ``strict=True`` is the whole point: a checkpoint that does not match the
    vendored architecture exactly must fail here, loudly, rather than load
    partially and produce plausible-sounding nonsense.
    """
    require_installed_weights(info, weights_file)
    try:
        model = MelBandRoformer(**parameters.model)
    except (TypeError, ValueError, AssertionError) as exc:
        raise parameters_invalid(info.model_id, str(exc)) from exc
    try:
        state = cast(
            "dict[str, Tensor]", torch.load(weights_file, map_location="cpu", weights_only=True)
        )
        model.load_state_dict(state, strict=True)
    except ApplicationError:
        raise
    except Exception as exc:
        logger.exception("Loading weights for model %s failed", info.model_id)
        raise weights_not_loadable(info.model_id, type(exc).__name__) from exc
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def _residual_stem_index(info: SeparatorInfo, parameters: RoFormerParameters) -> int | None:
    """Position of the advertised stem that is the mixture minus the network's output.

    A vocals model emits one stem and the catalog advertises two: the other is
    ``mixture - vocals``. *Which* other one must be **named**, in the catalog's
    ``default_inference_parameters.output.residual_stem``, and not inferred from
    position.

    The tempting inference — "the residual is the last advertised stem" — is
    unsound, and silently so. The manifest schema imposes no order on ``stems``,
    and this architecture's whole promise is that another checkpoint is a pure
    data edit; an entry written as ``"stems": ["instrumental", "vocals"]`` would
    then have had the network's vocals written to ``instrumental.wav`` and the
    residual to ``vocals.wav``, with nothing anywhere reporting a problem.
    Silently wrong audio is the worst thing this module could produce, so the
    fact is declared and every other shape is refused.

    The remaining stems map to the network's outputs in advertised order. For a
    one-output model that is fully determined; for a multi-output one it is the
    same ordering contract the ``advertised == produced`` case already has, and
    the catalog entry is where the author states it.

    Raises:
        ApplicationError: ``model_parameters_invalid`` (500) when the stem list
            and the network's output count cannot be reconciled, when a residual
            is implied but not named, when a residual is named that the model
            does not advertise, or when one is named that is not needed.
    """
    advertised = len(info.stems)
    produced = parameters.num_stems
    named = parameters.residual_stem

    if advertised == produced:
        if named is not None:
            raise parameters_invalid(
                info.model_id,
                (
                    f"output.residual_stem is {named!r}, but the network emits all "
                    f"{produced} advertised stems, so none is a residual"
                ),
            )
        return None

    if advertised != produced + 1:
        raise parameters_invalid(
            info.model_id,
            f"the catalog advertises {advertised} stems but the network produces {produced}",
        )

    if named is None:
        raise parameters_invalid(
            info.model_id,
            (
                f"the network produces {produced} of the {advertised} advertised stems, "
                f"so output.residual_stem must name the one derived by subtraction "
                f"(one of {', '.join(info.stems)})"
            ),
        )
    if named not in info.stems:
        raise parameters_invalid(
            info.model_id,
            (
                f"output.residual_stem is {named!r}, which this model does not "
                f"advertise (stems: {', '.join(info.stems)})"
            ),
        )
    return info.stems.index(named)


def _as_tuple(value: Any) -> Any:
    """JSON has arrays; the architecture's type hints demand tuples."""
    return tuple(cast("Sequence[Any]", value)) if isinstance(value, list) else value


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------


def _fade_window(chunk: int, fade: int, device: torch.device) -> Tensor:
    """The per-window envelope: linear fade in, flat, linear fade out."""
    window = torch.ones(chunk, dtype=torch.float32, device=device)
    window[:fade] *= torch.linspace(0.0, 1.0, fade, dtype=torch.float32, device=device)
    window[-fade:] *= torch.linspace(1.0, 0.0, fade, dtype=torch.float32, device=device)
    return window


def _pad_tail(part: Tensor, chunk: int, length: int) -> Tensor:
    """Pad a short final window up to ``chunk`` samples.

    Reflect-padding keeps the spectrum of the tail plausible, but it needs more
    signal than it produces; a very short remainder is zero-padded instead. Both
    branches are upstream's, and the padding is discarded by the ``[:length]``
    slice either way — it exists only so every forward pass sees the fixed
    window size the network was trained on.
    """
    missing = chunk - length
    if length > chunk // 2 + 1:
        return torch.nn.functional.pad(part, (0, missing), mode="reflect")
    return torch.nn.functional.pad(part, (0, missing), mode="constant", value=0.0)


__all__ = [
    "DEFAULT_CHUNK_SAMPLES",
    "DEFAULT_NUM_OVERLAP",
    "ROFORMER_ARCHITECTURE",
    "RoFormerParameters",
    "RoFormerSeparator",
]
