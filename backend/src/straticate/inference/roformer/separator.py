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

import asyncio
import atexit
import importlib
import logging
import math
import time
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, Self, cast

import torch
from torch import Tensor

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS, FFmpegTimeout
from straticate.errors import ApplicationError
from straticate.inference.base import (
    DeviceStats,
    ProcessingStats,
    ProgressCallback,
    SeparationProgress,
    SeparatorInfo,
    SeparatorRuntimeStats,
    StageCallback,
)
from straticate.inference.pcm import (
    AudioDecodeError,
    PcmAudio,
    decode_to_pcm,
    write_wav,
)
from straticate.inference.roformer.vendor import MelBandRoformer
from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)

logger = logging.getLogger(__name__)

ROFORMER_ARCHITECTURE: Final = "mel_band_roformer"
"""``architecture`` value this separator is registered under (§9's open set).

Nothing outside :mod:`straticate.inference` compares against this string; the
registry keys its builder map by it so that adding *another* Mel-Band RoFormer
checkpoint to ``models/catalog.json`` is a pure data edit.
"""

DEFAULT_CHUNK_SAMPLES: Final = 352800
"""Samples per forward pass when the catalog entry names no ``chunk_size``.

8 s at 44.1 kHz — the value the Kim Vocal 2 configuration ships with.
"""

DEFAULT_NUM_OVERLAP: Final = 2
"""Overlap factor when the catalog entry names none: windows advance by ``C/2``."""

FADE_FRACTION: Final = 10
"""The cross-fade at each window edge is ``chunk_samples // FADE_FRACTION`` long."""

