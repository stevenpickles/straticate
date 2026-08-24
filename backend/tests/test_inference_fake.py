"""Tests for the ``Separator`` seam and the ``FakeSeparator`` implementation.

Fixtures are tiny WAV files generated at test time (see
``tests/audio_fixtures.py``); no audio binaries are committed. Every separator
in these tests uses ``chunk_delay_seconds=0.0`` / ``model_load_seconds=0.0`` so
the suite stays fast, and cancellation is driven from the progress callback
rather than from a timer, so nothing depends on wall-clock timing.
"""

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from straticate.audio import ffmpeg as ffmpeg_module
from straticate.audio.ffmpeg import FFmpegTimeout
from straticate.errors import ApplicationError
from straticate.inference import (
    FAKE_ARCHITECTURE,
    FAKE_SEPARATOR_INFOS,
    FAKE_STANDARD_INFO,
    FAKE_VOCALS_INFO,
    FakeDeviceProfile,
    FakeSeparator,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    default_separator_builders,
    fake_separator_info,
    fake_separator_info_for_mode,
    job_output_dir,
    job_stems_dir,
    stem_path,
)
from straticate.inference import pcm as pcm_module
from straticate.inference.pcm import AudioDecodeError, decode_to_pcm
from straticate.jobs import CancellationToken, JobCancelled
from straticate.schemas import Model
from straticate.schemas.jobs import JobState, SeparationConfiguration
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav

JOB_ID = "01JOB000000000000000000000"
CATALOG_PATH = Path(__file__).resolve().parents[2] / "models" / "catalog.json"


def make_separator(info: SeparatorInfo = FAKE_VOCALS_INFO, **kwargs: Any) -> FakeSeparator:
    """A fake separator with every simulated delay switched off."""
    options: dict[str, Any] = {"chunk_delay_seconds": 0.0, "model_load_seconds": 0.0}
    options.update(kwargs)
    return FakeSeparator(info, **options)


def make_configuration(mode_id: str = "vocals") -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id="high_quality",
        device_id=None,
    )


class ProgressRecorder:
    """Collects every progress report a separator emits."""

    def __init__(self) -> None:
        self.reports: list[SeparationProgress] = []

    def __call__(self, progress: SeparationProgress) -> None:
        self.reports.append(progress)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the seam ---------------------------------------------------------------


def test_fake_separator_satisfies_the_separator_protocol() -> None:
    # A structural check: pyright fails this assignment if FakeSeparator drifts
    # from the protocol, and the attributes are exercised at runtime too.
    separator: Separator = make_separator()
    assert separator.info is FAKE_VOCALS_INFO
    assert separator.runtime_stats() is None


def test_separator_info_projects_onto_the_contract_model_info() -> None:
    model = FAKE_STANDARD_INFO.to_model_info()
    assert model.id == "fake-standard-001"
    assert model.architecture == "fake"
    assert model.separation_mode == "standard_stems"
    assert model.stem_count == 4


