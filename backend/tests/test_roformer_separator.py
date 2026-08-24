"""Tests for the real separator's plumbing, against a synthetic checkpoint.

Everything here runs with no GPU, no network and no model download: the network
under test is the ~20 000-parameter stand-in from :mod:`tests.roformer_fixtures`,
whose audio is meaningless and whose *behaviour under the ``Separator``
contract* is identical to the real one's. What the real weights add is quality,
not control flow.

Conventions follow the rest of the suite: WAV fixtures are generated, every wait
is gated by an :class:`asyncio.Event` or a :class:`threading.Event`, and nothing
sleeps for a duration as a way of synchronising.
"""

import asyncio
import hashlib
import sys
import threading
from array import array
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import torch

from straticate.errors import ApplicationError
from straticate.inference.base import SeparationProgress
from straticate.inference.pcm import PcmAudio, decode_to_pcm, interleave
from straticate.inference.roformer import NvmlProbe, RoFormerParameters, RoFormerSeparator
from straticate.inference.roformer import separator as separator_module
from straticate.inference.roformer.separator import (
    device_stats,
    pcm_to_tensor,
    reset_peak_memory,
    tensor_to_pcm,
)
from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.schemas.jobs import JobState, SeparationConfiguration, SeparationResult
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav
from tests.roformer_fixtures import (
    TINY_CHUNK_SAMPLES,
    TINY_SAMPLE_RATE,
    tiny_catalog_block,
    tiny_info,
    tiny_parameters,
    write_tiny_weights,
)

JOB_ID = "01JOB00000000000000000026"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class ProgressRecorder:
    """Collects every progress report, and which thread it arrived on."""

    def __init__(self) -> None:
        self.reports: list[SeparationProgress] = []
        self.threads: set[int] = set()

    def __call__(self, progress: SeparationProgress) -> None:
        self.reports.append(progress)
        self.threads.add(threading.get_ident())


class StageRecorder:
    """Collects the stages a separator announces, in order."""

    def __init__(self) -> None:
        self.stages: list[JobState] = []

    def __call__(self, stage: JobState) -> None:
        self.stages.append(stage)


def make_configuration(
    mode_id: str = "vocals", device_id: str | None = None
) -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id="high_quality",
        device_id=device_id,
    )


