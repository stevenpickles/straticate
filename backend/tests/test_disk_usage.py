"""Tests for disk-usage classification and ``GET /api/v1/system/disk-usage``.

Two tiers, like ``test_storage.py``/``test_system.py`` split for feature 040:

- Unit tests drive :func:`straticate.system.disk_usage.disk_usage_report`
  directly against a hand-built ``tmp_path`` tree — no application, no fake
  separator, just files this test writes and deletes itself.
- HTTP tests run the **real** application (the pattern
  ``test_api_export.py`` documents: a real job manager on the test's own
  event loop, a real ``FakeSeparator`` writing real WAV stems, a real export
  build) so uploads, job stems and export artifacts are the genuine article,
  not a stand-in for one.

Every seeded scenario is checked against an **independent** ``os.walk``
ground truth computed in the test (:func:`independent_ground_truth`) rather
than by re-invoking the module under test — a bug in the implementation's own
arithmetic would otherwise pass against itself.
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.config import Settings
from straticate.inference import (
    FAKE_ARCHITECTURE,
    SeparationProgress,
    SeparatorInfo,
    SeparatorRegistry,
    fake_separator_builder,
    separator_info_from_model,
)
from straticate.jobs import CancellationToken, JobEvent, JobManager
from straticate.main import create_app
from straticate.schemas import AudioFile, AudioMetadata, ComputeDevice
from straticate.schemas.events import JobCompletedEvent
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)
from straticate.system import CUDA_BACKEND, DeviceDetector, DiskUsageLike
from straticate.system.disk_usage import disk_usage_report
from tests.audio_fixtures import write_tone_wav
from tests.conftest import fake_quality_id

JOBS_URL = "/api/v1/jobs"
DISK_USAGE_URL = "/api/v1/system/disk-usage"
WAIT_TIMEOUT = 30.0

FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)

_MODULE_LOGGER = "straticate.system.disk_usage"


# ============================================================================
# Unit tests: the pure walker
# ============================================================================


@dataclass(frozen=True)
class _FixedUsage:
    """A ``shutil.disk_usage`` reading, as far as the report cares."""

    total: int
    free: int


def _reader(total: int, free: int) -> Callable[[Path], DiskUsageLike]:
    def read(path: Path) -> DiskUsageLike:
        return _FixedUsage(total=total, free=free)

    return read


def _write(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_an_empty_data_dir_reports_all_zero_buckets(tmp_path: Path) -> None:
    report = disk_usage_report(
        tmp_path, audio_ids=[], job_ids=[], read_usage=_reader(total=1000, free=400)
    )

    assert report.uploads.count == 0
    assert report.uploads.bytes == 0
    assert report.job_stems.count == 0
    assert report.job_exports.count == 0
    assert report.orphans.count == 0
    assert report.free_bytes == 400
    assert report.total_bytes == 1000


def test_a_missing_data_dir_reports_zero_buckets_and_real_ancestor_totals(tmp_path: Path) -> None:
    """Nothing has ever been uploaded or separated yet: `data_dir` itself may not exist."""
    missing = tmp_path / "data"

    report = disk_usage_report(
        missing, audio_ids=[], job_ids=[], read_usage=_reader(total=2000, free=1500)
    )

    assert report.uploads == report.job_stems == report.job_exports == report.orphans
    assert report.uploads.count == 0
    assert report.free_bytes == 1500
    assert report.total_bytes == 2000


def test_a_registered_upload_is_counted_in_uploads(tmp_path: Path) -> None:
    _write(tmp_path / "audio" / "aud1" / "original.wav", b"0123456789")
    _write(tmp_path / "audio" / "aud1" / "audio.json", b"{}")

    report = disk_usage_report(tmp_path, audio_ids=["aud1"], job_ids=[])

    assert report.uploads.count == 2
    assert report.uploads.bytes == 12
    assert report.orphans.count == 0


def test_an_unregistered_upload_directory_is_an_orphan(tmp_path: Path) -> None:
    _write(tmp_path / "audio" / "ghost" / "original.wav", b"stale")

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=[])

    assert report.uploads.count == 0
    assert report.orphans.count == 1
    assert report.orphans.bytes == 5


def test_a_stray_tmp_sidecar_inside_a_live_upload_is_an_orphan(tmp_path: Path) -> None:
    """An interrupted sidecar write (`straticate.audio.storage`) never got renamed."""
    _write(tmp_path / "audio" / "aud1" / "original.wav", b"0123456789")
    _write(tmp_path / "audio" / "aud1" / "audio.json", b"{}")
    _write(tmp_path / "audio" / "aud1" / "audio.json.deadbeef.tmp", b"half-written")

    report = disk_usage_report(tmp_path, audio_ids=["aud1"], job_ids=[])

    assert report.uploads.count == 2
    assert report.uploads.bytes == 12
    assert report.orphans.count == 1
    assert report.orphans.bytes == len(b"half-written")


def test_a_live_jobs_own_files_are_job_stems_and_its_exports_are_separate(tmp_path: Path) -> None:
    _write(tmp_path / "jobs" / "job1" / "job.json", b"{...}")
    _write(tmp_path / "jobs" / "job1" / "stems" / "vocals.wav", b"0123456789")
    _write(tmp_path / "jobs" / "job1" / "exports" / "wav_pcm24-vocals.wav", b"01234567890123456789")

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=["job1"])

    assert report.job_stems.count == 2
    assert report.job_stems.bytes == len(b"{...}") + 10
    assert report.job_exports.count == 1
    assert report.job_exports.bytes == 20
    assert report.orphans.count == 0


def test_an_unregistered_job_directory_is_wholly_an_orphan(tmp_path: Path) -> None:
    _write(tmp_path / "jobs" / "ghost" / "job.json", b"{}")
    _write(tmp_path / "jobs" / "ghost" / "stems" / "vocals.wav", b"stale-stem")
    _write(tmp_path / "jobs" / "ghost" / "exports" / "a.wav", b"stale-export")

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=[])

    assert report.job_stems.count == 0
    assert report.job_exports.count == 0
    assert report.orphans.count == 3


def test_a_stray_part_file_in_a_live_jobs_exports_is_an_orphan(tmp_path: Path) -> None:
    """An interrupted export build (`straticate.api.export`) never got published."""
    _write(tmp_path / "jobs" / "job1" / "job.json", b"{}")
    _write(tmp_path / "jobs" / "job1" / "exports" / "wav_pcm24-vocals.wav", b"published")
    _write(
        tmp_path / "jobs" / "job1" / "exports" / "wav_pcm24-vocals.wav.abc123.part", b"unfinished"
    )

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=["job1"])

    assert report.job_exports.count == 1
    assert report.job_exports.bytes == len(b"published")
    assert report.orphans.count == 1
    assert report.orphans.bytes == len(b"unfinished")


def test_a_build_staging_directory_left_behind_is_orphan_debris(tmp_path: Path) -> None:
    """A crashed archive build's `.build-*` `TemporaryDirectory` never got cleaned up."""
    _write(tmp_path / "jobs" / "job1" / "job.json", b"{}")
    _write(tmp_path / "jobs" / "job1" / "exports" / ".build-xyz" / "vocals.wav", b"half-encoded")

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=["job1"])

    assert report.job_exports.count == 0
    assert report.orphans.count == 1
    assert report.orphans.bytes == len(b"half-encoded")


