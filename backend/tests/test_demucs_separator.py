"""Tests for the four-stem separator's plumbing, against a synthetic checkpoint.

Everything here runs with no GPU, no network and no model download: the network
under test is the ~22 000-parameter stand-in from :mod:`tests.demucs_fixtures`,
whose audio is meaningless and whose *behaviour under the ``Separator``
contract* is identical to the real one's. What the real weights add is quality,
not control flow.

Conventions follow the rest of the suite: WAV fixtures are generated, every wait
is gated by an :class:`asyncio.Event` or a :class:`threading.Event`, and nothing
sleeps for a duration as a way of synchronising.
"""

import asyncio
import hashlib
import io
import pickle
import sys
import threading
from array import array
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy
import pytest
import torch

from straticate.errors import ApplicationError
from straticate.inference.base import SeparationProgress
from straticate.inference.demucs import (
    DEMUCS_ARCHITECTURE,
    DemucsParameters,
    DemucsSeparator,
    NvmlProbe,
)
from straticate.inference.demucs import separator as separator_module
from straticate.inference.demucs.separator import (
    CHECKPOINT_PICKLE_GLOBALS,
    CheckpointArchitecture,
    RestrictedUnpickler,
    device_stats,
    load_checkpoint_package,
    pcm_to_tensor,
    reset_peak_memory,
    stem_source_indices,
    tensor_to_pcm,
    torch_pickle_globals,
)
from straticate.inference.pcm import PcmAudio, interleave
from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.schemas.jobs import JobState, SeparationConfiguration, SeparationResult
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav
from tests.demucs_fixtures import (
    TINY_CHUNK_SAMPLES,
    TINY_SAMPLE_RATE,
    TINY_SOURCES,
    TINY_STEMS,
    tiny_catalog_block,
    tiny_info,
    tiny_package,
    tiny_parameters,
    write_tiny_weights,
)

JOB_ID = "01JOB00000000000000000028"
MODEL_ID = "standard-stems-001"


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
    mode_id: str = "standard_stems", device_id: str | None = None
) -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id="balanced",
        device_id=device_id,
    )