@pytest.fixture
def weights(tmp_path: Path) -> Path:
    """A saved synthetic checkpoint, where the installer would have put one."""
    return write_tiny_weights(tmp_path / "weights" / "tiny-vocals-001" / "weights.bin")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Two seconds of stereo tone — a handful of chunks at the tiny chunk size."""
    return write_tone_wav(tmp_path / "source.wav", seconds=2.0, channels=2, sample_rate=22050)


def make_separator(weights: Path, **overrides: Any) -> RoFormerSeparator:
    info = overrides.pop("info", tiny_info())
    parameters = overrides.pop("parameters", tiny_parameters(**overrides.pop("tuning", {})))
    return RoFormerSeparator(info, weights_file=weights, parameters=parameters, **overrides)


async def run(
    separator: RoFormerSeparator,
    source: Path,
    output_dir: Path,
    *,
    token: CancellationToken | None = None,
    progress: ProgressRecorder | None = None,
    stages: StageRecorder | None = None,
    configuration: SeparationConfiguration | None = None,
) -> SeparationResult:
    return await separator.separate(
        source,
        configuration or make_configuration(),
        progress or ProgressRecorder(),
        token or CancellationToken(),
        job_id=JOB_ID,
        output_dir=output_dir,
        stage_callback=stages or StageRecorder(),
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# A complete run
# --------------------------------------------------------------------------


async def test_a_run_writes_one_playable_wav_per_advertised_stem(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "stems"
    result = await run(make_separator(weights), source, output)

    assert [stem.name for stem in result.stems] == ["vocals", "instrumental"]
    assert sorted(path.name for path in output.iterdir()) == ["instrumental.wav", "vocals.wav"]
    for stem in result.stems:
        channels, rate, frames, samples = read_wav(output / f"{stem.name}.wav")
        assert (channels, rate) == (2, TINY_SAMPLE_RATE)
        assert frames == pytest.approx(2.0 * TINY_SAMPLE_RATE, rel=0.01)
        assert peak_amplitude(samples) > 0, f"{stem.name} is digital silence"
        assert stem.sample_rate_hz == TINY_SAMPLE_RATE
        assert stem.channels == 2
        assert stem.duration_seconds == pytest.approx(2.0, rel=0.01)


async def test_the_result_carries_real_performance_metrics(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    result = await run(make_separator(weights), source, tmp_path / "stems")
    assert result.job_id == JOB_ID
    assert result.model_id == "tiny-vocals-001"
    assert result.metrics.processing_seconds > 0.0
    assert result.metrics.realtime_factor > 0.0
    # RTF is audio over processing, both measured in this run.
    assert result.metrics.realtime_factor == pytest.approx(
        2.0 / result.metrics.processing_seconds, rel=0.05
    )


async def test_stems_are_separated_output_not_copies_of_the_input(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """Whatever the (random) network decides, the two stems are not the same file.

    With real weights this is the acceptance criterion "vocals audibly distinct
    from the instrumental"; with synthetic weights it is the weaker but still
    load-bearing claim that the mask is applied, the residual is computed, and
    neither stem is the mixture handed back.
    """
    output = tmp_path / "stems"
    await run(make_separator(weights), source, output)

    vocals = digest(output / "vocals.wav")
    instrumental = digest(output / "instrumental.wav")
    assert vocals != instrumental
    assert vocals != digest(source)
    assert instrumental != digest(source)


async def test_the_stems_reconstruct_the_mixture(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """``vocals + instrumental`` is the decoded mixture, to within quantization.

    That is what makes the second stem a *residual* rather than a second guess:
    the subtraction happens in the float domain, before either stem is rounded
    to 16 bits, so the two round-trips are the only error.
    """
    output = tmp_path / "stems"
    await run(make_separator(weights), source, output)
    _, _, _, vocals = read_wav(output / "vocals.wav")
    _, _, _, instrumental = read_wav(output / "instrumental.wav")

    mixture = interleave(
        await decode_to_pcm(source, sample_rate=TINY_SAMPLE_RATE, timeout_seconds=60)
    )
    assert len(vocals) == len(instrumental) == len(mixture)
    worst = max(abs(v + i - m) for v, i, m in zip(vocals, instrumental, mixture, strict=True))
    assert worst <= 2, f"reconstruction error of {worst} LSB is more than rounding"


async def test_a_mono_source_yields_mono_stems(weights: Path, tmp_path: Path) -> None:
    """A stereo-only network must not change how many channels a job returns."""
    mono = write_tone_wav(tmp_path / "mono.wav", seconds=1.0, channels=1, sample_rate=16000)
    output = tmp_path / "stems"
    result = await run(make_separator(weights), mono, output)

    for stem in result.stems:
        assert stem.channels == 1
        channels, _, _, samples = read_wav(output / f"{stem.name}.wav")
        assert channels == 1
        assert peak_amplitude(samples) > 0


async def test_nothing_partial_is_left_behind(weights: Path, source: Path, tmp_path: Path) -> None:
    output = tmp_path / "stems"
    await run(make_separator(weights), source, output)
    assert not list(output.glob("*.part"))


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


async def test_it_announces_exactly_the_stages_it_performs(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    stages = StageRecorder()
    await run(make_separator(weights), source, tmp_path / "stems", stages=stages)
    assert stages.stages == [
        JobState.DECODING,
        JobState.LOADING_MODEL,
        JobState.SEPARATING,
        JobState.POST_PROCESSING,
        JobState.ENCODING,
    ]


# --------------------------------------------------------------------------
# Progress is real work
# --------------------------------------------------------------------------


async def test_progress_is_chunk_grained_and_monotonic(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    progress = ProgressRecorder()
    await run(make_separator(weights), source, tmp_path / "stems", progress=progress)

    reports = progress.reports
    assert len(reports) >= 2
    total = reports[-1].chunks_total
    assert total > 1, "the fixture should be long enough to need several chunks"
    # The opening report announces the total before any work is claimed.
    assert (reports[0].chunks_completed, reports[0].chunks_total) == (0, total)
    assert [report.chunks_completed for report in reports] == list(range(len(reports)))
    assert all(report.chunks_total == total for report in reports)
    assert reports[-1].chunks_completed == total
    assert reports[-1].fraction == 1.0
    processed = [report.audio_processed_seconds for report in reports]
    assert processed == sorted(processed)
    assert processed[-1] == pytest.approx(reports[-1].audio_total_seconds, rel=0.01)


async def test_the_chunk_count_follows_the_configured_chunk_size(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """Progress granularity is the model's chunking, not a timer."""
    counts: list[int] = []
    for chunk_samples in (TINY_CHUNK_SAMPLES, TINY_CHUNK_SAMPLES // 2):
        progress = ProgressRecorder()
        separator = make_separator(weights, tuning={"chunk_samples": chunk_samples})
        await run(separator, source, tmp_path / f"stems-{chunk_samples}", progress=progress)
        counts.append(progress.reports[-1].chunks_total)

    assert counts[1] > counts[0]


async def test_progress_arrives_from_a_worker_thread(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """The chunk loop is offloaded, which is what the executor's marshalling is for."""
    progress = ProgressRecorder()
    await run(make_separator(weights), source, tmp_path / "stems", progress=progress)
    assert threading.get_ident() not in progress.threads


async def test_the_event_loop_keeps_running_while_a_separation_does(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """A separation must never monopolise the loop the job manager runs on.

    The gate is the cancellation token, which the chunk loop consults once per
    chunk *from its worker thread*: parking the first call there proves the loop
    is free, because a task scheduled afterwards still gets to run.
    """
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    class GatedToken(CancellationToken):
        def __init__(self) -> None:
            super().__init__()
            self._first = True

        def raise_if_cancelled(self) -> None:
            if self._first:
                self._first = False
                loop.call_soon_threadsafe(entered.set)
                release.wait(timeout=30)
            super().raise_if_cancelled()

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(tick())
    separation = asyncio.create_task(
        run(make_separator(weights), source, tmp_path / "stems", token=GatedToken())
    )
    await entered.wait()
    # The loop is alive while the worker thread is parked inside the chunk loop.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ticks > 0
    release.set()
    await ticker
    await separation


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------


async def test_cancellation_stops_at_a_chunk_boundary_and_leaves_no_stem(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "stems"
    token = CancellationToken()

    class CancelAfterFirstChunk(ProgressRecorder):
        def __call__(self, report: SeparationProgress) -> None:
            super().__call__(report)
            if report.chunks_completed == 1:
                token.cancel()

    recorder = CancelAfterFirstChunk()
    with pytest.raises(JobCancelled):
        await run(make_separator(weights), source, output, token=token, progress=recorder)

    # It stopped at the boundary after the first chunk, not at the end.
    assert recorder.reports[-1].chunks_completed == 1
    assert recorder.reports[-1].chunks_completed < recorder.reports[-1].chunks_total
    assert not output.exists() or not list(output.iterdir())


async def test_cancellation_before_the_first_chunk_writes_nothing(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    output = tmp_path / "stems"
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        await run(make_separator(weights), source, output, token=token)
    assert not output.exists() or not list(output.iterdir())


async def test_a_failure_removes_every_stem_it_had_already_written(
    weights: Path, source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first stem is written before the second fails; neither may survive."""
    output = tmp_path / "stems"
    original = separator_module.write_wav
    written = 0

    def exploding_write(path: Path, audio: PcmAudio) -> None:
        nonlocal written
        written += 1
        if written > 1:
            raise OSError("disk full")
        original(path, audio)

    monkeypatch.setattr(separator_module, "write_wav", exploding_write)

    with pytest.raises(OSError, match="disk full"):
        await run(make_separator(weights), source, output)

    assert written == 2, "the second stem must have been attempted"
    assert not output.exists() or not list(output.iterdir())


# --------------------------------------------------------------------------
# Runtime statistics
# --------------------------------------------------------------------------


async def test_runtime_stats_are_absent_before_the_first_run_and_real_after(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    separator = make_separator(weights)
    assert separator.runtime_stats() is None

    await run(separator, source, tmp_path / "stems")

    stats = separator.runtime_stats()
    assert stats is not None
    assert stats.job_id == JOB_ID
    assert stats.model == separator.info
    # CPU: the contract renders "no device block" as ``gpu: null``.
    assert stats.device is None
    processing = stats.processing
    assert processing.stage == JobState.ENCODING
    assert processing.chunks_completed == processing.chunks_total > 0
    assert processing.elapsed_seconds > 0.0
    assert processing.audio_processed_seconds == pytest.approx(2.0, rel=0.01)
    assert processing.realtime_factor > 0.0
    assert processing.last_chunk_seconds is not None
    assert processing.mean_chunk_seconds is not None
    assert processing.to_processing_metrics().stage == JobState.ENCODING


async def test_runtime_stats_track_the_stage_while_a_run_is_in_flight(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    separator = make_separator(weights)
    seen: list[JobState] = []

    class Observing(ProgressRecorder):
        def __call__(self, report: SeparationProgress) -> None:
            super().__call__(report)
            stats = separator.runtime_stats()
            assert stats is not None
            seen.append(stats.processing.stage)

    await run(separator, source, tmp_path / "stems", progress=Observing())
    assert seen and set(seen) == {JobState.SEPARATING}


# --------------------------------------------------------------------------
# Weights and error mapping
# --------------------------------------------------------------------------


async def test_absent_weights_are_a_409_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(
            tiny_info(),
            weights_file=tmp_path / "weights" / "tiny-vocals-001" / "weights.bin",
            parameters=tiny_parameters(),
        )
    error = excinfo.value
    assert error.code == "model_weights_missing"
    assert error.status_code == 409
    assert error.detail == {"model_id": "tiny-vocals-001"}


async def test_weights_that_do_not_match_the_architecture_are_rejected(tmp_path: Path) -> None:
    """``strict=True``: a partial load would produce plausible-sounding nonsense."""
    other = write_tiny_weights(tmp_path / "other.bin", num_bands=16)
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(tiny_info(), weights_file=other, parameters=tiny_parameters())
    error = excinfo.value
    assert error.code == "model_weights_invalid"
    assert error.status_code == 500
    assert error.detail is not None
    assert error.detail["model_id"] == "tiny-vocals-001"


async def test_an_unreadable_weights_file_is_reported_not_raised_raw(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.bin"
    corrupt.write_bytes(b"this is not a checkpoint")
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(tiny_info(), weights_file=corrupt, parameters=tiny_parameters())
    assert excinfo.value.code == "model_weights_invalid"


async def test_a_mode_it_does_not_serve_is_a_wiring_bug(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        await run(
            make_separator(weights),
            source,
            tmp_path / "stems",
            configuration=make_configuration(mode_id="standard_stems"),
        )
    error = excinfo.value
    assert error.code == "separation_mode_mismatch"
    assert error.status_code == 400


async def test_undecodable_input_is_a_422(weights: Path, tmp_path: Path) -> None:
    broken = tmp_path / "not-audio.wav"
    broken.write_bytes(b"RIFFnope")
    with pytest.raises(ApplicationError) as excinfo:
        await run(make_separator(weights), broken, tmp_path / "stems")
    assert excinfo.value.code == "audio_decode_failed"
    assert excinfo.value.status_code == 422


async def test_a_cuda_device_on_a_cpu_only_host_is_a_clear_409(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    if torch.cuda.is_available():  # pragma: no cover - CI and this host are CPU-only
        pytest.skip("this host has CUDA, so the unavailable path cannot be exercised")
    with pytest.raises(ApplicationError) as excinfo:
        await run(
            make_separator(weights),
            source,
            tmp_path / "stems",
            configuration=make_configuration(device_id="cuda:0"),
        )
    error = excinfo.value
    assert error.code == "compute_device_unavailable"
    assert error.status_code == 409
    assert error.detail is not None
    assert error.detail["device_id"] == "cuda:0"


async def test_one_separation_at_a_time(weights: Path, source: Path, tmp_path: Path) -> None:
    separator = make_separator(weights)
    first = asyncio.create_task(run(separator, source, tmp_path / "a"))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="one separation at a time"):
        await run(separator, source, tmp_path / "b")
    await first


def test_it_refuses_a_nonsense_ffmpeg_timeout(weights: Path) -> None:
    with pytest.raises(ValueError, match="ffmpeg_timeout_seconds"):
        make_separator(weights, ffmpeg_timeout_seconds=0.0)


# --------------------------------------------------------------------------
# Catalog parameters
# --------------------------------------------------------------------------


def test_parameters_come_from_the_catalog_block() -> None:
    parameters = RoFormerParameters.from_catalog(tiny_catalog_block(), model_id="tiny-vocals-001")
    assert parameters.chunk_samples == TINY_CHUNK_SAMPLES
    assert parameters.num_overlap == 2
    assert parameters.num_stems == 1
    assert parameters.audio_channels == 2
    assert parameters.model["num_bands"] == 8


def test_json_arrays_become_the_tuples_the_architecture_demands() -> None:
    block = tiny_catalog_block(model={"multi_stft_resolutions_window_sizes": [4096, 2048]})
    parameters = RoFormerParameters.from_catalog(block, model_id="m-001")
    assert parameters.model["multi_stft_resolutions_window_sizes"] == (4096, 2048)


def test_chunking_defaults_apply_when_the_catalog_omits_them() -> None:
    parameters = RoFormerParameters.from_catalog(
        {"model": dict(tiny_catalog_block()["model"])}, model_id="m-001"
    )
    assert parameters.chunk_samples == 352800
    assert parameters.num_overlap == 2
    assert parameters.residual_stem is None


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (None, "no default_inference_parameters"),
        ({}, "no default_inference_parameters"),
        ({"inference": {}}, "must be an object"),
        ({"model": []}, "must be an object"),
        ({"model": {"n_fft": 2048}}, "unknown architecture parameters: n_fft"),
        ({"model": {}, "inference": {"chunk_size": 0}}, "positive integer"),
        ({"model": {}, "inference": {"num_overlap": "two"}}, "positive integer"),
        ({"model": {}, "output": {"residual_stem": 7}}, "must be a stem name"),
    ],
)
def test_an_unusable_catalog_entry_fails_loudly(block: Any, fragment: str) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerParameters.from_catalog(block, model_id="m-001")
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert error.status_code == 500
    assert fragment in error.message


def test_a_stem_list_the_network_cannot_produce_is_refused(weights: Path) -> None:
    """Two advertised stems from a one-stem network is fine; four is not."""
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(
            tiny_info(stems=("vocals", "drums", "bass", "other"), separation_mode="standard_stems"),
            weights_file=weights,
            parameters=tiny_parameters(),
        )
    assert excinfo.value.code == "model_parameters_invalid"
    assert "produces 1" in excinfo.value.message


# --------------------------------------------------------------------------
# The residual stem is named by the catalog, never inferred from position
# --------------------------------------------------------------------------


async def test_a_reordered_stem_list_maps_correctly_rather_than_silently_swapping(
    weights: Path, tmp_path: Path
) -> None:
    """``["instrumental", "vocals"]`` must not put the network's vocals in
    ``instrumental.wav``.

    The manifest schema imposes no order on ``stems``, and adding a checkpoint is
    advertised as a pure data edit, so this ordering is a maintainer's free
    choice. Before the residual was named, the residual was assumed to be the
    *last* stem and this entry produced two files with each other's audio and no
    error anywhere.

    The check is content, not naming: the same audio is separated twice, once
    with each stem order, and the file called ``vocals.wav`` must come out
    byte-identical both times.
    """
    source = write_tone_wav(tmp_path / "source.wav", seconds=1.0, channels=2, sample_rate=16000)

    forward = tmp_path / "forward"
    await run(
        RoFormerSeparator(
            tiny_info(stems=("vocals", "instrumental")),
            weights_file=weights,
            parameters=tiny_parameters(residual_stem="instrumental"),
        ),
        source,
        forward,
    )

    reversed_order = tmp_path / "reversed"
    await run(
        RoFormerSeparator(
            tiny_info(stems=("instrumental", "vocals")),
            weights_file=weights,
            parameters=tiny_parameters(residual_stem="instrumental"),
        ),
        source,
        reversed_order,
    )

    assert digest(forward / "vocals.wav") == digest(reversed_order / "vocals.wav")
    assert digest(forward / "instrumental.wav") == digest(reversed_order / "instrumental.wav")
    assert digest(forward / "vocals.wav") != digest(forward / "instrumental.wav")


def test_a_residual_that_is_needed_but_not_named_is_refused(weights: Path) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(
            tiny_info(),
            weights_file=weights,
            parameters=tiny_parameters(residual_stem=None),
        )
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert "output.residual_stem must name" in error.message
    assert "vocals, instrumental" in error.message


def test_a_residual_the_model_does_not_advertise_is_refused(weights: Path) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(
            tiny_info(),
            weights_file=weights,
            parameters=tiny_parameters(residual_stem="drums"),
        )
    assert excinfo.value.code == "model_parameters_invalid"
    assert "does not" in excinfo.value.message


def test_a_residual_named_for_a_network_that_needs_none_is_refused(tmp_path: Path) -> None:
    """Naming one when the network emits every stem is a contradiction, not a hint."""
    weights = write_tiny_weights(tmp_path / "two-stem.bin", num_stems=2)
    with pytest.raises(ApplicationError) as excinfo:
        RoFormerSeparator(
            tiny_info(),
            weights_file=weights,
            parameters=tiny_parameters(model={"num_stems": 2}, residual_stem="instrumental"),
        )
    assert excinfo.value.code == "model_parameters_invalid"
    assert "none is a residual" in excinfo.value.message


def test_the_repository_catalog_names_its_residual_stem() -> None:
    """The shipped entry must satisfy the rule the code enforces."""
    from straticate.config import Settings
    from straticate.models import ModelCatalog

    catalog = ModelCatalog.from_directory(Settings().models_dir)
    parameters = RoFormerParameters.from_catalog(
        catalog.inference_parameters("vocals-hq-001"), model_id="vocals-hq-001"
    )
    model = catalog.get_model("vocals-hq-001")
    assert parameters.residual_stem == "instrumental"
    assert parameters.residual_stem in model.stems
    assert parameters.num_stems == len(model.stems) - 1


async def test_a_network_that_emits_every_stem_needs_no_residual(tmp_path: Path) -> None:
    """``num_stems == len(stems)`` maps one-to-one, with nothing subtracted."""
    weights = write_tiny_weights(tmp_path / "two-stem.bin", num_stems=2)
    separator = RoFormerSeparator(
        tiny_info(),
        weights_file=weights,
        parameters=tiny_parameters(model={"num_stems": 2}, residual_stem=None),
    )
    source = write_tone_wav(tmp_path / "source.wav", seconds=1.0, channels=2, sample_rate=16000)
    output = tmp_path / "stems"
    result = await run(separator, source, output)
    assert [stem.name for stem in result.stems] == ["vocals", "instrumental"]
    assert digest(output / "vocals.wav") != digest(output / "instrumental.wav")


def test_the_separator_exposes_what_it_was_configured_with(weights: Path) -> None:
    separator = make_separator(weights, ffmpeg_timeout_seconds=12.5)
    assert separator.ffmpeg_timeout_seconds == 12.5
    assert separator.parameters.chunk_samples == TINY_CHUNK_SAMPLES
    assert separator.info.architecture == "mel_band_roformer"


def test_planar_conversion_round_trips_through_the_pcm_module() -> None:
    """Guards the int16-to-float bridge every stem is written through."""
    original = PcmAudio(
        sample_rate=TINY_SAMPLE_RATE,
        channels=(array("h", [0, 1000, -1000, 32767, -32768]), array("h", [5, -5, 15, -15, 25])),
    )
    tensor = pcm_to_tensor(original, 2)
    restored = tensor_to_pcm(tensor, TINY_SAMPLE_RATE)
    # -32768 clamps to -32767: one LSB, and the only value that cannot survive a
    # symmetric float scaling. Everything else is exact.
    assert list(interleave(restored))[:8] == list(interleave(original))[:8]
    assert restored.channels[0][4] == -32767


# --------------------------------------------------------------------------
# The CUDA telemetry path, exercised on a CPU-only host through one seam
# --------------------------------------------------------------------------
#
# Everything below replaces ``separator_module.cuda_namespace`` with a double.
# It is the only way this code gets covered before it meets real hardware, and
# feature 026 shipped it explicitly unrun — so the parts that are checkable
# without a GPU are checked here rather than left to a reviewer's reading.


class FakeProperties:
    """What ``torch.cuda.get_device_properties`` gives us, minus everything else."""

    def __init__(self, name: str, total_memory: int) -> None:
        self.name = name
        self.total_memory = total_memory


class FakeCuda:
    """A recording stand-in for the ``torch.cuda`` namespace."""

    def __init__(self, *, allocated: int = 1 << 30, peak: int = 3 << 30) -> None:
        self.allocated = allocated
        self.peak = peak
        self.resets: list[str] = []

    def is_available(self) -> bool:
        return True

    def get_device_properties(self, index: int) -> FakeProperties:
        return FakeProperties(f"NVIDIA Fake {index}", 8 << 30)

    def memory_allocated(self, index: int) -> int:
        return self.allocated

    def max_memory_allocated(self, index: int) -> int:
        return self.peak

    def reset_peak_memory_stats(self, device: object) -> None:
        self.resets.append(str(device))
        # What torch really does: the high-water mark drops to what is
        # currently allocated, so the resident model still counts.
        self.peak = self.allocated


class FakeAtexit:
    """Captures teardown registrations instead of leaking them into the session."""

    def __init__(self) -> None:
        self.hooks: list[object] = []

    def register(self, hook: Any) -> Any:
        self.hooks.append(hook)
        return hook


class FakeNvml:
    """A ``pynvml`` double that counts how often it is initialised."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self) -> None:
        self.inits = 0
        self.shutdowns = 0
        self.handles = 0
        self.samples = 0

    def nvmlInit(self) -> None:
        self.inits += 1

    def nvmlShutdown(self) -> None:
        self.shutdowns += 1

    def nvmlDeviceGetHandleByIndex(self, index: int) -> str:
        self.handles += 1
        return f"handle-{index}"

    def nvmlDeviceGetUtilizationRates(self, handle: str) -> Any:
        self.samples += 1
        return SimpleNamespace(gpu=63)

    def nvmlDeviceGetTemperature(self, handle: str, sensor: int) -> int:
        return 61


@pytest.fixture
def fake_cuda(monkeypatch: pytest.MonkeyPatch) -> FakeCuda:
    cuda = FakeCuda()
    monkeypatch.setattr(separator_module, "cuda_namespace", lambda: cuda)
    return cuda


def test_device_stats_report_the_devices_real_memory_figures(fake_cuda: FakeCuda) -> None:
    stats = device_stats(torch.device("cuda:1"))
    assert stats is not None
    assert stats.device_id == "cuda:1"
    assert stats.backend == "cuda"
    assert stats.name == "NVIDIA Fake 1"
    assert stats.memory_allocated_bytes == 1 << 30
    assert stats.memory_peak_bytes == 3 << 30
    assert stats.memory_total_bytes == 8 << 30
    # NVML absent: the two optional fields stay empty, everything else does not.
    assert stats.utilization is None
    assert stats.temperature_celsius is None
    assert stats.to_gpu_metrics().device_id == "cuda:1"


def test_device_stats_are_absent_on_cpu(fake_cuda: FakeCuda) -> None:
    """The contract renders "no device block" as ``gpu: null``."""
    assert device_stats(torch.device("cpu")) is None
    assert fake_cuda.resets == []


def test_reset_peak_memory_only_touches_cuda(fake_cuda: FakeCuda) -> None:
    reset_peak_memory(torch.device("cpu"))
    assert fake_cuda.resets == []
    reset_peak_memory(torch.device("cuda:0"))
    assert fake_cuda.resets == ["cuda:0"]


async def test_the_peak_is_reset_once_per_run_not_once_per_device(
    weights: Path, source: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short job after a long one must report **its own** peak.

    ``torch.cuda.max_memory_allocated`` is a per-device high-water mark that only
    an explicit reset clears, so a reset that happens once per *device placement*
    leaves the second run of a separator reporting the first run's peak. Two runs
    on one separator must reset twice.
    """
    resets: list[str] = []

    def record(device: torch.device) -> None:
        resets.append(str(device))

    monkeypatch.setattr(separator_module, "reset_peak_memory", record)

    separator = make_separator(weights)
    await run(separator, source, tmp_path / "first")
    await run(separator, source, tmp_path / "second")

    assert resets == ["cpu", "cpu"], "the peak measurement must restart with every separation"


def test_a_reset_restarts_the_high_water_mark_from_the_resident_allocation(
    fake_cuda: FakeCuda,
) -> None:
    """A previous run's peak does not survive the reset; the resident model does."""
    fake_cuda.allocated = 2 << 30
    fake_cuda.peak = 7 << 30
    before = device_stats(torch.device("cuda:0"))
    assert before is not None
    assert before.memory_peak_bytes == 7 << 30

    reset_peak_memory(torch.device("cuda:0"))

    after = device_stats(torch.device("cuda:0"))
    assert after is not None
    assert after.memory_peak_bytes == 2 << 30


# --------------------------------------------------------------------------
# NVML stays optional, and stays cheap
# --------------------------------------------------------------------------


def test_nvml_is_initialised_once_and_not_per_sample(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``runtime_stats()`` is called on the event loop, ~1 Hz, for a whole job.

    ``straticate.telemetry.sampler`` polls it directly on the loop because
    ``inference/base.py`` promises a "cheap, non-blocking snapshot". An
    ``nvmlInit``/``nvmlShutdown`` pair per sample is tens of milliseconds of
    driver setup in front of every WebSocket frame the loop owes somebody, so
    the binding is initialised once and the handles are cached.
    """
    nvml = FakeNvml()
    exit_hooks = FakeAtexit()
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(separator_module, "atexit", exit_hooks)
    monkeypatch.setattr(separator_module, "_NVML", NvmlProbe())

    for _ in range(5):
        stats = device_stats(torch.device("cuda:0"))
        assert stats is not None
        assert stats.utilization == 0.63
        assert stats.temperature_celsius == 61.0

    assert nvml.inits == 1, "NVML was re-initialised per sample"
    assert nvml.shutdowns == 0, "NVML was torn down while a job was still sampling"
    assert nvml.handles == 1, "the device handle was re-fetched per sample"
    assert nvml.samples == 5
    assert len(exit_hooks.hooks) == 1, "teardown must be registered once, at exit"


def test_nvml_shuts_down_at_teardown(fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch) -> None:
    nvml = FakeNvml()
    exit_hooks = FakeAtexit()
    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(separator_module, "atexit", exit_hooks)
    probe = NvmlProbe()
    monkeypatch.setattr(separator_module, "_NVML", probe)

    assert device_stats(torch.device("cuda:0")) is not None
    hook = exit_hooks.hooks[0]
    assert callable(hook)
    hook()
    assert nvml.shutdowns == 1
    # Idempotent: a second teardown is not a second shutdown.
    hook()
    assert nvml.shutdowns == 1


def test_a_missing_nvml_binding_costs_one_failed_import(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NVML is optional (ARCHITECTURE.md §12) and its absence is not an error."""
    attempts = 0

    def refuse(name: str) -> Any:
        nonlocal attempts
        attempts += 1
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(separator_module.importlib, "import_module", refuse)
    monkeypatch.setattr(separator_module, "_NVML", NvmlProbe())

    for _ in range(4):
        stats = device_stats(torch.device("cuda:0"))
        assert stats is not None
        assert stats.utilization is None
        assert stats.temperature_celsius is None
        # Everything that does not come from NVML is unaffected.
        assert stats.memory_total_bytes == 8 << 30

    assert attempts == 1, "an absent binding must not be re-imported on every sample"


def test_a_driver_failure_mid_job_does_not_break_the_snapshot(
    fake_cuda: FakeCuda, monkeypatch: pytest.MonkeyPatch
) -> None:
    nvml = FakeNvml()

    def explode(handle: str) -> Any:
        raise RuntimeError("NVML_ERROR_GPU_IS_LOST")

    monkeypatch.setitem(sys.modules, "pynvml", nvml)
    monkeypatch.setattr(separator_module, "atexit", FakeAtexit())
    monkeypatch.setattr(separator_module, "_NVML", NvmlProbe())
    monkeypatch.setattr(nvml, "nvmlDeviceGetUtilizationRates", explode)

    stats = device_stats(torch.device("cuda:0"))
    assert stats is not None
    assert stats.utilization is None
    assert stats.memory_allocated_bytes == 1 << 30