def test_a_job_with_no_stems_or_exports_yet_still_counts_its_record(tmp_path: Path) -> None:
    """A queued/running job is live from submission (feature 057); never an orphan."""
    _write(tmp_path / "jobs" / "job1" / "job.json", b"{...}")

    report = disk_usage_report(tmp_path, audio_ids=[], job_ids=["job1"])

    assert report.job_stems.count == 1
    assert report.job_stems.bytes == len(b"{...}")
    assert report.orphans.count == 0


def test_an_unlistable_subtree_degrades_to_zero_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A permissions failure walking `audio/` must not fail the whole report."""
    (tmp_path / "audio").mkdir()
    caplog.set_level(logging.WARNING, logger=_MODULE_LOGGER)

    def failing_walk(top: Any, onerror: Any = None, **kwargs: Any) -> Any:
        if onerror is not None:
            onerror(PermissionError(13, "Permission denied"))
        return iter(())

    monkeypatch.setattr(os, "walk", failing_walk)

    report = disk_usage_report(tmp_path, audio_ids=["aud1"], job_ids=[])

    assert report.uploads.count == 0
    assert report.orphans.count == 0
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1


# ============================================================================
# HTTP tests: the real application
# ============================================================================


class StaticProbe:
    """Probe reporting one fixed device, so device resolution is deterministic."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [FAKE_GPU]


def fast_registry() -> SeparatorRegistry:
    """The real fake separator with every simulated delay removed."""
    return SeparatorRegistry(
        {
            FAKE_ARCHITECTURE: fake_separator_builder(
                chunk_seconds=0.2,
                chunk_delay_seconds=0.0,
                model_load_seconds=0.0,
            )
        }
    )


def build_app(data_dir: Path, **overrides: Any) -> FastAPI:
    """An application isolated to ``data_dir``, with deterministic devices."""
    app = create_app(Settings(data_dir=data_dir, **overrides))
    app.state.device_detector = DeviceDetector(probes=[StaticProbe()])
    app.state.separator_registry = fast_registry()
    return app