INT16_SCALE: Final = 32767.0
"""Full-scale multiplier for the float → 16-bit PCM conversion."""

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
            raise _parameters_invalid(model_id, "no default_inference_parameters block")
        model_block = raw.get("model")
        if not isinstance(model_block, Mapping):
            raise _parameters_invalid(
                model_id, "default_inference_parameters.model must be an object"
            )
        parameters = cast(Mapping[str, Any], model_block)
        unknown = sorted(set(parameters) - _MODEL_PARAMETER_NAMES)
        if unknown:
            raise _parameters_invalid(
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
        chunk_samples = _positive_int(inference.get("chunk_size", DEFAULT_CHUNK_SAMPLES), model_id)
        num_overlap = _positive_int(inference.get("num_overlap", DEFAULT_NUM_OVERLAP), model_id)
        output_block = raw.get("output")
        output = cast(Mapping[str, Any], output_block if isinstance(output_block, Mapping) else {})
        residual = output.get("residual_stem")
        if residual is not None and not isinstance(residual, str):
            raise _parameters_invalid(
                model_id, f"output.residual_stem must be a stem name, got {residual!r}"
            )
        return cls(
            model=normalized,
            chunk_samples=chunk_samples,
            num_overlap=num_overlap,
            residual_stem=residual,
        )


@dataclass(slots=True)
class _RunState:
    """Mutable bookkeeping for the separation currently in flight."""

    job_id: str
    stage: JobState
    device: torch.device
    chunks_total: int
    audio_total_seconds: float
    started_monotonic: float
    chunks_completed: int = 0
    audio_processed_seconds: float = 0.0
    chunk_seconds_total: float = 0.0
    last_chunk_seconds: float | None = None
    finished_seconds: float | None = None


class RoFormerSeparator:
    """A :class:`~straticate.inference.base.Separator` running Mel-Band RoFormer.

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
        if ffmpeg_timeout_seconds <= 0:
            raise ValueError("ffmpeg_timeout_seconds must be positive")
        self._info = info
        self._parameters = parameters
        self._ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self._residual_stem = _residual_stem_index(info, parameters)
        self._model = _load_model(info, weights_file, parameters)
        self._loaded_device = torch.device("cpu")
        self._active = False
        self._run: _RunState | None = None

    @property
    def ffmpeg_timeout_seconds(self) -> float:
        """The bound this separator applies to its decode subprocesses."""
        return self._ffmpeg_timeout_seconds

    @property
    def parameters(self) -> RoFormerParameters:
        """The catalog parameters this separator was built with (for tests/telemetry)."""
        return self._parameters

    # -- Separator protocol -------------------------------------------------

    @property
    def info(self) -> SeparatorInfo:
        """The model descriptor this separator advertises."""
        return self._info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        """Snapshot of the current (or most recent) run; ``None`` before the first.

        Unlike the fake separator, every number here is measured. On CUDA the
        memory figures come from ``torch.cuda.memory_allocated`` /
        ``max_memory_allocated`` and the device's own total; ``utilization`` and
        ``temperature_celsius`` are filled in only if NVML happens to be
        importable, because ARCHITECTURE.md §12 requires that basic operation
        never depend on it. On CPU there is no device block at all — the
        contract renders that as ``gpu: null``.
        """
        run = self._run
        if run is None:
            return None
        elapsed = (
            run.finished_seconds
            if run.finished_seconds is not None
            else time.monotonic() - run.started_monotonic
        )
        elapsed = max(elapsed, 0.0)
        mean = run.chunk_seconds_total / run.chunks_completed if run.chunks_completed else None
        return SeparatorRuntimeStats(
            job_id=run.job_id,
            model=self._info,
            device=device_stats(run.device),
            processing=ProcessingStats(
                stage=run.stage,
                chunks_completed=run.chunks_completed,
                chunks_total=run.chunks_total,
                elapsed_seconds=elapsed,
                audio_processed_seconds=run.audio_processed_seconds,
                audio_total_seconds=run.audio_total_seconds,
                realtime_factor=_realtime_factor(run.audio_processed_seconds, elapsed),
                last_chunk_seconds=run.last_chunk_seconds,
                mean_chunk_seconds=mean,
            ),
        )

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> SeparationResult:
        """Separate ``input_path`` into real stems. See :class:`RoFormerSeparator`.

        Raises:
            RuntimeError: A separation is already running on this instance.
            JobCancelled: Cancellation was observed at a chunk boundary.
            ApplicationError: ``audio_decode_failed`` (422),
                ``audio_decode_timed_out`` (504),
                ``separation_mode_mismatch`` (400) or
                ``compute_device_unavailable`` (409).
        """
        if self._active:
            raise RuntimeError("RoFormerSeparator supports one separation at a time")
        self._active = True
        try:
            return await self._separate(
                input_path,
                configuration,
                progress_callback,
                cancellation_token,
                job_id=job_id,
                output_dir=output_dir,
                stage_callback=stage_callback,
            )
        finally:
            self._active = False

    # -- implementation -----------------------------------------------------

    async def _separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None,
    ) -> SeparationResult:
        self._check_mode(configuration)
        device = _resolve_torch_device(configuration.device_id)
        started = time.monotonic()
        run = _RunState(
            job_id=job_id,
            stage=JobState.DECODING,
            device=device,
            chunks_total=0,
            audio_total_seconds=0.0,
            started_monotonic=started,
        )
        self._run = run
        try:
            _announce(stage_callback, JobState.DECODING)
            source = await self._decode(input_path)
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.LOADING_MODEL)
            run.stage = JobState.LOADING_MODEL
            await asyncio.to_thread(self._place_on_device, device)
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.SEPARATING)
            run.stage = JobState.SEPARATING
            # Per **run**, not per device placement: ``max_memory_allocated`` is
            # a per-device high-water mark that nothing else resets, so without
            # this a ten-second track following a six-minute one would report
            # the six-minute track's peak as its own. It resets to the *current*
            # allocation, so the resident model still counts.
            reset_peak_memory(device)
            estimates = await asyncio.to_thread(
                self._run_chunks, source, run, progress_callback, cancellation_token, device
            )

            _announce(stage_callback, JobState.POST_PROCESSING)
            run.stage = JobState.POST_PROCESSING
            stems = await asyncio.to_thread(self._finish_stems, estimates, source)
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.ENCODING)
            run.stage = JobState.ENCODING
            written = await self._encode(stems, output_dir, cancellation_token)
        except BaseException:
            # Cancellation (or any failure) must never leave a stem behind —
            # complete or partial — that a later reader would take for output.
            _discard_outputs(output_dir, self._info.stems)
            raise

        run.finished_seconds = time.monotonic() - started
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=written,
            metrics=SeparationResultMetrics(
                processing_seconds=run.finished_seconds,
                realtime_factor=_realtime_factor(source.duration_seconds, run.finished_seconds),
            ),
        )

    def _check_mode(self, configuration: SeparationConfiguration) -> None:
        """Reject a configuration this separator does not serve (a wiring bug)."""
        if configuration.mode_id and configuration.mode_id != self._info.separation_mode:
            raise ApplicationError(
                "separation_mode_mismatch",
                (
                    f"Model {self._info.model_id!r} serves separation mode "
                    f"{self._info.separation_mode!r}, not {configuration.mode_id!r}."
                ),
                status_code=400,
                detail={
                    "requested_mode_id": configuration.mode_id,
                    "model_separation_mode": self._info.separation_mode,
                },
            )

    async def _decode(self, input_path: Path) -> PcmAudio:
        """Decode the source to the model's native sample rate."""
        try:
            return await decode_to_pcm(
                input_path,
                sample_rate=self._info.sample_rate,
                timeout_seconds=self._ffmpeg_timeout_seconds,
            )
        except AudioDecodeError as exc:
            raise ApplicationError(
                "audio_decode_failed",
                f"The input audio could not be decoded: {exc}",
                status_code=422,
            ) from exc
        except FFmpegTimeout as exc:
            raise ApplicationError(
                "audio_decode_timed_out",
                "Decoding the input audio timed out.",
                status_code=504,
                detail={"timeout_seconds": exc.timeout_seconds},
            ) from exc

    def _place_on_device(self, device: torch.device) -> None:
        """Move the network onto ``device`` (a no-op when it is already there)."""
        if self._loaded_device == device:
            return
        self._model.to(device)
        self._loaded_device = device

    def _run_chunks(
        self,
        source: PcmAudio,
        run: _RunState,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        device: torch.device,
    ) -> Tensor:
        """The chunked overlap-add loop. Runs in a worker thread.

        Returns the per-network-stem estimates as a ``(num_stems, channels,
        samples)`` float tensor on the CPU, in the model's channel layout.
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
        :meth:`_encode` then relies on when it zips the two together.
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
            tensor_to_pcm(_to_source_channels(plane, channels), source.sample_rate)
            for plane in planes
        ]

    async def _encode(
        self,
        stems: Sequence[PcmAudio],
        output_dir: Path,
        cancellation_token: CancellationToken,
    ) -> list[Stem]:
        """Write one WAV per stem and describe them for the result record."""
        written: list[Stem] = []
        for name, audio in zip(self._info.stems, stems, strict=True):
            cancellation_token.raise_if_cancelled()
            target = output_dir / f"{name}.wav"
            temporary = target.with_name(f"{target.name}.part")
            await asyncio.to_thread(write_wav, temporary, audio)
            temporary.replace(target)
            written.append(
                Stem(
                    name=name,
                    duration_seconds=audio.duration_seconds,
                    sample_rate_hz=audio.sample_rate,
                    channels=audio.channel_count,
                )
            )
        return written

    def _report(self, progress_callback: ProgressCallback, run: _RunState) -> None:
        progress_callback(
            SeparationProgress(
                chunks_completed=run.chunks_completed,
                chunks_total=run.chunks_total,
                audio_processed_seconds=run.audio_processed_seconds,
                audio_total_seconds=run.audio_total_seconds,
            )
        )


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
    if not weights_file.is_file():
        raise ApplicationError(
            "model_weights_missing",
            f"Model {info.model_id!r} is catalogued but its weights are not installed.",
            status_code=409,
            detail={"model_id": info.model_id},
        )
    try:
        model = MelBandRoformer(**parameters.model)
    except (TypeError, ValueError, AssertionError) as exc:
        raise _parameters_invalid(info.model_id, str(exc)) from exc
    try:
        state = cast(
            "dict[str, Tensor]", torch.load(weights_file, map_location="cpu", weights_only=True)
        )
        model.load_state_dict(state, strict=True)
    except ApplicationError:
        raise
    except Exception as exc:
        logger.exception("Loading weights for model %s failed", info.model_id)
        raise ApplicationError(
            "model_weights_invalid",
            (
                f"The installed weights for model {info.model_id!r} could not be loaded "
                f"into its architecture."
            ),
            status_code=500,
            detail={"model_id": info.model_id, "reason": type(exc).__name__},
        ) from exc
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
            raise _parameters_invalid(
                info.model_id,
                (
                    f"output.residual_stem is {named!r}, but the network emits all "
                    f"{produced} advertised stems, so none is a residual"
                ),
            )
        return None

    if advertised != produced + 1:
        raise _parameters_invalid(
            info.model_id,
            f"the catalog advertises {advertised} stems but the network produces {produced}",
        )

    if named is None:
        raise _parameters_invalid(
            info.model_id,
            (
                f"the network produces {produced} of the {advertised} advertised stems, "
                f"so output.residual_stem must name the one derived by subtraction "
                f"(one of {', '.join(info.stems)})"
            ),
        )
    if named not in info.stems:
        raise _parameters_invalid(
            info.model_id,
            (
                f"output.residual_stem is {named!r}, which this model does not "
                f"advertise (stems: {', '.join(info.stems)})"
            ),
        )
    return info.stems.index(named)


def _parameters_invalid(model_id: str, reason: str) -> ApplicationError:
    """The one error for "this catalog entry cannot be run as configured"."""
    return ApplicationError(
        "model_parameters_invalid",
        f"Model {model_id!r} has unusable inference parameters: {reason}.",
        status_code=500,
        detail={"model_id": model_id, "reason": reason},
    )


def _as_tuple(value: Any) -> Any:
    """JSON has arrays; the architecture's type hints demand tuples."""
    return tuple(cast("Sequence[Any]", value)) if isinstance(value, list) else value


def _positive_int(value: Any, model_id: str) -> int:
    """Coerce a catalog number to a positive int, or fail loudly."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _parameters_invalid(model_id, f"expected a positive integer, got {value!r}")
    return value


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


def _resolve_torch_device(device_id: str | None) -> torch.device:
    """Map a *logical* device ID (feature 018) onto a torch device.

    This is the only place the two vocabularies meet, and it is deliberately one
    small function: ARCHITECTURE.md §10 says raw torch device objects never leak
    through application-level APIs, so they are constructed here from the ID the
    job already resolved and recorded.

    Raises:
        ApplicationError: ``compute_device_unavailable`` (409) when the job
            names a device this process cannot use — a CUDA device on a host
            whose CUDA runtime has gone away since detection.
    """
    if device_id is None or device_id == "cpu":
        return torch.device("cpu")
    try:
        device = torch.device(device_id)
    except (RuntimeError, ValueError) as exc:
        raise _device_unavailable(device_id, "not a device this build understands") from exc
    if device.type == "cpu":
        return device
    if device.type != "cuda" or not torch.cuda.is_available():
        raise _device_unavailable(device_id, "no such compute backend is available")
    if (device.index or 0) >= torch.cuda.device_count():
        raise _device_unavailable(device_id, "no such device index")
    return device


def _device_unavailable(device_id: str, reason: str) -> ApplicationError:
    return ApplicationError(
        "compute_device_unavailable",
        f"Compute device {device_id!r} is not available: {reason}.",
        status_code=409,
        detail={"device_id": device_id, "reason": reason},
    )


class _CudaDevicePropertiesLike(Protocol):
    """The subset of ``torch.cuda.get_device_properties()`` this module reads.

    The same narrowing :mod:`straticate.system.devices` applies, and for the
    same reason: torch's own return type is untyped, and a structural protocol
    states exactly what is consumed instead of spraying ``Any`` through a strict
    type check.
    """

    name: str
    total_memory: int


def cuda_namespace() -> Any:
    """The ``torch.cuda`` namespace, as ``Any``.

    Two jobs in one small function. It is where torch's unannotated CUDA
    members are reached (strict mode reports them as partially unknown, and
    :mod:`straticate.system.devices` narrows the same way), and it is the single
    seam a test replaces to exercise the CUDA telemetry path on a host with no
    GPU — which is the only way any of this gets covered until real hardware
    runs it.
    """
    return torch.cuda


def reset_peak_memory(device: torch.device) -> None:
    """Start a fresh peak-memory measurement for ``device``; a no-op off CUDA.

    ``torch.cuda.max_memory_allocated`` is a **per-device high-water mark that
    only an explicit reset clears**, so this belongs once per separation. It
    resets the peak to the currently allocated figure rather than to zero, so
    the resident network still counts towards the run's peak.
    """
    if device.type != "cuda":
        return
    cuda_namespace().reset_peak_memory_stats(device)


def device_stats(device: torch.device) -> DeviceStats | None:
    """Real device telemetry, or ``None`` on CPU (the contract's ``gpu: null``).

    ARCHITECTURE.md §12 lists utilization and temperature as NVML-sourced and
    **optional**; they stay ``None`` unless an NVML binding happens to be
    importable, so nothing here can make basic operation depend on it.
    """
    cuda = cuda_namespace()
    if device.type != "cuda" or not cuda.is_available():
        return None
    index = device.index or 0
    properties = cast(_CudaDevicePropertiesLike, cuda.get_device_properties(index))
    utilization, temperature = _NVML.sample(index)
    return DeviceStats(
        device_id=f"cuda:{index}",
        name=str(properties.name),
        backend="cuda",
        memory_allocated_bytes=int(cuda.memory_allocated(index)),
        memory_peak_bytes=int(cuda.max_memory_allocated(index)),
        memory_total_bytes=int(properties.total_memory),
        utilization=utilization,
        temperature_celsius=temperature,
    )


class NvmlProbe:
    """Optional NVML utilization/temperature, initialised **at most once**.

    NVML is not a dependency and never becomes one (ARCHITECTURE.md §12: basic
    operation must never require it). But it is sampled from
    :meth:`RoFormerSeparator.runtime_stats`, which
    :class:`straticate.telemetry.TelemetrySampler` calls **directly on the event
    loop** — deliberately, because :mod:`straticate.inference.base` promises that
    ``runtime_stats()`` "must be a cheap, non-blocking snapshot".

    An ``nvmlInit()``/``nvmlShutdown()`` pair per sample is not that: it is tens
    of milliseconds of driver setup and teardown, once a second, for the whole
    length of a job, in front of every WebSocket frame, job event and HTTP
    request the loop owes somebody. So the binding is loaded and initialised
    lazily on the first sample, the device handles are cached, and shutdown is
    left to :mod:`atexit` — which is how a long-running NVML consumer is meant
    to behave anyway. What remains per sample is two driver queries.

    A failure at any point is absorbed: the two optional fields stay ``None``
    and every other number in the snapshot is unaffected. A failure to *load*
    is remembered, so an absent binding costs one failed import per process
    rather than one per sample.
    """

    __slots__ = ("_handles", "_module", "_unavailable")

    def __init__(self) -> None:
        self._module: Any | None = None
        self._handles: dict[int, Any] = {}
        self._unavailable = False

    def sample(self, index: int) -> tuple[float | None, float | None]:
        """Utilization (0..1) and temperature in °C, or ``(None, None)``."""
        module = self._load()
        if module is None:
            return None, None
        try:  # pragma: no cover - needs a real NVML binding and driver
            handle = self._handles.get(index)
            if handle is None:
                handle = module.nvmlDeviceGetHandleByIndex(index)
                self._handles[index] = handle
            rates = module.nvmlDeviceGetUtilizationRates(handle)
            celsius = module.nvmlDeviceGetTemperature(handle, module.NVML_TEMPERATURE_GPU)
            return round(float(rates.gpu) / 100.0, 3), float(celsius)
        except Exception:
            self._handles.pop(index, None)
            return None, None

    def _load(self) -> Any | None:
        """Import and initialise NVML once, or remember that it is unusable."""
        if self._module is not None:
            return self._module
        if self._unavailable:
            return None
        try:
            module: Any = importlib.import_module("pynvml")
            module.nvmlInit()
        except Exception:
            logger.debug("NVML is unavailable; GPU utilization and temperature stay empty.")
            self._unavailable = True
            return None
        atexit.register(self._shutdown)
        self._module = module
        return module

    def _shutdown(self) -> None:
        """Release NVML at interpreter exit. Never raises."""
        module, self._module = self._module, None
        self._handles.clear()
        if module is None:
            return
        try:  # pragma: no cover - only runs at interpreter exit on an NVML host
            module.nvmlShutdown()
        except Exception:
            logger.debug("NVML shutdown failed; ignoring at teardown.")


_NVML = NvmlProbe()
"""Process-wide NVML probe. Replaced wholesale in tests; never re-created here."""


# --------------------------------------------------------------------------
# Audio conversion
# --------------------------------------------------------------------------


def pcm_to_tensor(source: PcmAudio, wanted_channels: int) -> Tensor:
    """Decoded 16-bit PCM → a ``(channels, samples)`` float tensor in ``[-1, 1]``.

    A mono source fed to a stereo network is duplicated across both channels
    (and folded back down afterwards by :func:`_to_source_channels`), so the
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


def _to_source_channels(plane: Tensor, channels: int) -> Tensor:
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


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def _announce(stage_callback: StageCallback | None, stage: JobState) -> None:
    if stage_callback is not None:
        stage_callback(stage)


def _realtime_factor(audio_seconds: float, processing_seconds: float) -> float:
    """RTF = audio duration / processing duration (``0.0`` when not meaningful)."""
    if processing_seconds <= 0.0 or audio_seconds <= 0.0:
        return 0.0
    return audio_seconds / processing_seconds


def _discard_outputs(output_dir: Path, stems: tuple[str, ...]) -> None:
    """Remove any stem file this separator may have written under ``output_dir``."""
    for name in stems:
        for candidate in (output_dir / f"{name}.wav", output_dir / f"{name}.wav.part"):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                logger.warning("Could not remove partial output %s", candidate)


__all__ = [
    "DEFAULT_CHUNK_SAMPLES",
    "DEFAULT_NUM_OVERLAP",
    "ROFORMER_ARCHITECTURE",
    "NvmlProbe",
    "RoFormerParameters",
    "RoFormerSeparator",
]