@pytest.mark.parametrize(
    "stems",
    [("vocals",), ("vocals", "vocals"), ("vocals", "Bad Name"), ("vocals", "../escape")],
)
def test_separator_info_rejects_invalid_stem_lists(stems: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        SeparatorInfo(
            model_id="x",
            display_name="X",
            architecture="fake",
            version="1.0",
            separation_mode="vocals",
            stems=stems,
            sample_rate=44100,
        )


def _catalog_model() -> Model:
    """The first ``fake``-architecture entry of the repository catalog."""
    catalog: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entry = next(item for item in catalog["models"] if item["architecture"] == "fake")
    return Model.model_validate(entry)


def test_fake_infos_match_the_model_catalog() -> None:
    catalog: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = {
        entry["id"]: entry for entry in catalog["models"] if entry["architecture"] == "fake"
    }
    assert set(entries) == set(FAKE_SEPARATOR_INFOS)
    for model_id, info in FAKE_SEPARATOR_INFOS.items():
        entry = entries[model_id]
        assert info.display_name == entry["display_name"]
        assert info.version == entry["version"]
        assert info.separation_mode == entry["separation_mode"]
        assert list(info.stems) == entry["stems"]
        assert info.sample_rate == entry["sample_rate"]


def test_fake_info_lookup_helpers() -> None:
    assert fake_separator_info("fake-standard-001") is FAKE_STANDARD_INFO
    assert fake_separator_info_for_mode("vocals") is FAKE_VOCALS_INFO
    with pytest.raises(KeyError):
        fake_separator_info("nope-001")
    with pytest.raises(KeyError):
        fake_separator_info_for_mode("nope")


def test_layout_helpers_describe_the_documented_paths(tmp_path: Path) -> None:
    assert job_output_dir(tmp_path, JOB_ID) == tmp_path / "jobs" / JOB_ID
    assert job_stems_dir(tmp_path, JOB_ID) == tmp_path / "jobs" / JOB_ID / "stems"
    assert stem_path(tmp_path, JOB_ID, "vocals") == tmp_path / "jobs" / JOB_ID / "stems/vocals.wav"
    with pytest.raises(ValueError):
        stem_path(tmp_path, JOB_ID, "../../escape")


# -- stems ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "mode_id", "expected"),
    [
        (FAKE_VOCALS_INFO, "vocals", ["vocals", "instrumental"]),
        (FAKE_STANDARD_INFO, "standard_stems", ["vocals", "drums", "bass", "other"]),
    ],
)
async def test_produces_exactly_the_stems_of_the_requested_mode(
    tmp_path: Path, info: SeparatorInfo, mode_id: str, expected: list[str]
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    output = tmp_path / "stems"
    separator = make_separator(info, chunk_seconds=0.1)

    result = await separator.separate(
        source,
        make_configuration(mode_id),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=output,
    )

    assert [stem.name for stem in result.stems] == expected
    assert result.model_id == info.model_id
    assert result.job_id == JOB_ID
    assert sorted(path.name for path in output.iterdir()) == sorted(f"{s}.wav" for s in expected)

    for stem in result.stems:
        channels, sample_rate, frames, samples = read_wav(output / f"{stem.name}.wav")
        assert channels == 2
        assert sample_rate == info.sample_rate
        assert frames == pytest.approx(0.5 * info.sample_rate, abs=64)
        assert peak_amplitude(samples) > 1000, "placeholder stems must be audible"
        assert stem.channels == channels
        assert stem.sample_rate_hz == sample_rate
        assert stem.duration_seconds == pytest.approx(0.5, abs=0.01)


async def test_stems_are_audibly_distinct_from_each_other_and_the_source(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.3)
    output = tmp_path / "stems"
    separator = make_separator(FAKE_STANDARD_INFO, chunk_seconds=0.1)

    await separator.separate(
        source,
        make_configuration("standard_stems"),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=output,
    )

    digests = {digest(output / f"{name}.wav") for name in FAKE_STANDARD_INFO.stems}
    assert len(digests) == FAKE_STANDARD_INFO.stem_count
    assert digest(source) not in digests


async def test_mono_source_is_resampled_to_the_model_rate(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "mono.wav", seconds=0.4, channels=1, sample_rate=22050)
    output = tmp_path / "stems"
    separator = make_separator(chunk_seconds=0.1)

    result = await separator.separate(
        source,
        make_configuration(),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=output,
    )

    for stem in result.stems:
        channels, sample_rate, _, samples = read_wav(output / f"{stem.name}.wav")
        assert channels == 1
        assert sample_rate == FAKE_VOCALS_INFO.sample_rate
        assert peak_amplitude(samples) > 1000
        assert stem.duration_seconds == pytest.approx(0.4, abs=0.02)


# -- progress ---------------------------------------------------------------


async def test_progress_is_chunk_grained_monotonic_and_ends_complete(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    recorder = ProgressRecorder()
    separator = make_separator(chunk_seconds=0.1)

    await separator.separate(
        source,
        make_configuration(),
        recorder,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    reports = recorder.reports
    chunks_total = reports[0].chunks_total
    assert chunks_total == 5  # 0.5 s of audio in 0.1 s chunks
    assert [report.chunks_completed for report in reports] == list(range(chunks_total + 1))
    assert all(report.chunks_total == chunks_total for report in reports)
    assert reports[0].fraction == 0.0
    assert reports[-1].fraction == 1.0
    assert reports[-1].chunks_completed == chunks_total

    processed = [report.audio_processed_seconds for report in reports]
    assert processed == sorted(processed)
    assert processed[-1] == pytest.approx(reports[-1].audio_total_seconds, abs=1e-6)
    assert reports[-1].audio_total_seconds == pytest.approx(0.5, abs=0.01)


async def test_chunk_count_follows_the_configured_chunk_length(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    recorder = ProgressRecorder()
    separator = make_separator(chunk_seconds=0.25)

    await separator.separate(
        source,
        make_configuration(),
        recorder,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    assert recorder.reports[-1].chunks_total == 2


def test_progress_fraction_is_zero_when_the_chunk_count_is_unknown() -> None:
    unknown = SeparationProgress(
        chunks_completed=0, chunks_total=0, audio_processed_seconds=0.0, audio_total_seconds=0.0
    )
    assert unknown.fraction == 0.0


# -- determinism ------------------------------------------------------------


async def test_two_identical_runs_produce_byte_identical_stems(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.4)
    digests: list[dict[str, str]] = []
    counts: list[int] = []

    for run in range(2):
        output = tmp_path / f"run{run}"
        recorder = ProgressRecorder()
        separator = make_separator(chunk_seconds=0.1)
        await separator.separate(
            source,
            make_configuration(),
            recorder,
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=output,
        )
        counts.append(recorder.reports[-1].chunks_total)
        digests.append({name: digest(output / f"{name}.wav") for name in FAKE_VOCALS_INFO.stems})

    assert counts[0] == counts[1]
    assert digests[0] == digests[1]


async def test_output_is_independent_of_the_chunk_length(tmp_path: Path) -> None:
    # Filter state is carried across chunk boundaries, so chunking changes the
    # progress granularity only — never the audio.
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.4)
    digests: list[dict[str, str]] = []

    for index, chunk_seconds in enumerate((0.05, 0.31)):
        output = tmp_path / f"chunk{index}"
        separator = make_separator(chunk_seconds=chunk_seconds)
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=output,
        )
        digests.append({name: digest(output / f"{name}.wav") for name in FAKE_VOCALS_INFO.stems})

    assert digests[0] == digests[1]


# -- cancellation -----------------------------------------------------------


async def test_cancellation_mid_run_raises_and_leaves_no_outputs(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    output = tmp_path / "stems"
    token = CancellationToken()
    recorder = ProgressRecorder()

    def cancel_after_first_chunk(progress: SeparationProgress) -> None:
        recorder(progress)
        if progress.chunks_completed == 1:
            token.cancel()

    separator = make_separator(chunk_seconds=0.1)
    with pytest.raises(JobCancelled):
        await separator.separate(
            source,
            make_configuration(),
            cancel_after_first_chunk,
            token,
            job_id=JOB_ID,
            output_dir=output,
        )

    # Cancellation is observed at the very next chunk boundary, not at the end.
    assert recorder.reports[-1].chunks_completed == 1
    assert recorder.reports[-1].chunks_total == 5
    assert not output.exists() or list(output.iterdir()) == []


async def test_cancellation_removes_stale_and_partial_stem_files(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    output = tmp_path / "stems"
    output.mkdir(parents=True)
    (output / "vocals.wav").write_bytes(b"stale")
    (output / "instrumental.wav.part").write_bytes(b"half written")
    token = CancellationToken()

    def cancel_after_first_chunk(progress: SeparationProgress) -> None:
        if progress.chunks_completed == 1:
            token.cancel()

    separator = make_separator(chunk_seconds=0.1)
    with pytest.raises(JobCancelled):
        await separator.separate(
            source,
            make_configuration(),
            cancel_after_first_chunk,
            token,
            job_id=JOB_ID,
            output_dir=output,
        )

    assert list(output.iterdir()) == [], "no stem may survive a cancelled separation"


async def test_cancellation_before_the_first_chunk_is_observed(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    token = CancellationToken()
    token.cancel()
    separator = make_separator()

    with pytest.raises(JobCancelled):
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            token,
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )


# -- metrics and runtime statistics -----------------------------------------


async def test_result_metrics_report_processing_time_and_realtime_factor(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    separator = make_separator(chunk_seconds=0.1)

    result = await separator.separate(
        source,
        make_configuration(),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    metrics = result.metrics
    assert metrics.processing_seconds > 0.0
    assert metrics.realtime_factor > 0.0
    # RTF is audio duration / processing duration, by definition.
    assert metrics.realtime_factor == pytest.approx(0.5 / metrics.processing_seconds, rel=0.05)


async def test_runtime_stats_expose_fake_model_and_device_numbers(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    separator = make_separator(FAKE_STANDARD_INFO, chunk_seconds=0.1)
    seen: list[int] = []

    def sample(progress: SeparationProgress) -> None:
        stats = separator.runtime_stats()
        assert stats is not None
        assert stats.job_id == JOB_ID
        assert stats.processing.stage is JobState.SEPARATING
        seen.append(0 if stats.device is None else stats.device.memory_peak_bytes)

    await separator.separate(
        source,
        make_configuration("standard_stems"),
        sample,
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    assert seen == sorted(seen), "peak memory never decreases"

    stats = separator.runtime_stats()
    assert stats is not None
    assert stats.model is FAKE_STANDARD_INFO
    assert stats.processing.chunks_completed == stats.processing.chunks_total == 5
    assert stats.processing.elapsed_seconds > 0.0
    assert stats.processing.realtime_factor > 0.0
    assert stats.processing.last_chunk_seconds is not None
    assert stats.processing.mean_chunk_seconds is not None

    device = stats.device
    assert device is not None
    assert device.backend == "fake"
    assert 0 < device.memory_allocated_bytes <= device.memory_peak_bytes
    assert device.memory_peak_bytes < device.memory_total_bytes
    assert device.utilization is not None and 0.0 <= device.utilization <= 1.0
    assert device.temperature_celsius is not None

    # Feature 019 builds its runtime_metrics event out of these projections.
    assert stats.model.to_model_info().stem_count == 4
    assert device.to_gpu_metrics().device_id == "fake:0"
    assert stats.processing.to_processing_metrics().stage is JobState.ENCODING


async def test_runtime_stats_omit_the_device_block_when_running_on_cpu(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    separator = make_separator(device=None)

    await separator.separate(
        source,
        make_configuration(),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    stats = separator.runtime_stats()
    assert stats is not None
    assert stats.device is None


async def test_device_profile_is_configurable(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    profile = FakeDeviceProfile(
        device_id="cuda:0", name="Pretend RTX", backend="cuda", memory_total_bytes=34359738368
    )
    separator = make_separator(device=profile)

    await separator.separate(
        source,
        make_configuration(),
        ProgressRecorder(),
        CancellationToken(),
        job_id=JOB_ID,
        output_dir=tmp_path / "stems",
    )

    stats = separator.runtime_stats()
    assert stats is not None
    assert stats.device is not None
    assert stats.device.name == "Pretend RTX"
    assert stats.device.memory_total_bytes == 34359738368


# -- failure modes ----------------------------------------------------------


async def test_mode_mismatch_is_an_application_error(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    separator = make_separator(FAKE_VOCALS_INFO)

    with pytest.raises(ApplicationError) as excinfo:
        await separator.separate(
            source,
            make_configuration("standard_stems"),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )
    assert excinfo.value.code == "separation_mode_mismatch"
    assert excinfo.value.status_code == 400


async def test_undecodable_input_is_an_application_error(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"this is not audio at all\n")
    separator = make_separator()

    with pytest.raises(ApplicationError) as excinfo:
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )
    assert excinfo.value.code == "audio_decode_failed"
    assert not (tmp_path / "stems").exists()


async def test_a_decode_timeout_is_its_own_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged FFmpeg fails the job with its own code, and never hangs.

    The runner is stubbed, so the expiry path runs instantly — the test asserts
    the classification, not that waiting works. ``audio_decode_failed`` would
    tell the user their file is undecodable, which FFmpeg never determined.
    """
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    separator = make_separator()

    def wedged(command: Sequence[str], **_kwargs: object) -> NoReturn:
        raise FFmpegTimeout(command[0], 600.0)

    monkeypatch.setattr(pcm_module, "run_ffmpeg", wedged)

    with pytest.raises(ApplicationError) as excinfo:
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )
    assert excinfo.value.code == "audio_decode_timed_out"
    assert excinfo.value.status_code == 504
    assert excinfo.value.detail == {"timeout_seconds": 600.0}
    assert not (tmp_path / "stems").exists()


async def test_the_separators_configured_bound_governs_its_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound reaches FFmpeg from construction, not from a global read.

    ``create_app`` builds the registry with ``Settings.ffmpeg_timeout_seconds``
    (see ``default_separator_builders``), so an application built with explicit
    settings really governs its decode subprocesses. The real
    ``subprocess.run`` is stubbed and records what it was handed; nothing here
    waits.
    """
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    builder = default_separator_builders(ffmpeg_timeout_seconds=1.5)[FAKE_ARCHITECTURE]
    separator = builder(_catalog_model())
    recorded: list[float] = []

    def record_and_wedge(command: Sequence[str], **kwargs: Any) -> NoReturn:
        timeout = cast(float, kwargs["timeout"])
        recorded.append(timeout)
        raise subprocess.TimeoutExpired(list(command), timeout)

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", record_and_wedge)

    with pytest.raises(ApplicationError) as excinfo:
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )

    assert excinfo.value.code == "audio_decode_timed_out"
    assert excinfo.value.detail == {"timeout_seconds": 1.5}
    assert recorded == [1.5], "ffprobe must be bounded by the configured value"


async def test_a_separator_runs_one_separation_at_a_time(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.3)
    separator = make_separator(chunk_seconds=0.1)

    first = asyncio.create_task(
        separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "stems",
        )
    )
    # One loop tick is enough for the task to enter separate() and block on the
    # decode thread — no timing assumption beyond "a scheduled task starts".
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="one separation at a time"):
        await separator.separate(
            source,
            make_configuration(),
            ProgressRecorder(),
            CancellationToken(),
            job_id=JOB_ID,
            output_dir=tmp_path / "other",
        )

    result = await first
    assert len(result.stems) == 2
    assert not (tmp_path / "other").exists()


# -- PCM plumbing -----------------------------------------------------------


async def test_decode_to_pcm_rejects_a_non_audio_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"nope")
    with pytest.raises(AudioDecodeError):
        await decode_to_pcm(path, sample_rate=44100, timeout_seconds=30.0)


async def test_decode_to_pcm_downmixes_wide_layouts(tmp_path: Path) -> None:
    source = write_tone_wav(tmp_path / "quad.wav", seconds=0.2, channels=4)
    audio = await decode_to_pcm(source, sample_rate=44100, timeout_seconds=30.0)
    assert audio.channel_count == 2
    assert audio.duration_seconds == pytest.approx(0.2, abs=0.01)