def register_audio(app: FastAPI, *, seconds: float = 0.5) -> str:
    """Write a real tone WAV into the app's audio store and register it."""
    store = app.state.audio_store
    audio_id = cast(str, store.new_id())
    path = cast(Path, store.prepare_original_path(audio_id, "song.wav"))
    write_tone_wav(path, seconds=seconds, channels=2, sample_rate=44100)
    store.register(
        AudioFile(
            id=audio_id,
            filename="song.wav",
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.now(UTC),
            metadata=AudioMetadata(
                duration_seconds=seconds,
                container="wav",
                codec="pcm_s16le",
                channels=2,
                sample_rate_hz=44100,
                bit_depth=16,
                bit_rate_bps=1411000,
            ),
        )
    )
    return audio_id


class EventRecorder:
    """Sync manager listener that records events and lets tests await terminal state."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        self._changed.set()

    async def wait_for_terminal(self, job_id: str) -> JobEvent:
        index = 0
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                if event.job_id == job_id and isinstance(event, JobCompletedEvent):
                    return event
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)


def configuration(audio_id: str, **overrides: Any) -> dict[str, Any]:
    mode_id = cast(str, overrides.pop("mode_id", "vocals"))
    body: dict[str, Any] = {
        "audio_id": audio_id,
        "mode_id": mode_id,
        "quality_id": fake_quality_id(mode_id),
    }
    body.update(overrides)
    return body


async def create_job(client: httpx2.AsyncClient, **body: Any) -> str:
    response = await client.post(JOBS_URL, json=body)
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


async def run_to_completion(
    client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> str:
    job_id = await create_job(client, **configuration(audio_id))
    await recorder.wait_for_terminal(job_id)
    return job_id


@pytest.fixture
def usage_app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
async def usage_client(usage_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for a running application (lifespan started on this loop)."""
    async with usage_app.router.lifespan_context(usage_app):
        transport = httpx2.ASGITransport(app=usage_app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def recorder(
    usage_client: httpx2.AsyncClient, usage_app: FastAPI
) -> AsyncIterator[EventRecorder]:
    listener = EventRecorder()
    manager = cast(JobManager, usage_app.state.job_manager)
    manager.add_listener(listener)
    yield listener
    manager.remove_listener(listener)


@dataclass
class _Bucket:
    count: int = 0
    bytes: int = 0


@dataclass
class _GroundTruth:
    uploads: _Bucket = field(default_factory=_Bucket)
    job_stems: _Bucket = field(default_factory=_Bucket)
    job_exports: _Bucket = field(default_factory=_Bucket)
    orphans: _Bucket = field(default_factory=_Bucket)


def independent_ground_truth(
    data_dir: Path, audio_ids: set[str], job_ids: set[str]
) -> _GroundTruth:
    """Recompute the four buckets from a fresh ``os.walk``, independent of the module under test."""
    truth = _GroundTruth()

    def is_debris(parts: tuple[str, ...]) -> bool:
        name = parts[-1]
        if name.endswith(".tmp") or name.endswith(".part"):
            return True
        return any(part.startswith(".build-") for part in parts[:-1])

    def add(bucket: _Bucket, size: int) -> None:
        bucket.count += 1
        bucket.bytes += size

    audio_root = data_dir / "audio"
    if audio_root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(audio_root):
            for filename in filenames:
                path = Path(dirpath) / filename
                parts = path.relative_to(audio_root).parts
                size = path.stat().st_size
                if parts[0] in audio_ids and not is_debris(parts[1:]):
                    add(truth.uploads, size)
                else:
                    add(truth.orphans, size)

    jobs_root = data_dir / "jobs"
    if jobs_root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(jobs_root):
            for filename in filenames:
                path = Path(dirpath) / filename
                parts = path.relative_to(jobs_root).parts
                size = path.stat().st_size
                job_id, rest = parts[0], parts[1:]
                if job_id not in job_ids or is_debris(rest):
                    add(truth.orphans, size)
                elif rest[:1] == ("exports",):
                    add(truth.job_exports, size)
                else:
                    add(truth.job_stems, size)

    return truth


def assert_matches_ground_truth(payload: dict[str, Any], truth: _GroundTruth) -> None:
    for name, bucket in (
        ("uploads", truth.uploads),
        ("job_stems", truth.job_stems),
        ("job_exports", truth.job_exports),
        ("orphans", truth.orphans),
    ):
        assert payload[name] == {"count": bucket.count, "bytes": bucket.bytes}, name


async def test_disk_usage_endpoint_is_present_and_reports_zero_for_an_empty_app(
    usage_client: httpx2.AsyncClient,
) -> None:
    """The headline: the route exists and answers, before anything is seeded."""
    response = await usage_client.get(DISK_USAGE_URL)
    assert response.status_code == 200, response.text

    payload = response.json()
    assert set(payload) == {
        "uploads",
        "job_stems",
        "job_exports",
        "orphans",
        "free_bytes",
        "total_bytes",
    }
    for bucket_name in ("uploads", "job_stems", "job_exports", "orphans"):
        assert payload[bucket_name] == {"count": 0, "bytes": 0}
    assert isinstance(payload["free_bytes"], int)
    assert isinstance(payload["total_bytes"], int)
    assert payload["total_bytes"] > 0


async def test_disk_usage_matches_ground_truth_for_uploads_job_export_and_orphans(
    usage_client: httpx2.AsyncClient, usage_app: FastAPI, recorder: EventRecorder
) -> None:
    """Real upload, real completed job, a real export, a hand-made orphan, a stray `.part`."""
    audio_id = register_audio(usage_app)
    job_id = await run_to_completion(usage_client, recorder, audio_id)

    export_response = await usage_client.get(
        f"{JOBS_URL}/{job_id}/export", params={"stems": "vocals"}
    )
    assert export_response.status_code == 200, export_response.text

    data_dir = cast(Settings, usage_app.state.settings).data_dir
    _write(data_dir / "audio" / "01ORPHANAUDIOID000000000" / "original.wav", b"orphaned-upload")
    _write(data_dir / "jobs" / "01ORPHANJOBID0000000000" / "stems" / "x.wav", b"orphaned-job")
    _write(
        data_dir / "jobs" / job_id / "exports" / "wav_pcm24-vocals.wav.deadbeef.part",
        b"leftover-build",
    )

    manager = cast(JobManager, usage_app.state.job_manager)
    live_audio_ids = {audio_id}
    live_job_ids = {job.id for job in manager.list_jobs()}
    truth = independent_ground_truth(data_dir, live_audio_ids, live_job_ids)

    response = await usage_client.get(DISK_USAGE_URL)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert_matches_ground_truth(payload, truth)
    # Sanity: every bucket this scenario seeded actually has something in it,
    # so the comparison above is not vacuously true.
    assert truth.uploads.count > 0
    assert truth.job_stems.count > 0
    assert truth.job_exports.count > 0
    assert truth.orphans.count >= 3  # the two hand-made orphans, plus the stray .part


class _GatedSeparator:
    """A separator that starts, then parks until released — no audio work performed.

    Lifted from ``test_api_export.py``'s ``StageGatedSeparator``, trimmed to
    just what this one test needs: proof that a job's directory is visible
    (and never orphaned) while it is still running.
    """

    def __init__(self, info: SeparatorInfo, gate: asyncio.Event, started: asyncio.Event) -> None:
        self._info = info
        self.gate = gate
        self.started = started

    @property
    def info(self) -> SeparatorInfo:
        return self._info

    def runtime_stats(self) -> None:
        return None

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: Callable[[SeparationProgress], None],
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: Callable[[JobState], None] | None = None,
    ) -> SeparationResult:
        self.started.set()
        await self.gate.wait()
        progress_callback(SeparationProgress(1, 1, 1.0, 1.0))
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=[
                Stem(
                    name=stem,
                    duration_seconds=1.0,
                    sample_rate_hz=self._info.sample_rate,
                    channels=2,
                )
                for stem in self._info.stems
            ],
            metrics=SeparationResultMetrics(processing_seconds=1.0, realtime_factor=1.0),
        )


async def test_a_running_jobs_directory_counts_under_jobs_never_orphans(
    usage_client: httpx2.AsyncClient, usage_app: FastAPI, recorder: EventRecorder
) -> None:
    """A job still running has a live record from submission (feature 057); never an orphan."""
    started, gate = asyncio.Event(), asyncio.Event()

    def build(model: Any) -> _GatedSeparator:
        return _GatedSeparator(separator_info_from_model(model), gate, started)

    usage_app.state.separator_registry = SeparatorRegistry({FAKE_ARCHITECTURE: build})

    audio_id = register_audio(usage_app)
    job_id = await create_job(usage_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    try:
        response = await usage_client.get(DISK_USAGE_URL)
        assert response.status_code == 200, response.text
        payload = response.json()

        # The running job's own record (job.json) is on disk already; it must
        # be counted as a live job, never as an orphan.
        assert payload["job_stems"]["count"] >= 1
        assert payload["orphans"]["count"] == 0
    finally:
        gate.set()
        await recorder.wait_for_terminal(job_id)