@pytest.fixture
def weights(tmp_path: Path) -> Path:
    """A saved synthetic checkpoint, where the installer would have put one."""
    return write_tiny_weights(tmp_path / "weights" / "tiny-standard-001" / "weights.bin")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Two seconds of stereo tone — a handful of chunks at the tiny window."""
    return write_tone_wav(tmp_path / "source.wav", seconds=2.0, channels=2, sample_rate=22050)


def make_separator(weights: Path, **overrides: Any) -> DemucsSeparator:
    info = overrides.pop("info", tiny_info())
    parameters = overrides.pop("parameters", tiny_parameters(**overrides.pop("tuning", {})))
    return DemucsSeparator(info, weights_file=weights, parameters=parameters, **overrides)


async def run(
    separator: DemucsSeparator,
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

    assert [stem.name for stem in result.stems] == list(TINY_STEMS)
    assert sorted(path.name for path in output.iterdir()) == [
        "bass.wav",
        "drums.wav",
        "other.wav",
        "vocals.wav",
    ]
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
    assert result.model_id == "tiny-standard-001"
    assert result.metrics.processing_seconds > 0.0
    assert result.metrics.realtime_factor > 0.0
    # RTF is audio over processing, both measured in this run.
    assert result.metrics.realtime_factor == pytest.approx(
        2.0 / result.metrics.processing_seconds, rel=0.05
    )


async def test_the_four_stems_are_four_different_signals(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """Whatever the (random) network decides, no two stems are the same file.

    With real weights this is the acceptance criterion "genuinely separated";
    with synthetic weights it is the weaker but still load-bearing claim that
    four distinct masks were applied and none of them is the mixture handed
    back.
    """
    output = tmp_path / "stems"
    await run(make_separator(weights), source, output)

    digests = {name: digest(output / f"{name}.wav") for name in TINY_STEMS}
    assert len(set(digests.values())) == 4, f"stems are not distinct: {digests}"
    assert digest(source) not in set(digests.values())


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


async def test_a_source_shorter_than_one_window_still_separates(
    weights: Path, tmp_path: Path
) -> None:
    """One chunk, padded up to the training length and trimmed back."""
    short = write_tone_wav(tmp_path / "short.wav", seconds=0.2, channels=2, sample_rate=16000)
    progress = ProgressRecorder()
    output = tmp_path / "stems"
    result = await run(make_separator(weights), short, output, progress=progress)

    assert progress.reports[-1].chunks_total == 1
    for stem in result.stems:
        _, _, frames, _ = read_wav(output / f"{stem.name}.wav")
        assert frames == pytest.approx(0.2 * TINY_SAMPLE_RATE, rel=0.02)


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
    for chunk_size in (TINY_CHUNK_SAMPLES, TINY_CHUNK_SAMPLES // 2):
        progress = ProgressRecorder()
        separator = make_separator(weights, tuning={"chunk_size": chunk_size})
        await run(separator, source, tmp_path / f"stems-{chunk_size}", progress=progress)
        counts.append(progress.reports[-1].chunks_total)

    assert counts[1] > counts[0]


async def test_the_chunk_count_follows_the_configured_overlap(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """More overlap is more forward passes over the same audio, and says so."""
    counts: list[int] = []
    for overlap in (0.25, 0.75):
        progress = ProgressRecorder()
        separator = make_separator(weights, tuning={"overlap": overlap})
        await run(separator, source, tmp_path / f"stems-{overlap}", progress=progress)
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
    """Three stems are written before the fourth fails; none may survive."""
    output = tmp_path / "stems"
    original = separator_module.write_wav
    written = 0

    def exploding_write(path: Path, audio: PcmAudio) -> None:
        nonlocal written
        written += 1
        if written > 3:
            raise OSError("disk full")
        original(path, audio)

    monkeypatch.setattr(separator_module, "write_wav", exploding_write)

    with pytest.raises(OSError, match="disk full"):
        await run(make_separator(weights), source, output)

    assert written == 4, "the fourth stem must have been attempted"
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
        DemucsSeparator(
            tiny_info(),
            weights_file=tmp_path / "weights" / "tiny-standard-001" / "weights.bin",
            parameters=tiny_parameters(),
        )
    error = excinfo.value
    assert error.code == "model_weights_missing"
    assert error.status_code == 409
    assert error.detail == {"model_id": "tiny-standard-001"}


async def test_weights_that_do_not_match_the_architecture_are_rejected(tmp_path: Path) -> None:
    """``strict=True``: a partial load would produce plausible-sounding nonsense."""
    other = write_tiny_weights(tmp_path / "other.bin", channels=16)
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(tiny_info(), weights_file=other, parameters=tiny_parameters())
    error = excinfo.value
    assert error.code == "model_weights_invalid"
    assert error.status_code == 500
    assert error.detail is not None
    assert error.detail["model_id"] == "tiny-standard-001"


async def test_an_unreadable_weights_file_is_reported_not_raised_raw(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.bin"
    corrupt.write_bytes(b"this is not a checkpoint")
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(tiny_info(), weights_file=corrupt, parameters=tiny_parameters())
    assert excinfo.value.code == "model_weights_invalid"


async def test_a_file_that_is_not_a_demucs_package_is_rejected(tmp_path: Path) -> None:
    """A bare state dict is a readable pickle, and still not a checkpoint."""
    bare = tmp_path / "bare.bin"
    torch.save({"encoder.0.conv.weight": torch.zeros(1)}, bare)
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(tiny_info(), weights_file=bare, parameters=tiny_parameters())
    assert excinfo.value.code == "model_weights_invalid"
    assert excinfo.value.detail is not None
    assert excinfo.value.detail["reason"] == "not_a_demucs_package"


async def test_a_mode_it_does_not_serve_is_a_wiring_bug(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        await run(
            make_separator(weights),
            source,
            tmp_path / "stems",
            configuration=make_configuration(mode_id="vocals"),
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
    if torch.cuda.is_available():  # pragma: no cover - depends on the host
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


async def test_a_failed_device_move_does_not_wedge_the_cached_separator(
    weights: Path, source: Path, tmp_path: Path
) -> None:
    """A CUDA OOM part-way through ``.to()`` must not poison every later job.

    ``nn.Module.to`` moves parameters one at a time, so a failure leaves the
    network split across devices. Recording the intended device only *after* the
    move succeeds looks like the safe order and is not: a separator is cached per
    model for the life of the process, so the next job takes the "already there"
    early-out, skips the move, and fails forever. Forgetting where the network is
    *before* moving it is what makes the next run repair it.
    """
    separator = make_separator(weights)
    model: Any = separator._model  # pyright: ignore[reportPrivateUsage]
    moves: list[str] = []
    original = model.to

    def flaky_to(target: object) -> object:
        moves.append(str(target))
        if len(moves) == 1:
            raise RuntimeError("CUDA out of memory")
        return original(target)

    model.to = flaky_to

    # A job that resolves elsewhere fails part-way through the move, leaving the
    # network split. (Called directly so the test needs no CUDA device.)
    with pytest.raises(RuntimeError, match="out of memory"):
        separator._place_on_device(torch.device("cuda:0"))  # pyright: ignore[reportPrivateUsage]

    assert separator._loaded_device is None, (  # pyright: ignore[reportPrivateUsage]
        "after a partial move the separator must not claim to know where the network is"
    )

    # The next job resolves back to the device the network *used* to be on. It
    # must still move it -- half of it is somewhere else -- rather than taking
    # the "already there" early-out and failing on mismatched devices forever.
    result = await run(separator, source, tmp_path / "after")
    assert moves == ["cuda:0", "cpu"], "the repairing move was skipped"
    assert [stem.name for stem in result.stems] == list(TINY_STEMS)


def test_the_decode_rate_and_the_network_rate_must_agree(weights: Path) -> None:
    """Two catalog fields, two edits apart, that must never diverge.

    ``sample_rate`` is what FFmpeg resamples the source to;
    ``model.samplerate`` is what the network is built with and what sizes the
    training window. Change one and the network is fed audio at the wrong rate
    while its window is sized from the other — degraded output from a data edit,
    with nothing reporting anything.
    """
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(
            tiny_info(sample_rate=TINY_SAMPLE_RATE * 2),
            weights_file=weights,
            parameters=tiny_parameters(),
        )
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert "must agree" in error.message
    assert str(TINY_SAMPLE_RATE * 2) in error.message


def test_it_refuses_a_nonsense_ffmpeg_timeout(weights: Path) -> None:
    with pytest.raises(ValueError, match="ffmpeg_timeout_seconds"):
        make_separator(weights, ffmpeg_timeout_seconds=0.0)


# --------------------------------------------------------------------------
# A checkpoint is data, not code
# --------------------------------------------------------------------------


def test_the_architecture_reference_in_a_checkpoint_is_never_imported(weights: Path) -> None:
    """A real package pickles ``demucs.htdemucs.HTDemucs``; reading it must not import it.

    The fixture writes exactly that reference (see
    ``tests/demucs_fixtures.write_tiny_weights``). Reading the file has to
    resolve it *without* the module existing — which is also the only way this
    application can read an upstream checkpoint at all, because that module is
    not installed here.
    """
    assert "demucs.htdemucs" not in sys.modules

    package = load_checkpoint_package(weights, model_id="tiny-standard-001")

    assert "demucs.htdemucs" not in sys.modules, "reading a checkpoint imported upstream's module"
    assert package["klass"] is CheckpointArchitecture
    assert list(package["kwargs"]["sources"]) == TINY_SOURCES


def global_opcode_pickle(module: str, name: str, argument: str = "pwnd") -> bytes:
    """A pickle that resolves ``module.name`` and **calls** it with one string.

    Hand-assembled, because that is the whole point: a ``GLOBAL`` opcode is a
    pair of raw strings and is not bound by any object's real ``__module__``.
    ``pickle.dumps`` cannot produce ``c torch\nload\n`` — it would look up
    ``torch.load.__module__``, find ``torch.serialization`` and write that — so a
    reader that allowlists by *module* cannot be tested through it, which is how
    the hole below survived the first round of tests.
    """
    encoded = argument.encode()
    return (
        b"\x80\x04"  # PROTO 4
        + f"c{module}\n{name}\n".encode()  # GLOBAL module name
        + b"X"
        + len(encoded).to_bytes(4, "little")
        + encoded  # BINUNICODE argument
        + b"\x85"  # TUPLE1
        + b"R"  # REDUCE  -- i.e. call it
        + b"."  # STOP
    )


@pytest.mark.parametrize(
    ("module", "name"),
    [
        # The one code review found: this resolved, and was *called*. It failed
        # on its argument, not on the allowlist. ``torch.load`` on the
        # ``torch>=4`` this project allows defaults to ``weights_only=False``,
        # so a checkpoint could have had a second file of its choosing fully
        # unpickled.
        ("torch", "load"),
        ("torch", "save"),
        ("torch.serialization", "load"),
        ("os", "system"),
        ("builtins", "eval"),
        ("builtins", "exec"),
        ("subprocess", "Popen"),
    ],
)
def test_the_reader_refuses_a_hand_written_global_opcode(module: str, name: str) -> None:
    """No ``GLOBAL`` opcode may reach a callable, whatever module it claims.

    The refusal has to happen in ``find_class``, before ``REDUCE`` runs — so the
    assertion is on the exception *type*, not merely that something went wrong.
    Resolving ``torch.load`` and then failing inside it on a missing file, which
    is what the module-allowlisting version did, would satisfy a weaker test and
    is exactly the defect.
    """
    with pytest.raises(pickle.UnpicklingError, match="may not reference"):
        RestrictedUnpickler(io.BytesIO(global_opcode_pickle(module, name))).load()


def test_the_allowlist_is_an_enumeration_not_a_namespace() -> None:
    """The property that makes the test above hold, stated directly.

    ``torch._weights_only_unpickler`` enumerates individual names — every
    ``_rebuild_*`` helper, every storage and tensor type, the dtype singletons —
    and no entry point among them. Trusting the ``torch`` *module* instead, which
    is what the first version did, admits every one of them.
    """
    allowed = CHECKPOINT_PICKLE_GLOBALS | torch_pickle_globals()
    assert len(allowed) > 100, "torch's allowlist was not reached; the fallback is much smaller"
    assert "torch._utils._rebuild_tensor_v2" in allowed
    assert "collections.OrderedDict" in allowed
    assert "_codecs.encode" in allowed
    for entry_point in ("torch.load", "torch.save", "torch.compile", "torch.hub"):
        assert entry_point not in allowed


def test_a_checkpoint_may_not_name_an_arbitrary_callable(tmp_path: Path) -> None:
    """Feature 025 verifies a digest; a verified pickle is still a pickle.

    ``torch.load`` over an unrestricted pickle — which is what upstream's own
    loader does — executes whatever the file names. This one does not, and the
    refusal surfaces as ``model_weights_invalid`` rather than as anything
    happening.
    """
    for index, callable_ in enumerate((pickle.loads, torch.load)):
        dangerous = tmp_path / f"dangerous-{index}.bin"
        payload = tiny_package()
        payload["klass"] = callable_  # perfectly ordinary, perfectly awful choices
        torch.save(payload, dangerous)

        with pytest.raises(ApplicationError) as excinfo:
            load_checkpoint_package(dangerous, model_id="tiny-standard-001")
        assert excinfo.value.code == "model_weights_invalid"
        assert excinfo.value.detail is not None
        assert excinfo.value.detail["reason"] == "UnpicklingError"


def test_half_precision_weights_are_loaded_into_a_float32_network(weights: Path) -> None:
    """Upstream stores ``float16``; the network runs in ``float32``."""
    package = load_checkpoint_package(weights, model_id="tiny-standard-001")
    assert all(tensor.dtype == torch.float16 for tensor in package["state"].values())

    separator = make_separator(weights)
    module: Any = separator._model  # pyright: ignore[reportPrivateUsage]
    assert all(parameter.dtype == torch.float32 for parameter in module.parameters())
    assert not any(parameter.requires_grad for parameter in module.parameters())


# --------------------------------------------------------------------------
# Stems are matched by name, never by position
# --------------------------------------------------------------------------


def test_the_advertised_order_is_not_the_networks_order(weights: Path) -> None:
    """The premise of every test below: these two lists genuinely differ."""
    separator = make_separator(weights)
    assert list(separator.info.stems) != TINY_SOURCES
    assert sorted(separator.info.stems) == sorted(TINY_SOURCES)
    # vocals is the network's *last* output and the catalog's *first* stem.
    assert separator.stem_sources == (3, 0, 1, 2)


async def test_a_reordered_stem_list_maps_correctly_rather_than_silently_swapping(
    weights: Path, tmp_path: Path
) -> None:
    """A catalog that lists the stems in another order must not swap the audio.

    The manifest schema imposes no order on ``stems``, and adding a checkpoint
    is advertised as a pure data edit, so this ordering is a maintainer's free
    choice. Feature 026 found the positional version of this bug in review; here
    the orders differ *by default*, so a positional implementation would be
    wrong on the shipped entry rather than only on a hypothetical one.

    The check is content, not naming: the same audio is separated three times,
    under three different advertised orders, and each named file must come out
    byte-identical every time.
    """
    source = write_tone_wav(tmp_path / "source.wav", seconds=1.0, channels=2, sample_rate=16000)
    orders = (
        ("vocals", "drums", "bass", "other"),
        ("drums", "bass", "other", "vocals"),
        ("other", "vocals", "bass", "drums"),
    )
    digests: list[dict[str, str]] = []
    for index, stems in enumerate(orders):
        output = tmp_path / f"order-{index}"
        await run(
            DemucsSeparator(
                tiny_info(stems=stems), weights_file=weights, parameters=tiny_parameters()
            ),
            source,
            output,
        )
        digests.append({name: digest(output / f"{name}.wav") for name in stems})

    assert digests[0] == digests[1] == digests[2]
    assert len(set(digests[0].values())) == 4, "the four stems must be four different signals"


def test_a_stem_the_network_does_not_produce_is_refused(weights: Path) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(
            tiny_info(stems=("vocals", "drums", "bass", "piano")),
            weights_file=weights,
            parameters=tiny_parameters(),
        )
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert "not produced: ['piano']" in error.message


def test_a_source_the_catalog_does_not_advertise_is_refused(weights: Path) -> None:
    """Every network output has to end up in a file somebody asked for."""
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(
            tiny_info(stems=("vocals", "drums", "bass")),
            weights_file=weights,
            parameters=tiny_parameters(),
        )
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert "not advertised: ['other']" in error.message


def test_the_set_check_alone_would_let_a_transposition_through(weights: Path) -> None:
    """Why :func:`_check_sources` is mandatory, stated as a property of the other guard.

    ``stem_source_indices`` compares the advertised stems and the catalog's
    ``sources`` as *sets*, so a catalog that transposes two of the network's
    sources satisfies it — and would then map each stem name onto the wrong
    output index. Nothing downstream notices: the shapes, the counts and the
    strict ``load_state_dict`` are all still right.
    """
    transposed = tiny_parameters(model={"sources": ["bass", "drums", "other", "vocals"]})
    indices = stem_source_indices(tiny_info(), transposed)

    # It succeeds, and it is wrong: `vocals` is still index 3, but `drums` now
    # reads output 1, which the network fills with bass.
    assert indices == (3, 1, 0, 2)
    assert indices != (3, 0, 1, 2), "this is the mis-assignment the checkpoint check catches"

    # The separator refuses it, because the checkpoint disagrees.
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(tiny_info(), weights_file=weights, parameters=transposed)
    assert excinfo.value.code == "model_parameters_invalid"


def test_a_checkpoint_that_records_no_sources_is_refused(tmp_path: Path) -> None:
    """An order that cannot be verified is not an order to trust.

    The original version of ``_check_sources`` returned quietly here, which
    defeated the whole guard: a checkpoint saved without ``kwargs`` plus a
    transposed catalog entry would have written the drums into ``bass.wav``,
    passing every shape assertion and every other test in this file.
    """
    unusable: tuple[dict[str, Any] | None, ...] = (
        None,
        {},
        {"sources": "vocals"},
        {"sources": []},
    )
    for index, kwargs in enumerate(unusable):
        path = tmp_path / f"no-sources-{index}.bin"
        package = tiny_package()
        if kwargs is None:
            del package["kwargs"]
        else:
            package["kwargs"] = kwargs
        torch.save(package, path)

        with pytest.raises(ApplicationError) as excinfo:
            DemucsSeparator(tiny_info(), weights_file=path, parameters=tiny_parameters())
        assert excinfo.value.code == "model_weights_invalid", kwargs
        assert excinfo.value.detail is not None
        assert excinfo.value.detail["reason"] == "no_recorded_sources"


def test_sources_recorded_as_a_numpy_array_are_still_checked(tmp_path: Path) -> None:
    """A ``numpy.ndarray`` is not a ``Sequence``, and must not read as "absent".

    Testing the recorded value with ``isinstance(..., Sequence)`` — the original
    version — silently skipped the check for one of the container types a torch
    checkpoint plausibly carries.
    """
    matching = tmp_path / "numpy-sources.bin"
    package = tiny_package()
    package["kwargs"] = dict(package["kwargs"], sources=numpy.array(TINY_SOURCES))
    torch.save(package, matching)
    # It matches, so it loads — the array was read, not ignored.
    assert DemucsSeparator(
        tiny_info(), weights_file=matching, parameters=tiny_parameters()
    ).stem_sources == (3, 0, 1, 2)

    swapped = tmp_path / "numpy-sources-swapped.bin"
    package = tiny_package()
    package["kwargs"] = dict(
        package["kwargs"], sources=numpy.array(["bass", "drums", "other", "vocals"])
    )
    torch.save(package, swapped)
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(tiny_info(), weights_file=swapped, parameters=tiny_parameters())
    assert excinfo.value.code == "model_parameters_invalid"
    assert "installed checkpoint was trained to emit" in excinfo.value.message


def test_a_catalog_that_transposes_the_networks_sources_is_refused(tmp_path: Path) -> None:
    """The checkpoint is authoritative about what it emits, and it is consulted.

    This is the edit that would otherwise swap two stems' *audio* while every
    assertion about names, counts and shapes still passed: the catalog says the
    network emits ``bass`` before ``drums``, the checkpoint was trained the
    other way round, and both lists contain the same four names.
    """
    weights = write_tiny_weights(tmp_path / "weights.bin")
    transposed = ["bass", "drums", "other", "vocals"]
    with pytest.raises(ApplicationError) as excinfo:
        DemucsSeparator(
            tiny_info(),
            weights_file=weights,
            parameters=tiny_parameters(model={"sources": transposed}),
        )
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert "installed checkpoint was trained to emit" in error.message


# --------------------------------------------------------------------------
# Catalog parameters
# --------------------------------------------------------------------------


def test_parameters_come_from_the_catalog_block() -> None:
    parameters = DemucsParameters.from_catalog(tiny_catalog_block(), model_id="tiny-standard-001")
    assert parameters.sources == tuple(TINY_SOURCES)
    assert parameters.audio_channels == 2
    assert parameters.sample_rate == TINY_SAMPLE_RATE
    assert parameters.overlap == 0.25
    assert parameters.transition_power == 1.0


def test_a_rational_segment_survives_json() -> None:
    """``[39, 5]`` reaches the architecture as a rational, not as a float.

    ``HTDemucs`` computes its training length as ``int(segment * samplerate)``,
    which is exact for a ``Fraction`` and approximate for a float — and ``int``
    truncates, so a float segment can come out one sample short of the length
    the network was trained at. It happens not to for 39/5; it does for its
    neighbours at the same rate, which is the point.
    """
    exact = DemucsParameters.from_catalog(
        {"model": {"sources": TINY_SOURCES, "samplerate": 44100, "segment": [39, 5]}},
        model_id="m",
    )
    assert exact.model["segment"] == Fraction(39, 5)
    assert exact.training_samples == 343980

    # A plain number is still accepted, for a checkpoint whose segment is one.
    plain = DemucsParameters.from_catalog(
        {"model": {"sources": TINY_SOURCES, "samplerate": 44100, "segment": 10}},
        model_id="m",
    )
    assert plain.training_samples == 441000


@pytest.mark.parametrize(("numerator", "denominator"), [(7, 5), (41, 5), (46, 5)])
def test_the_float_spelling_of_a_segment_really_can_lose_a_sample(
    numerator: int, denominator: int
) -> None:
    """Evidence for the rule above, rather than an assertion that it is prudent.

    Each of these is a fifths-of-a-second segment at 44.1 kHz — the same family
    ``htdemucs``'s own 39/5 belongs to — where the exact product is an integer
    and the float product falls just below it.
    """
    rational = DemucsParameters.from_catalog(
        {
            "model": {
                "sources": TINY_SOURCES,
                "samplerate": 44100,
                "segment": [numerator, denominator],
            }
        },
        model_id="m",
    )
    floating = DemucsParameters.from_catalog(
        {
            "model": {
                "sources": TINY_SOURCES,
                "samplerate": 44100,
                "segment": numerator / denominator,
            }
        },
        model_id="m",
    )
    assert rational.training_samples == floating.training_samples + 1


def test_the_window_defaults_to_the_training_segment() -> None:
    parameters = DemucsParameters.from_catalog(tiny_catalog_block(), model_id="m")
    assert parameters.chunk_samples is None
    assert parameters.window_samples == parameters.training_samples == TINY_CHUNK_SAMPLES
    assert parameters.stride_samples == 3000


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        (None, "no default_inference_parameters block"),
        ({}, "no default_inference_parameters block"),
        ({"model": []}, "must be an object"),
        ({"model": {"sources": TINY_SOURCES, "nfft": 64, "nftt": 64}}, "unknown architecture"),
        ({"model": {}}, "sources must be a list"),
        ({"model": {"sources": "vocals"}}, "sources must be a list"),
        ({"model": {"sources": ["a", "a"]}}, "duplicate name"),
        ({"model": {"sources": ["a"]}, "inference": {"chunk_size": 0}}, "positive integer"),
        ({"model": {"sources": ["a"]}, "inference": {"overlap": 1.0}}, "overlap must be"),
        ({"model": {"sources": ["a"]}, "inference": {"overlap": -0.1}}, "overlap must be"),
        (
            {"model": {"sources": ["a"]}, "inference": {"transition_power": 0.5}},
            "transition_power must be",
        ),
        ({"model": {"sources": ["a"], "segment": "long"}}, "segment must be a number"),
        ({"model": {"sources": ["a"], "segment": [1, 0]}}, "segment must be a number"),
    ],
)
def test_an_unusable_catalog_entry_fails_loudly(block: Any, fragment: str) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        DemucsParameters.from_catalog(block, model_id="broken-001")
    error = excinfo.value
    assert error.code == "model_parameters_invalid"
    assert error.status_code == 500
    assert fragment in error.message


def test_a_window_longer_than_the_training_segment_is_refused() -> None:
    """``use_train_segment`` means longer windows run off the training distribution."""
    with pytest.raises(ApplicationError) as excinfo:
        DemucsParameters.from_catalog(
            tiny_catalog_block(chunk_size=TINY_CHUNK_SAMPLES * 2), model_id="m"
        )
    assert "longer than" in excinfo.value.message


def test_the_repository_catalog_entry_is_runnable_as_written() -> None:
    """The shipped entry must satisfy every rule this module enforces.

    It cannot be *run* without the weights, but everything decided before the
    tensors are read — the hyperparameters, the source list, the window and the
    stem mapping — is checkable in normal CI, and is exactly what a hand edit to
    ``models/catalog.json`` would get wrong.
    """
    from straticate.config import Settings
    from straticate.models import ModelCatalog

    catalog = ModelCatalog.from_directory(Settings().models_dir)
    model = catalog.get_model(MODEL_ID)
    parameters = DemucsParameters.from_catalog(
        catalog.inference_parameters(MODEL_ID), model_id=MODEL_ID
    )

    assert model.architecture == DEMUCS_ARCHITECTURE
    assert parameters.sources == ("drums", "bass", "other", "vocals")
    assert sorted(parameters.sources) == sorted(model.stems)
    assert parameters.sample_rate == model.sample_rate == 44100
    # The pinned window is the training segment exactly, to the sample.
    assert parameters.training_samples == 343980
    assert parameters.window_samples == 343980
    assert parameters.stride_samples == 257985

    from straticate.inference.registry import separator_info_from_model

    indices = stem_source_indices(separator_info_from_model(model), parameters)
    assert indices == (3, 0, 1, 2)


def test_the_separator_exposes_what_it_was_configured_with(weights: Path) -> None:
    separator = make_separator(weights, ffmpeg_timeout_seconds=12.5)
    assert separator.ffmpeg_timeout_seconds == 12.5
    assert separator.parameters.window_samples == TINY_CHUNK_SAMPLES
    assert separator.info.architecture == "htdemucs"


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
