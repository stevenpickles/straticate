"""``TorchSeparator`` — the run skeleton every torch-backed separator shares.

Feature 039. A separation has a shape that has nothing to do with which network
runs inside it: decode, place the network on the device, loop over chunks,
assemble stems, encode, and clean up whatever a failure left behind — while
reporting real progress, checking cancellation between chunks and keeping a
cheap telemetry snapshot readable from the event loop.

That shape was written twice. ``inference/roformer/separator.py`` and
``inference/demucs/separator.py`` were **781 of 1,591 lines byte-identical**
(49%, measured with :mod:`difflib`), the largest identical block being the 154
lines from ``separate`` to ``_place_on_device``. It had already cost: the
``_place_on_device`` defect PR #45's review found — the network's location
recorded *after* a ``.to()`` that can fail part-way, wedging a cached separator
for the life of the process — existed in both files and needed two fixes and two
regression tests. This module is where that stops.

Two holes, and only two
-----------------------

A concrete backend implements exactly :meth:`TorchSeparator._run_chunks` and
:meth:`TorchSeparator._finish_stems`. Those are where RoFormer and Demucs
genuinely differ — the chunk loop (window shape, stride, padding, normalization,
autocast) and stem assembly (a residual computed by subtraction, versus the
network's own sources mapped onto advertised names). Everything else — run-state
lifecycle, stage sequencing, decode plumbing, the job's stereo-handling choice
(feature 041, in :mod:`straticate.inference.stereo`), device placement, the CUDA
peak reset, cleanup and RTF — is here, once. The PCM bridge is one module further
out, in :mod:`straticate.inference.torch_audio`, and the host-resident
overlap-add buffer feature 038 added is beside it in
:mod:`straticate.inference.torch_overlap_add`: this class never touches either,
only the two subclasses do, inside their holes.

Adding a third architecture is therefore those two methods, a parameters
dataclass and a loader. It is *not* another copy of the lifecycle.

Threading
---------

``separate`` is awaited on the job manager's event loop and a single forward
pass is seconds of compute, so the two holes are dispatched with
:func:`asyncio.to_thread` and call their progress and stage callbacks from that
worker thread. :class:`straticate.inference.executor.SeparatorJobExecutor`
marshals them back onto the loop, which is exactly why the contract puts that
adapter there.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import torch
from torch import Tensor
from torch.nn import Module

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS, FFmpegTimeout
from straticate.errors import ApplicationError
from straticate.inference.base import (
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
from straticate.inference.stereo import apply_stereo_handling_async
from straticate.inference.torch_device import device_stats, reset_peak_memory, resolve_torch_device
from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RunState:
    """Mutable bookkeeping for the separation currently in flight.

    A backend's :meth:`TorchSeparator._run_chunks` fills in
    :attr:`chunks_total` and :attr:`audio_total_seconds` once it knows them, then
    updates the per-chunk fields as it goes; everything else is the skeleton's.
    :meth:`TorchSeparator.runtime_stats` reads this — from the event loop, while
    the worker thread writes it — which is safe because every field is a single
    ``int`` or ``float`` store and a snapshot may legally be a chunk stale.
    """

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


class TorchSeparator(ABC):
    """A :class:`~straticate.inference.base.Separator` running a torch network.

    Construction is the expensive half — a subclass reads weights off disk and
    builds a network — which is why
    :meth:`straticate.inference.registry.SeparatorRegistry.aget` exists (a build
    must never happen on the event loop) and why the network is built once and
    reused for every job of that model.

    A subclass calls ``super().__init__`` **first**, so a nonsense FFmpeg timeout
    is refused before any expensive or catalog-dependent check runs, then does
    its own validation and assigns :attr:`_model`.

    Args:
        info: The model descriptor, projected from the catalog entry, so stems,
            sample rate, version and display name all come from
            ``models/catalog.json`` and never from a constant in code.
        ffmpeg_timeout_seconds: Bound for the decode subprocesses, passed down
            from ``Settings.ffmpeg_timeout_seconds`` exactly as the fake
            separator takes it.
    """

    _model: Module
    """The network. Assigned by the subclass's ``__init__`` once it has loaded."""

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        if ffmpeg_timeout_seconds <= 0:
            raise ValueError("ffmpeg_timeout_seconds must be positive")
        self._info = info
        self._ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self._loaded_device: torch.device | None = torch.device("cpu")
        self._active = False
        self._run: RunState | None = None

    @property
    def ffmpeg_timeout_seconds(self) -> float:
        """The bound this separator applies to its decode subprocesses."""
        return self._ffmpeg_timeout_seconds

    # -- Separator protocol -------------------------------------------------

    @property
    def info(self) -> SeparatorInfo:
        """The model descriptor this separator advertises."""
        return self._info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        """Snapshot of the current (or most recent) run; ``None`` before the first.

        Unlike the fake separator, every number here is measured, and reading
        them is cheap: on CUDA the memory figures are three allocator queries and
        a cached device-property lookup, and ``utilization`` /
        ``temperature_celsius`` are two NVML queries against a handle initialised
        once per process (see :class:`straticate.inference.torch_device.NvmlProbe`).
        :mod:`straticate.inference.base` requires that — the telemetry sampler
        calls this **directly on the event loop**, ~1 Hz, for the length of a
        job. On CPU there is no device block at all; the contract renders that as
        ``gpu: null``.
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
                realtime_factor=realtime_factor(run.audio_processed_seconds, elapsed),
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
        """Separate ``input_path`` into real stems. See :class:`TorchSeparator`.

        Raises:
            RuntimeError: A separation is already running on this instance.
            JobCancelled: Cancellation was observed at a chunk boundary.
            ApplicationError: ``audio_decode_failed`` (422),
                ``audio_decode_timed_out`` (504),
                ``separation_mode_mismatch`` (400) or
                ``compute_device_unavailable`` (409).
        """
        if self._active:
            raise RuntimeError(f"{type(self).__name__} supports one separation at a time")
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

    # -- the two architecture-specific holes --------------------------------

    @abstractmethod
    def _run_chunks(
        self,
        source: PcmAudio,
        run: RunState,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        device: torch.device,
    ) -> Tensor:
        """The chunked loop over the mixture. **The extension point.**

        This is one of the two methods a backend implements, and it is
        deliberately given the whole loop rather than a per-chunk callback:
        feature 038's job was to change how the accumulator is held, and that is
        a decision about the loop rather than about a chunk. It now holds the
        accumulator on the **host**, through
        :class:`~straticate.inference.torch_overlap_add.HostOverlapAdd`, which
        both backends stream into and which a third would too — the shared piece
        of 038 is that class, not another layer of loop.

        Runs in a worker thread (:meth:`_separate` dispatches it with
        :func:`asyncio.to_thread`), so it may block, and its callbacks arrive off
        the event loop.

        What the skeleton has already done when this is called: the source is
        decoded, the network is on ``device``, the ``separating`` stage is
        announced, and the CUDA peak-memory measurement has been reset for this
        run. What it does afterwards is :meth:`_finish_stems`, encoding and
        cleanup — so an implementation owes nothing but the tensor.

        The contract an implementation must keep:

        - Set ``run.chunks_total`` and ``run.audio_total_seconds`` as soon as
          they are known and report once *before* the first chunk, so progress
          announces its denominator before claiming any work.
        - Call ``cancellation_token.raise_if_cancelled()`` **between** chunks,
          not inside one: cancellation is observed at a chunk boundary
          (ARCHITECTURE.md §7).
        - After each chunk, update ``run.last_chunk_seconds``,
          ``run.chunk_seconds_total``, ``run.chunks_completed`` and
          ``run.audio_processed_seconds``, then call :meth:`_report`. Progress is
          real work: a chunk is reported once it has actually been processed,
          never on a timer (AGENTS.md principle 3).

        Args:
            source: The decoded mixture, at :attr:`SeparatorInfo.sample_rate`.
            run: The run's bookkeeping, to be updated as described above.
            progress_callback: Pass to :meth:`_report`; do not call directly.
            cancellation_token: Checked between chunks.
            device: Where the network is.

        Returns:
            The network's estimates as a float tensor **on the CPU**, in the
            network's own layout — ``(outputs, channels, samples)``.
            :meth:`_finish_stems` gives that layout meaning.
        """

    @abstractmethod
    def _finish_stems(self, estimates: Tensor, source: PcmAudio) -> list[PcmAudio]:
        """Turn the network's estimates into one :class:`PcmAudio` per advertised stem.

        The other architecture-specific hole, and the reason ``post_processing``
        is announced as a real stage: this is where a residual stem is derived by
        subtraction, or the network's own source order is reconciled with the
        advertised one — **by name, never by position**. Both backends learned
        that the expensive way; see their implementations.

        Runs in a worker thread.

        Returns:
            One entry per name in :attr:`SeparatorInfo.stems`, **in advertised
            order**, in the source's channel layout and sample rate.
            :meth:`_encode` zips the two lists together and relies on it.
        """

    # -- the shared skeleton ------------------------------------------------

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
        device = resolve_torch_device(configuration.device_id)
        started = time.monotonic()
        run = RunState(
            job_id=job_id,
            stage=JobState.DECODING,
            device=device,
            chunks_total=0,
            audio_total_seconds=0.0,
            started_monotonic=started,
        )
        self._run = run
        try:
            announce(stage_callback, JobState.DECODING)
            source = await self._decode(input_path)
            # The job's stereo-handling choice (feature 041) applies here and
            # nowhere else: from this line on, ``source`` *is* the mixture, so
            # the chunk loop, the residual arithmetic in ``_finish_stems`` and
            # the encoded stems all agree about what was separated. The default
            # is identity, so an existing job is untouched — literally the same
            # object, not an equal one, and no thread hop either. A fold that
            # *was* asked for runs in cancellable blocks, because on long
            # material it is a minute of pure-Python work.
            source = await apply_stereo_handling_async(
                source, configuration.stereo_handling, cancellation_token
            )
            cancellation_token.raise_if_cancelled()

            announce(stage_callback, JobState.LOADING_MODEL)
            run.stage = JobState.LOADING_MODEL
            await asyncio.to_thread(self._place_on_device, device)
            cancellation_token.raise_if_cancelled()

            announce(stage_callback, JobState.SEPARATING)
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

            announce(stage_callback, JobState.POST_PROCESSING)
            run.stage = JobState.POST_PROCESSING
            stems = await asyncio.to_thread(self._finish_stems, estimates, source)
            cancellation_token.raise_if_cancelled()

            announce(stage_callback, JobState.ENCODING)
            run.stage = JobState.ENCODING
            written = await self._encode(stems, output_dir, cancellation_token)
        except BaseException:
            # Cancellation (or any failure) must never leave a stem behind —
            # complete or partial — that a later reader would take for output.
            discard_outputs(output_dir, self._info.stems)
            raise

        run.finished_seconds = time.monotonic() - started
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=written,
            metrics=SeparationResultMetrics(
                processing_seconds=run.finished_seconds,
                realtime_factor=realtime_factor(source.duration_seconds, run.finished_seconds),
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
        """Move the network onto ``device`` (a no-op when it is already there).

        The network's location is forgotten *before* the move, not after it.
        ``nn.Module.to`` walks the parameters and moves them one at a time, so a
        failure part-way — a CUDA OOM is the realistic one — leaves the network
        split across two devices. A separator is cached per model for the life of
        the process, so recording the *intended* device only on success is not
        the safe order it looks like: it is the order in which the next job, on
        any device, takes the early-out above, skips the move, and dies with
        "Expected all tensors to be on the same device" forever. ``None`` matches
        no device, so the next run always re-places the network, which is the one
        thing that can put it back together.
        """
        if self._loaded_device == device:
            return
        self._loaded_device = None
        self._model.to(device)
        self._loaded_device = device

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

    def _report(self, progress_callback: ProgressCallback, run: RunState) -> None:
        """Publish the run's current counters. Called by :meth:`_run_chunks`."""
        progress_callback(
            SeparationProgress(
                chunks_completed=run.chunks_completed,
                chunks_total=run.chunks_total,
                audio_processed_seconds=run.audio_processed_seconds,
                audio_total_seconds=run.audio_total_seconds,
            )
        )


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------


def announce(stage_callback: StageCallback | None, stage: JobState) -> None:
    """Announce a processing stage, if the caller asked to hear about them."""
    if stage_callback is not None:
        stage_callback(stage)


def realtime_factor(audio_seconds: float, processing_seconds: float) -> float:
    """RTF = audio duration / processing duration (``0.0`` when not meaningful)."""
    if processing_seconds <= 0.0 or audio_seconds <= 0.0:
        return 0.0
    return audio_seconds / processing_seconds


def discard_outputs(output_dir: Path, stems: tuple[str, ...]) -> None:
    """Remove any stem file this separator may have written under ``output_dir``."""
    for name in stems:
        for candidate in (output_dir / f"{name}.wav", output_dir / f"{name}.wav.part"):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                logger.warning("Could not remove partial output %s", candidate)


__all__ = [
    "RunState",
    "TorchSeparator",
    "announce",
    "discard_outputs",
    "realtime_factor",
]
