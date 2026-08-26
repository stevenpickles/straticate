"""The fake separator — the architectural milestone of ARCHITECTURE.md §8.

:class:`FakeSeparator` implements the full :class:`~straticate.inference.base.Separator`
contract without CUDA, model downloads or any ML infrastructure, and behaves
like a real chunk-based model in every way the rest of the application can
observe:

- **Chunk-based processing.** The decoded audio is split into fixed-length
  chunks (default ~5 s) and processed one chunk at a time, with a small,
  configurable per-chunk delay. Progress is therefore *real work*
  (``completed_chunks / total_chunks``), never a timer over wall-clock.
- **Deterministic.** Same input plus same settings produce the same chunk
  count and byte-identical stems. The per-stem filter carries its state across
  chunk boundaries, so even changing ``chunk_seconds`` does not change the
  audio — only how the progress is granulated.
- **Cooperatively cancellable.** The cancellation token is checked before every
  chunk and before every written stem; cancellation removes any partial output
  so a half-written stem is never presented as complete.
- **Real, playable placeholder stems.** The source is decoded with FFmpeg and
  each stem is a cheap deterministic transform of it (see
  :ref:`the transform <fake-transform>` below), written as an ordinary 16-bit
  WAV. This is emphatically **not** separation: it exists so results, the stem
  player, and export can be built and verified before any model exists.
- **Fake runtime statistics.** :meth:`FakeSeparator.runtime_stats` reports
  plausible model/GPU-style numbers (pretend VRAM allocated/peak, utilization,
  temperature, chunk timings, RTF) for feature 019 to publish. This module
  publishes no events itself.

.. _fake-transform:

The placeholder transform
-------------------------

Each stem is the source run through a **feed-forward comb filter** with a
per-stem delay, polarity and gain::

    y_i[n] = g_i * (0.6 * x[n] + 0.4 * s_i * x[n - D_i])

    g_i = 0.9 * 0.85**i            gain, so stems are also level-distinct
    s_i = +1 if i is even else -1  polarity of the reflection
    D_i = round(sample_rate / (110 Hz * 2**i))

Properties that make it useful as a test fixture:

- **Audibly distinct.** Each stem gets a different comb colouration (notches
  an octave apart) and a different level, so a human can tell them apart in
  the stem player and a test can tell them apart by hash.
- **Never silent.** The two coefficients sum to 1 and differ, so for any
  sinusoid the output amplitude stays within ``[0.2, 1.0]`` of the input's —
  a stem derived from non-silent audio is always non-silent.
- **Never clipping.** ``|y| <= g_i * |x| <= 0.9 * full scale``.
- **Cheap.** One multiply-add per sample per stem, expressed as a list
  comprehension over ``array`` slices; no third-party numerics needed.

Cost note: the fake holds the decoded source plus every stem in memory
(roughly ``(1 + stem_count) x`` the decoded size) and does its arithmetic in
pure Python. That is fine for a local development tool on song-length input;
a real separator (feature 026) will stream and use the GPU.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from array import array
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

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
    INT16_MAX,
    INT16_MIN,
    AudioDecodeError,
    PcmAudio,
    decode_to_pcm,
    write_wav,
)
from straticate.inference.stereo import apply_stereo_handling
from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)

logger = logging.getLogger(__name__)

FAKE_ARCHITECTURE = "fake"
"""``architecture`` value of every fake catalog entry (open set, §9)."""

FAKE_VOCALS_INFO = SeparatorInfo(
    model_id="fake-vocals-001",
    display_name="Fake Vocals (development)",
    architecture=FAKE_ARCHITECTURE,
    version="1.0",
    separation_mode="vocals",
    stems=("vocals", "instrumental"),
    sample_rate=44100,
)
"""Descriptor of the ``fake-vocals-001`` entry in ``models/catalog.json``."""

FAKE_STANDARD_INFO = SeparatorInfo(
    model_id="fake-standard-001",
    display_name="Fake Standard Stems (development)",
    architecture=FAKE_ARCHITECTURE,
    version="1.0",
    separation_mode="standard_stems",
    stems=("vocals", "drums", "bass", "other"),
    sample_rate=44100,
)
"""Descriptor of the ``fake-standard-001`` entry in ``models/catalog.json``."""

FAKE_SEPARATOR_INFOS: Mapping[str, SeparatorInfo] = MappingProxyType(
    {info.model_id: info for info in (FAKE_VOCALS_INFO, FAKE_STANDARD_INFO)}
)
"""Every built-in fake model, keyed by model ID.

These mirror ``models/catalog.json`` exactly (a test asserts it). The catalog
*service* is feature 010 — this mapping is only so the fake engine and the
catalog file cannot disagree about stems or sample rate.
"""

DEFAULT_CHUNK_SECONDS = 5.0
"""Audio seconds per simulated chunk — the progress granularity."""

DEFAULT_CHUNK_DELAY_SECONDS = 0.05
"""Simulated per-chunk compute delay; set to ``0.0`` in tests."""

DEFAULT_MODEL_LOAD_SECONDS = 0.1
"""Simulated weight-loading delay for the ``loading_model`` stage."""

DEFAULT_FADE_SECONDS = 0.01
"""Fade applied to both ends of every stem during ``post_processing``."""

_DIRECT_WEIGHT = 0.6
_REFLECT_WEIGHT = 0.4
_BASE_GAIN = 0.9
_GAIN_FALLOFF = 0.85
_BASE_COMB_HZ = 110.0

_BASE_VRAM_BYTES = 512 * 1024**2
_VRAM_PER_STEM_BYTES = 96 * 1024**2
_VRAM_WOBBLE_BYTES = 8 * 1024**2


@dataclass(frozen=True, slots=True)
class FakeDeviceProfile:
    """The pretend compute device :class:`FakeSeparator` reports statistics for.

    The defaults describe an explicitly fake backend rather than impersonating
    a real GPU: ``backend`` is an open set (ARCHITECTURE.md §10), so a
    ``"fake"`` device is a legitimate value and nobody is misled into thinking
    a GPU is present. Override the fields to exercise UI that expects
    CUDA-shaped values.
    """

    device_id: str = "fake:0"
    name: str = "Straticate Fake Accelerator"
    backend: str = FAKE_ARCHITECTURE
    memory_total_bytes: int = 8 * 1024**3


DEFAULT_FAKE_DEVICE = FakeDeviceProfile()
"""The device :class:`FakeSeparator` pretends to run on unless told otherwise."""


def fake_separator_info(model_id: str) -> SeparatorInfo:
    """Return the built-in fake descriptor for ``model_id``.

    Raises:
        KeyError: ``model_id`` is not a built-in fake model.
    """
    return FAKE_SEPARATOR_INFOS[model_id]


def fake_separator_info_for_mode(mode_id: str) -> SeparatorInfo:
    """Return the built-in fake descriptor serving separation mode ``mode_id``.

    A stand-in for the catalog lookup of feature 010, so callers (tests, the
    dev wiring of feature 015) can get "the fake model for the vocals mode"
    without a catalog service.

    Raises:
        KeyError: No built-in fake model serves ``mode_id``.
    """
    for info in FAKE_SEPARATOR_INFOS.values():
        if info.separation_mode == mode_id:
            return info
    raise KeyError(mode_id)


class _CombFilter:
    """Stateful feed-forward comb filter over one channel of 16-bit PCM.

    Computes ``y[n] = direct * x[n] + reflect * x[n - delay]``, carrying the
    trailing ``delay`` samples between calls so the result is independent of
    how the signal was chunked.
    """

    __slots__ = ("_delay", "_direct", "_reflect", "_tail")

    def __init__(self, delay: int, gain: float, sign: int) -> None:
        self._delay = max(delay, 1)
        self._direct = gain * _DIRECT_WEIGHT
        self._reflect = gain * _REFLECT_WEIGHT * sign
        self._tail: array[int] = array("h", bytes(2 * self._delay))

    def process(self, chunk: array[int]) -> array[int]:
        """Filter one chunk and return it, updating the carried state."""
        source = self._tail + chunk
        delayed = source[: len(chunk)]
        self._tail = source[len(chunk) :]
        direct = self._direct
        reflect = self._reflect
        values = [int(direct * a + reflect * b) for a, b in zip(chunk, delayed, strict=True)]
        if values and (max(values) > INT16_MAX or min(values) < INT16_MIN):
            # Unreachable with the shipped coefficients (|y| <= 0.9 full
            # scale); kept so a future re-tuning cannot corrupt a WAV file.
            values = [min(max(value, INT16_MIN), INT16_MAX) for value in values]
        return array("h", values)


@dataclass(slots=True)
class _RunState:
    """Mutable bookkeeping for the separation currently in flight."""

    job_id: str
    stage: JobState
    chunks_total: int
    audio_total_seconds: float
    started_monotonic: float
    chunks_completed: int = 0
    audio_processed_seconds: float = 0.0
    chunk_seconds_total: float = 0.0
    last_chunk_seconds: float | None = None
    allocated_bytes: int = 0
    peak_bytes: int = 0
    finished_seconds: float | None = None


class FakeSeparator:
    """A :class:`~straticate.inference.base.Separator` that fakes the model, not the plumbing.

    Args:
        info: The model this separator claims to be — normally
            :data:`FAKE_VOCALS_INFO` or :data:`FAKE_STANDARD_INFO`. The stem
            list comes from here, so two-stem and four-stem modes are the same
            code path and nothing is hardcoded to two stems.
        chunk_seconds: Audio seconds per simulated chunk (progress
            granularity). Does not affect the produced audio.
        chunk_delay_seconds: Simulated compute time per chunk. Keep the
            default for a demo where progress is visibly real-time; set to
            ``0.0`` in tests. The loop awaits it even at ``0.0``, which yields
            to the event loop and keeps cancellation responsive.
        model_load_seconds: Simulated weight-loading delay.
        fade_seconds: Length of the fade applied to both ends of every stem in
            the ``post_processing`` stage.
        device: The pretend device reported by :meth:`runtime_stats`; ``None``
            reports no device block at all (i.e. "running on CPU").
        ffmpeg_timeout_seconds: Bound for the FFmpeg/ffprobe subprocesses this
            separator's decode runs. A construction option rather than a
            ``separate()`` argument because the ``Separator`` protocol
            (ARCHITECTURE.md §7) is deliberately free of tool-specific
            parameters: *how* a separator gets PCM is its own business. The
            application passes ``Settings.ffmpeg_timeout_seconds`` in through
            :func:`~straticate.inference.registry.default_separator_builders`.
    """

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
        chunk_delay_seconds: float = DEFAULT_CHUNK_DELAY_SECONDS,
        model_load_seconds: float = DEFAULT_MODEL_LOAD_SECONDS,
        fade_seconds: float = DEFAULT_FADE_SECONDS,
        device: FakeDeviceProfile | None = DEFAULT_FAKE_DEVICE,
        ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        if chunk_seconds <= 0:
            raise ValueError("chunk_seconds must be positive")
        if chunk_delay_seconds < 0 or model_load_seconds < 0 or fade_seconds < 0:
            raise ValueError("delays and fade lengths must not be negative")
        if ffmpeg_timeout_seconds <= 0:
            raise ValueError("ffmpeg_timeout_seconds must be positive")
        self._info = info
        self._chunk_seconds = chunk_seconds
        self._chunk_delay_seconds = chunk_delay_seconds
        self._model_load_seconds = model_load_seconds
        self._fade_seconds = fade_seconds
        self._device = device
        self._ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self._active = False
        self._run: _RunState | None = None

    @property
    def ffmpeg_timeout_seconds(self) -> float:
        """The bound this separator applies to its decode subprocesses.

        Exposed so the wiring is assertable: an application built with explicit
        settings must really govern its subprocesses, and a test should not
        have to read a private attribute to prove it.
        """
        return self._ffmpeg_timeout_seconds

    # -- Separator protocol -------------------------------------------------

    @property
    def info(self) -> SeparatorInfo:
        """The model descriptor this separator advertises."""
        return self._info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        """Snapshot of the current (or most recent) run; ``None`` before the first.

        This is the accessor feature 019's telemetry sampler polls. The
        numbers are fabricated but internally consistent: allocation grows
        with the stem count, peak never decreases, utilisation and temperature
        vary deterministically with the chunk index, and the RTF is computed
        from real elapsed time and the audio actually processed.
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
            device=self._device_stats(run),
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
        """Produce placeholder stems for ``input_path``. See :class:`FakeSeparator`.

        Stages announced, in order: ``decoding`` → ``loading_model`` →
        ``separating`` → ``post_processing`` → ``encoding``. Every one of them
        corresponds to work this separator really does.

        Raises:
            RuntimeError: A separation is already running on this instance.
            JobCancelled: Cancellation was observed.
            ApplicationError: ``audio_decode_failed`` if the input cannot be
                decoded, ``audio_decode_timed_out`` if FFmpeg exceeded its
                bounded run time, ``separation_mode_mismatch`` if the requested
                mode is not the one this separator serves.
        """
        if self._active:
            raise RuntimeError("FakeSeparator supports one separation at a time")
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
        started = time.monotonic()
        run = _RunState(
            job_id=job_id,
            stage=JobState.DECODING,
            chunks_total=0,
            audio_total_seconds=0.0,
            started_monotonic=started,
        )
        self._run = run
        try:
            _announce(stage_callback, JobState.DECODING)
            source = await self._decode(input_path)
            # The job's stereo-handling choice (feature 041), at the same point
            # and through the same pure function as the real separators. This
            # engine's *audio* is a placeholder, but what it reports about its
            # own behaviour must be true: a job that asked for the fold and got
            # two-channel stems back would be the application lying about what
            # it did, which is exactly what feature 032 exists to prevent. The
            # default is identity, so nothing that does not ask for it changes.
            source = await asyncio.to_thread(
                apply_stereo_handling, source, configuration.stereo_handling
            )
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.LOADING_MODEL)
            run.stage = JobState.LOADING_MODEL
            self._allocate(run, chunk_index=0)
            await asyncio.sleep(self._model_load_seconds)
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.SEPARATING)
            run.stage = JobState.SEPARATING
            stems = await self._run_chunks(source, run, progress_callback, cancellation_token)

            _announce(stage_callback, JobState.POST_PROCESSING)
            run.stage = JobState.POST_PROCESSING
            self._apply_fades(stems, source.sample_rate)
            cancellation_token.raise_if_cancelled()

            _announce(stage_callback, JobState.ENCODING)
            run.stage = JobState.ENCODING
            written = await self._encode(stems, source, output_dir, cancellation_token)
        except BaseException:
            # Cancellation (or any failure) must never leave a partial stem
            # behind that a later reader would mistake for a finished output.
            _discard_outputs(output_dir, self._info.stems)
            raise

        run.finished_seconds = time.monotonic() - started
        processing_seconds = run.finished_seconds
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=written,
            metrics=SeparationResultMetrics(
                processing_seconds=processing_seconds,
                realtime_factor=_realtime_factor(source.duration_seconds, processing_seconds),
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
            # Its own code: "not decodable" is a claim about the audio, and a
            # tool that ran out of time never made one. The distinction reaches
            # the user, because a failed job's error code is what the UI shows.
            raise ApplicationError(
                "audio_decode_timed_out",
                "Decoding the input audio timed out.",
                status_code=504,
                detail={"timeout_seconds": exc.timeout_seconds},
            ) from exc

    async def _run_chunks(
        self,
        source: PcmAudio,
        run: _RunState,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
    ) -> list[list[array[int]]]:
        """Process the source chunk by chunk, reporting progress after each.

        Returns the per-stem, per-channel sample planes.
        """
        sample_rate = source.sample_rate
        channel_count = source.channel_count
        frames = source.frame_count
        chunk_frames = max(1, round(self._chunk_seconds * sample_rate))
        chunks_total = max(1, math.ceil(frames / chunk_frames))
        run.chunks_total = chunks_total
        run.audio_total_seconds = source.duration_seconds

        filters = [
            [
                _CombFilter(_comb_delay(index, sample_rate), _stem_gain(index), _stem_sign(index))
                for _ in range(channel_count)
            ]
            for index in range(self._info.stem_count)
        ]
        stems: list[list[array[int]]] = [
            [array("h") for _ in range(channel_count)] for _ in range(self._info.stem_count)
        ]

        self._report(progress_callback, run)
        for index in range(chunks_total):
            cancellation_token.raise_if_cancelled()
            chunk_started = time.monotonic()
            start = index * chunk_frames
            stop = min(start + chunk_frames, frames)
            planes = [plane[start:stop] for plane in source.channels]
            for stem_index, stem_filters in enumerate(filters):
                for channel_index, comb in enumerate(stem_filters):
                    stems[stem_index][channel_index].extend(comb.process(planes[channel_index]))
            # Awaited even at 0.0: it yields to the event loop, so a cancel
            # request lands before the next chunk and progress is observable.
            await asyncio.sleep(self._chunk_delay_seconds)

            run.last_chunk_seconds = time.monotonic() - chunk_started
            run.chunk_seconds_total += run.last_chunk_seconds
            run.chunks_completed = index + 1
            run.audio_processed_seconds = stop / sample_rate
            self._allocate(run, chunk_index=index + 1)
            self._report(progress_callback, run)
        return stems

    def _apply_fades(self, stems: list[list[array[int]]], sample_rate: int) -> None:
        """Fade both ends of every stem so playback never starts with a click."""
        fade_frames = int(self._fade_seconds * sample_rate)
        if fade_frames <= 0:
            return
        for planes in stems:
            for plane in planes:
                _fade_edges(plane, fade_frames)

    async def _encode(
        self,
        stems: list[list[array[int]]],
        source: PcmAudio,
        output_dir: Path,
        cancellation_token: CancellationToken,
    ) -> list[Stem]:
        """Write one WAV per stem and describe them for the result record."""
        written: list[Stem] = []
        for name, planes in zip(self._info.stems, stems, strict=True):
            cancellation_token.raise_if_cancelled()
            audio = PcmAudio(sample_rate=source.sample_rate, channels=tuple(planes))
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

    def _allocate(self, run: _RunState, *, chunk_index: int) -> None:
        """Update the pretend VRAM figures deterministically."""
        allocated = (
            _BASE_VRAM_BYTES
            + _VRAM_PER_STEM_BYTES * self._info.stem_count
            + _VRAM_WOBBLE_BYTES * (chunk_index % 8)
        )
        run.allocated_bytes = allocated
        run.peak_bytes = max(run.peak_bytes, allocated)

    def _device_stats(self, run: _RunState) -> DeviceStats | None:
        device = self._device
        if device is None:
            return None
        completed = run.chunks_completed
        return DeviceStats(
            device_id=device.device_id,
            name=device.name,
            backend=device.backend,
            memory_allocated_bytes=run.allocated_bytes,
            memory_peak_bytes=run.peak_bytes,
            memory_total_bytes=device.memory_total_bytes,
            utilization=round(0.72 + 0.045 * (completed % 5), 3),
            temperature_celsius=round(58.0 + 2.0 * (completed % 4), 1),
        )


def _announce(stage_callback: StageCallback | None, stage: JobState) -> None:
    if stage_callback is not None:
        stage_callback(stage)


def _stem_gain(index: int) -> float:
    """Per-stem output gain — distinct levels, always below full scale."""
    return _BASE_GAIN * _GAIN_FALLOFF**index


def _stem_sign(index: int) -> int:
    """Polarity of the comb reflection: even stems darker, odd stems brighter."""
    return 1 if index % 2 == 0 else -1


def _comb_delay(index: int, sample_rate: int) -> int:
    """Comb delay in samples: notch spacing one octave apart per stem."""
    return max(1, round(sample_rate / (_BASE_COMB_HZ * 2**index)))


def _fade_edges(plane: array[int], fade_frames: int) -> None:
    """Apply a linear fade in and out to ``plane`` in place."""
    length = len(plane)
    fade = min(fade_frames, length // 2)
    for offset in range(fade):
        factor = (offset + 1) / (fade + 1)
        plane[offset] = int(plane[offset] * factor)
        plane[length - 1 - offset] = int(plane[length - 1 - offset] * factor)


def _realtime_factor(audio_seconds: float, processing_seconds: float) -> float:
    """RTF = audio duration / processing duration (``0.0`` when not yet meaningful)."""
    if processing_seconds <= 0.0 or audio_seconds <= 0.0:
        return 0.0
    return audio_seconds / processing_seconds


def _discard_outputs(output_dir: Path, stems: tuple[str, ...]) -> None:
    """Remove any stem files this separator may have written under ``output_dir``."""
    for name in stems:
        for candidate in (output_dir / f"{name}.wav", output_dir / f"{name}.wav.part"):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best-effort cleanup
                logger.warning("Could not remove partial output %s", candidate)


__all__ = [
    "DEFAULT_CHUNK_DELAY_SECONDS",
    "DEFAULT_CHUNK_SECONDS",
    "DEFAULT_FADE_SECONDS",
    "DEFAULT_FAKE_DEVICE",
    "DEFAULT_MODEL_LOAD_SECONDS",
    "FAKE_ARCHITECTURE",
    "FAKE_SEPARATOR_INFOS",
    "FAKE_STANDARD_INFO",
    "FAKE_VOCALS_INFO",
    "FakeDeviceProfile",
    "FakeSeparator",
    "fake_separator_info",
    "fake_separator_info_for_mode",
]
